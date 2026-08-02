from odoo import api, fields, models


class LogisticsRatePlan(models.Model):
    """Versioned pricing container for one lane's scheduled shared-LTL service.

    Philosophy (Scheduled Shared LTL Network):
      - The truck is shared by multiple customers.
      - Pricing is designed so that COMBINED shipments cover the desired one-way
        route revenue.
      - Temperature (chilled / frozen) is ALWAYS a surcharge, never a copied
        rate plan with its own tier table.
      - Quantity discounts are fully editable per version — no hardcoded
        formula bands.

    Versioning rule:
      - Never edit an existing version after it has priced a real booking.
      - Close it via effective_to and create a new version instead.
      - Historical bookings keep their own frozen price snapshot regardless of
        what happens to the rate plan afterward.
    """

    _name = "logistics.rate.plan"
    _description = "Versioned rate plan for one lane / service level"
    _order = "lane_id, version desc"

    # ── Identity ──────────────────────────────────────────────────────
    service_offering_id = fields.Many2one(
        "logistics.service.offering", required=True, index=True,
        ondelete="cascade",
    )
    lane_id = fields.Many2one(
        "logistics.lane", related="service_offering_id.lane_id",
        help="Lane served by this rate plan (via the service offering).",
    )
    version = fields.Integer(required=True, default=1)
    effective_from = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    effective_to = fields.Date()
    active = fields.Boolean(default=True)

    # ── Phase 9: Pricing Mode ──────────────────────────────────────────
    pricing_mode = fields.Selection([
        ("tiered", "Tiered (Legacy)"),
        ("simple", "Simple (Revenue Target / Planned Pallets)"),
    ], default="tiered", required=True,
       help="Simple: Customer Price = Revenue Target ÷ Planned Pallets. "
            "No tiers, temperature surcharges, liftgate, or FSA adjustments. "
            "Tiered: Full legacy pipeline with tier tables, surcharges, and discounts.")

    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    name = fields.Char(compute="_compute_name", store=True)

    # ── Commercial Planning ───────────────────────────────────────────
    revenue_target = fields.Float(
        default=0.0,
        help="Minimum desired revenue for ONE truck travelling ONE direction "
             "on this lane. Internal KPI — never shown to customers.",
    )
    planned_pallets = fields.Integer(
        default=8, string="Planned Pallets",
        help="Expected combined pallets from all customers for commercial "
             "viability. NOT a minimum order quantity, NOT truck capacity. "
             "Defaults from the lane's target_load_pallets.",
    )
    truck_capacity = fields.Integer(
        related="lane_id.equipment_profile_id.max_pallets",
        help="Physical truck capacity from the lane's equipment profile.",
    )

    # ── V4 LTL Hub Pricing Fields ──────────────────────────────────────
    target_load_quantity = fields.Integer(
        default=7, string="Target Load Quantity",
        help="Pricing denominator — the planned pallet count used to compute "
             "the base price per pallet. Separate from physical truck capacity. "
             "Default 7 for long-haul routes.",
    )
    included_weight_per_pallet = fields.Float(
        default=500.0, string="Included Weight Per Pallet (lb)",
        help="Weight included in the base pallet price. Default 500 lb per pallet.",
    )
    safe_weight_capacity = fields.Float(
        default=11000.0, string="Safe Truck Weight Capacity (lb)",
        help="Operational safe freight-weight limit. Default 11,000 lb for "
             "26ft straight truck. Used to compute excess weight rate.",
    )
    customer_price_per_pallet = fields.Float(
        compute="_compute_targets", store=True,
        string="Customer Price / Pallet",
        help="Base price per pallet = Revenue Target ÷ Target Load Quantity.",
    )
    base_price_per_pallet = fields.Float(
        compute="_compute_targets", store=True,
        string="Base Price Per Pallet",
        help="Revenue Target ÷ Target Load Quantity. The base pallet rate "
             "before weight surcharge.",
    )
    excess_weight_rate = fields.Float(
        compute="_compute_targets", store=True,
        string="Excess Weight Rate ($/lb)",
        help="Revenue Target ÷ Safe Truck Weight Capacity. Per-pound rate "
             "charged for weight exceeding included weight.",
    )
    minimum_booking_charge = fields.Float(
        default=0.0, string="Minimum Booking Charge",
        help="Floor price applied when the computed charge falls below this amount.",
    )

    # ── Route Metadata ─────────────────────────────────────────────────
    distance_km = fields.Float(string="Road Distance (km)")
    suggested_rate_per_km = fields.Float(
        default=2.80, string="Suggested Rate Per KM",
        help="Distance-based suggestion tool. Configured range: $2.80–$3.10/km.",
    )
    direction = fields.Selection([
        ("east", "East"), ("west", "West"), ("north", "North"), ("south", "South"),
    ], string="Direction")
    service_type = fields.Selection([
        ("direct", "Direct"), ("feeder", "Feeder"), ("linehaul", "Linehaul"),
        ("final_delivery", "Final Delivery"),
    ], default="direct", string="Service Type")
    pricing_method = fields.Selection([
        ("simple", "Simple (Revenue Target ÷ TLQ)"),
        ("local_manual", "Local / Manual"),
        ("local_zone", "Local / Zone-Based"),
    ], default="simple", string="Pricing Method")
    locked = fields.Boolean(default=False, string="Locked",
        help="When locked, prevents automatic recalculation of target revenue.")
    effective_date = fields.Date(string="Effective Date", default=fields.Date.today)
    expiry_date = fields.Date(string="Expiry Date")

    # ── Prema AI Estimated Cost (internal only) ───────────────────────
    estimated_one_way_cost = fields.Float(
        string="Estimated One-Way Cost",
        help="Prema AI estimated one-way operating cost. Internal only.",
    )

    # ── Children ──────────────────────────────────────────────────────
    tier_ids = fields.One2many(
        "logistics.rate.tier", "rate_plan_id",
        string="Quantity Discount Table",
    )
    surcharge_ids = fields.One2many(
        "logistics.rate.plan.surcharge", "rate_plan_id",
        string="Surcharge Overrides",
    )
    fsa_adjustment_ids = fields.One2many(
        "logistics.fsa.rate.adjustment", "rate_plan_id",
        string="FSA Adjustments",
    )

    _sql_constraints = [
        (
            "offering_version_uniq",
            "unique(service_offering_id, version)",
            "This service offering already has a rate plan at this version.",
        ),
    ]

    # ── Computes ──────────────────────────────────────────────────────
    @api.depends("service_offering_id.name", "version")
    def _compute_name(self):
        for rec in self:
            base = rec.service_offering_id.name or "?"
            rec.name = f"{base} v{rec.version}"

    @api.depends("revenue_target", "planned_pallets", "target_load_quantity",
                 "safe_weight_capacity")
    def _compute_targets(self):
        for rec in self:
            tlq = max(rec.target_load_quantity, 1)
            swc = max(rec.safe_weight_capacity, 1.0)
            rec.customer_price_per_pallet = rec.revenue_target / tlq
            rec.base_price_per_pallet = rec.revenue_target / tlq
            rec.excess_weight_rate = rec.revenue_target / swc

    # ── CRUD ──────────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("version") and vals.get("service_offering_id"):
                last = self.search(
                    [("service_offering_id", "=", vals["service_offering_id"])],
                    order="version desc", limit=1,
                )
                vals["version"] = (last.version + 1) if last else 1
        return super().create(vals_list)

    # ── Actions ───────────────────────────────────────────────────────
    def action_create_new_version(self):
        """Close the current version and create a new one, copying all
        configuration (tiers, surcharges, FSA adjustments)."""
        self.ensure_one()
        today = fields.Date.context_today(self)

        # Close current version
        self.write({"effective_to": today, "active": False})

        # Build copy data
        new_vals = self.copy_data(default={
            "version": self.version + 1,
            "effective_from": today,
            "effective_to": False,
            "active": True,
            "estimated_one_way_cost": self.lane_id.estimated_one_way_cost or self.estimated_one_way_cost,
        })[0]

        new_plan = self.create(new_vals)

        # Copy tiers
        for tier in self.tier_ids:
            tier.copy({"rate_plan_id": new_plan.id})

        # Copy surcharge overrides
        for sur in self.surcharge_ids:
            sur.copy({"rate_plan_id": new_plan.id})

        # Copy FSA adjustments
        for adj in self.fsa_adjustment_ids:
            adj.copy({"rate_plan_id": new_plan.id})

        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.rate.plan",
            "res_id": new_plan.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_regenerate_tiers(self):
        """Regenerate pallet pricing tiers from current Revenue Target and
        Planned Pallets.  NOTE: tiers are no longer used for customer pricing
        (the simple revenue_target / planned_pallets formula is used instead).
        This method is kept for backward compatibility only."""
        self.ensure_one()
        target = self.customer_price_per_pallet or 225.0

        self.tier_ids.filtered(lambda t: t.tier_type == "pallet").unlink()

        Tier = self.env["logistics.rate.tier"]
        truck_cap = self.truck_capacity or 13
        for qty in range(1, truck_cap + 1):
            Tier.create({
                "rate_plan_id": self.id,
                "tier_type": "pallet",
                "min_qty": qty,
                "max_qty": qty,
                "calc_method": "per_unit",
                "rate": round(target, 2),
            })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Pricing Tiers Regenerated",
                "message": (
                    f"{truck_cap} rows (1–{truck_cap} pallets) at "
                    f"${target:.2f}/pallet. "
                    f"Note: tiers are no longer used for customer pricing."
                ),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

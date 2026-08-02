from odoo import api, fields, models


class LogisticsLane(models.Model):
    _name = "logistics.lane"
    _description = "Region-pair service capability (no schedule/price here)"
    _order = "origin_region_id, destination_region_id"

    origin_region_id = fields.Many2one("logistics.region", required=True, index=True)
    destination_region_id = fields.Many2one("logistics.region", required=True, index=True)
    
    # V4 Map & routing fields
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    direct_allowed = fields.Boolean(default=True, string="Direct Allowed")
    via_hub_allowed = fields.Boolean(default=True, string="Via Hub Allowed")
    default_hub_id = fields.Many2one('logistics.hub', string='Default Hub')
    route_status = fields.Selection([('active','Active'),('incomplete','Incomplete'),('inactive','Inactive')], default='active')
    pricing_status = fields.Selection([('configured','Configured'),('pending','Pending'),('review','Needs Review')], default='configured')

    active = fields.Boolean(default=True)
    ltl_capable = fields.Boolean(default=True)
    ftl_capable = fields.Boolean(default=True)
    equipment_profile_id = fields.Many2one("logistics.equipment.profile")
    max_pallets = fields.Integer()
    max_weight_lbs = fields.Float()
    reefer_supported = fields.Boolean()
    road_km = fields.Float(string="Est. Road km", help="Estimated road distance between region centroids.")
    revenue_target = fields.Float(string="Minimum Revenue Target", help="Minimum target revenue per way for this lane.")
    preferred_revenue_target = fields.Float(string="Preferred Revenue Target", help="Preferred target revenue per way.")
    target_load_pallets = fields.Integer(string="Planned Pallets", default=6,
        help="Realistic number of paying pallets expected for commercial viability.")
    target_avg_per_pallet = fields.Float(string="Target Avg / Pallet", compute="_compute_target_avg", store=True,
        help="Minimum Revenue Target ÷ Planning Pallets.")
    preferred_avg_per_pallet = fields.Float(string="Preferred Avg / Pallet", compute="_compute_target_avg", store=True,
        help="Preferred Revenue Target ÷ Planning Pallets.")

    # ── One-Way Cost (from Prema AI Estimator) ──────────────────────
    estimated_one_way_cost = fields.Float(string="Estimated One-Way Cost",
        help="Prema AI Estimator cost for representative one-way route on this lane.")
    cost_estimated_at = fields.Datetime(string="Cost Estimated At", readonly=True)
    cost_vehicle_id = fields.Many2one("fleet.vehicle", string="Cost Estimate Vehicle", readonly=True)
    target_net_profit = fields.Float(string="Target NET Profit", compute="_compute_profit", store=True,
        help="Revenue Target − Estimated One-Way Cost.")
    target_margin_pct = fields.Float(string="Target Margin %", compute="_compute_profit", store=True,
        help="NET Profit ÷ Revenue Target × 100.")

    # ── Phase 12: Round-Trip Profit ──────────────────────────────────
    return_revenue_target = fields.Float(
        string="Return Revenue Target",
        help="Revenue target for the return/backhaul direction."
    )
    return_estimated_cost = fields.Float(
        string="Return Estimated Cost",
        help="Estimated cost for the return/backhaul direction."
    )
    round_trip_revenue = fields.Float(
        string="Round Trip Revenue", compute="_compute_round_trip", store=True,
        help="Outbound revenue target + return revenue target."
    )
    round_trip_cost = fields.Float(
        string="Round Trip Cost", compute="_compute_round_trip", store=True,
        help="Outbound estimated cost + return estimated cost."
    )
    round_trip_profit = fields.Float(
        string="Round Trip Profit", compute="_compute_round_trip", store=True,
        help="Round trip revenue − round trip cost."
    )
    round_trip_margin_pct = fields.Float(
        string="Round Trip Margin %", compute="_compute_round_trip", store=True,
        help="Round trip profit ÷ round trip revenue × 100."
    )

    @api.depends("revenue_target", "preferred_revenue_target", "estimated_one_way_cost",
                 "return_revenue_target", "return_estimated_cost")
    def _compute_round_trip(self):
        for rec in self:
            outbound_rev = rec.revenue_target or rec.preferred_revenue_target or 0
            outbound_cost = rec.estimated_one_way_cost or 0
            return_rev = rec.return_revenue_target or 0
            return_cost = rec.return_estimated_cost or 0

            rec.round_trip_revenue = outbound_rev + return_rev
            rec.round_trip_cost = outbound_cost + return_cost
            rec.round_trip_profit = rec.round_trip_revenue - rec.round_trip_cost
            rec.round_trip_margin_pct = (
                rec.round_trip_profit / rec.round_trip_revenue * 100
            ) if rec.round_trip_revenue else 0.0

    via_hub_id = fields.Many2one("logistics.region", string="Via Hub", help="If set, this lane routes through a hub region (hub-and-spoke).")

    # ── Corridor linkage (Phase 2) ──────────────────────────────────
    corridor_ids = fields.Many2many("logistics.corridor", "corridor_lane_rel",
                                    "lane_id", "corridor_id", string="Serving Corridors",
                                    help="Operational corridors that serve this commercial lane.")

    phase = fields.Char(string="Phase", default="1", help="Deployment phase: 1=active, 2=planned, etc.")
    name = fields.Char(compute="_compute_name", store=True)

    @api.depends("revenue_target", "preferred_revenue_target", "target_load_pallets")
    def _compute_target_avg(self):
        for rec in self:
            pals = rec.target_load_pallets or 1
            rec.target_avg_per_pallet = rec.revenue_target / pals if rec.revenue_target else 0.0
            rec.preferred_avg_per_pallet = rec.preferred_revenue_target / pals if rec.preferred_revenue_target else 0.0

    @api.depends("revenue_target", "estimated_one_way_cost")
    def _compute_profit(self):
        for rec in self:
            rec.target_net_profit = (rec.revenue_target or 0) - (rec.estimated_one_way_cost or 0)
            rec.target_margin_pct = (rec.target_net_profit / rec.revenue_target * 100) if rec.revenue_target else 0.0

    def action_refresh_cost(self):
        """Call Prema AI Estimator for one-way cost on this lane."""
        self.ensure_one()
        if not self.road_km:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Missing road distance", "message": "Set road_km first.", "type": "warning"}}
        try:
            from odoo.addons.premafirm_ai_engine.services.pricing_engine import PricingEngine
        except ImportError:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Estimator unavailable", "message": "Prema AI Engine not installed.", "type": "warning"}}

        vehicle = self.env["fleet.vehicle"].search([
            ("active","=",True),("x_operational_logistics","=",True)], limit=1)
        if not vehicle:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "No operational vehicle", "message": "No operational truck found.", "type": "warning"}}

        distance = self.road_km or 200
        duration = distance / 75.0  # ~75 km/h average
        load = (self.target_load_pallets or 6) * 800  # 800 lb/pallet

        engine = PricingEngine(self.env)
        try:
            costs = engine.calculate(vehicle.id, distance, duration, load_weight_lbs=load)
        except ValueError as e:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Cost error", "message": str(e), "type": "danger"}}

        self.write({
            "estimated_one_way_cost": costs["total_cost"],
            "cost_estimated_at": fields.Datetime.now(),
            "cost_vehicle_id": vehicle.id,
        })
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Cost Updated",
                           "message": f"One-way: ${costs['total_cost']:.0f} | NET: ${self.target_net_profit:.0f} | Margin: {self.target_margin_pct:.1f}%",
                           "type": "success"}}

    def action_open_one_way_estimator(self):
        """Open Prema AI Estimator pre-configured for one-way route:
        Hub → Destination Region. No margin, one-way only."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()

        # Get Hub coordinates
        hub_lat = float(ICP.get_param("estimator.hub_lat", "0") or "0")
        hub_lng = float(ICP.get_param("estimator.hub_lng", "0") or "0")
        hub_addr = ICP.get_param("estimator.hub_address", "") or ICP.get_param("estimator.hub_name", "") or "Hub"

        # Get destination region info
        dest = self.destination_region_id
        dest_name = dest.name or ""

        # Build one-way stops: Hub → Destination
        stops = [
            {"type": "pickup", "address": hub_addr, "lat": hub_lat, "lng": hub_lng},
            {"type": "delivery", "address": dest_name, "lat": 0, "lng": 0},
        ]

        # Find operational vehicle
        vehicle = self.env["fleet.vehicle"].search([
            ("active", "=", True), ("x_operational_logistics", "=", True)], limit=1)
        if not vehicle:
            vehicle = self.env["fleet.vehicle"].search([("active", "=", True)], limit=1)

        import json
        ctx = {
            "default_vehicle_id": vehicle.id if vehicle else False,
            "default_origin_address": hub_addr,
            "default_origin_lat": hub_lat,
            "default_origin_lng": hub_lng,
            "default_destination_address": dest_name,
            "default_stops_json": json.dumps(stops),
            "default_load_pallets": self.target_load_pallets or 8,
            "default_margin_pct": 0.0,  # No margin for one-way costing
            "default_partner_id": False,
            "one_way_mode": True,
        }

        return {
            "type": "ir.actions.act_window",
            "name": f"One-Way Estimator: {self.name}",
            "res_model": "premafirm.rate.estimator",
            "view_mode": "form",
            "target": "current",
            "context": ctx,
        }

    _sql_constraints = [
        (
            "lane_pair_uniq",
            "unique(origin_region_id, destination_region_id)",
            "A lane between these two regions already exists.",
        ),
    ]

    @api.depends("origin_region_id.name", "destination_region_id.name",
                 "origin_region_id.code", "destination_region_id.code")
    def _compute_name(self):
        for rec in self:
            origin = rec.origin_region_id.name or rec.origin_region_id.code or "?"
            dest = rec.destination_region_id.name or rec.destination_region_id.code or "?"
            rec.name = f"{origin} → {dest}"

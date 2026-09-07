from odoo import api, fields, models

# package_uom values shared by the booking line's loose-freight intake
# (no package-unit selection existed in the module before TODO 7).
PACKAGE_UOM_SELECTION = [
    ("cases", "Cases"),
    ("cartons", "Cartons"),
    ("totes", "Totes"),
    ("pieces", "Pieces"),
    ("bags", "Bags"),
    ("other", "Other"),
]


class LogisticsBookingLine(models.Model):
    _name = "logistics.booking.line"
    _description = "Freight line on a confirmed booking (maps 1:1 to a prema.dispatch.item)"
    _order = "booking_id, sequence"

    booking_id = fields.Many2one("logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    description = fields.Char(default="LTL Shipment")
    # Legacy committed/quote pallet count — required NOT NULL for every
    # booking line, kept untouched. It remains the operative quantity for
    # palletized freight; loose/mixed freight may legitimately carry 0 here
    # (0 satisfies the NOT NULL column) while planned_pallet_equivalent
    # carries the floor footprint.
    pallets = fields.Integer(required=True, default=1)
    weight_lbs = fields.Float(required=True)
    commodity = fields.Char()

    # ── Phase 5: Multi-Leg ────────────────────────────────────────────
    leg_id = fields.Many2one(
        "logistics.booking.leg", string="Leg",
        help="Which operational leg this freight rides on (for multi-leg bookings)."
    )

    # ── TODO 7: Loose / Mixed freight (canonical original record) ─────
    # These fields capture HOW the freight was originally booked/handled.
    # They are the ORIGINAL intake record — nothing here mutates quantities
    # later: pallet building (a later task) appends new actuals and never
    # overwrites these originals.
    handling_type = fields.Selection([
        ("palletized", "Palletized"),
        ("loose_floor_loaded", "Loose / Floor Loaded"),
        ("mixed", "Mixed (Pallets + Loose)"),
    ], string="Handling Type", default="palletized", required=True,
        help="How the freight is packed: on shipper pallets, loose on the "
             "floor, or a mixture of both. Loose/mixed lines may carry zero "
             "real pallets — their floor footprint is "
             "planned_pallet_equivalent.")
    package_quantity = fields.Integer(
        string="Package Quantity", default=0,
        help="Number of loose units in package_uom (0 when the freight is "
             "fully palletized and not counted in packages).")
    package_uom = fields.Selection(
        PACKAGE_UOM_SELECTION, string="Package Unit",
        help="Unit of measure for package_quantity.")
    case_count = fields.Integer(
        string="Case Count", default=0,
        help="Number of cases when the freight is counted in cases "
             "(the usual loose-freight unit).")
    actual_pallet_count = fields.Integer(
        string="Actual Pallets", default=0,
        help="Real pallets present at intake. 0 is legitimate for "
             "loose/mixed freight (pallet-equivalent floor space is "
             "planned_pallet_equivalent).")
    planned_pallet_equivalent = fields.Float(
        string="Pallet Equivalent", digits=(10, 1),
        compute="_compute_planned_pallet_equivalent", store=True,
        readonly=False,
        help="Pallet-equivalent floor space this freight must reserve. For "
             "palletized freight this equals the real pallet count; for "
             "loose freight it is the estimate (e.g. 10 cases ~ 1.0). "
             "Capacity logic must consume THIS for loose/mixed lines "
             "instead of dropping them to zero pallets.")
    current_load_form = fields.Selection([
        ("loose", "Loose / Floor Loaded"),
        ("shipper_palletized", "Shipper-Palletized"),
        ("carrier_palletized", "Carrier-Palletized"),
    ], string="Current Load Form",
        compute="_compute_current_load_form", store=True, readonly=False,
        help="Physical form of the freight on the truck. Derived from "
             "handling_type at intake (palletized → shipper-palletized, "
             "otherwise loose); may be switched to carrier-palletized once "
             "built.")
    carrier_pallet_used = fields.Boolean(
        string="Carrier Pallet Used",
        help="A carrier pallet was used to build this freight once loaded.")
    pallets_built = fields.Integer(
        string="Pallets Built", default=0,
        help="How many pallets were actually built from this freight "
             "(pallet building appends new actuals — never overwrites the "
             "original quantities above).")
    hand_bomb_required = fields.Boolean(
        string="Hand Bomb Required",
        help="Freight must be handled piece-by-piece by hand (no pallet "
             "jack/forklift unit can handle it).")
    handling_notes = fields.Text(string="Handling Notes")

    # ── Derived stored fields (same editable-stored-compute pattern as
    # prema.dispatch.item.consumes_floor_position) ────────────────────

    @api.depends("handling_type", "pallets", "actual_pallet_count")
    def _compute_planned_pallet_equivalent(self):
        """Palletized freight occupies exactly its real pallet count;
        loose/mixed freight carries an explicit estimate (entered at
        intake) and is never auto-guessed from counts."""
        for line in self:
            if line.handling_type == "palletized":
                line.planned_pallet_equivalent = float(
                    line.actual_pallet_count or line.pallets or 0)

    @api.depends("handling_type")
    def _compute_current_load_form(self):
        for line in self:
            # Editable override guard (same shape as
            # dispatch_item._compute_consumes_floor_position): only re-derive
            # when the handling type actually changed or no value exists yet
            # — a manually-set carrier_palletized stays until then.
            if not line.id or line._origin.handling_type != line.handling_type \
                    or not line.current_load_form:
                line.current_load_form = (
                    "shipper_palletized"
                    if line.handling_type == "palletized" else "loose")

    # ── On-change helpers ─────────────────────────────────────────────

    @api.onchange("handling_type")
    def _onchange_handling_type(self):
        if not self.handling_type:
            self.handling_type = "palletized"
        if self.handling_type == "palletized" and self.pallets:
            # Palletized keeps the real pallet count as the operative
            # quantity; planned_pallet_equivalent follows automatically
            # (stored compute).
            self.actual_pallet_count = self.pallets
        # Loose/mixed: never forced to enter real pallets — actual_pallet_
        # count may stay 0 while planned_pallet_equivalent > 0 (entered
        # below / at intake).

    @api.onchange("pallets")
    def _onchange_pallets(self):
        if self.handling_type == "palletized" and self.pallets \
                and not self.actual_pallet_count:
            self.actual_pallet_count = self.pallets

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Canonical defaulting for callers that only know the legacy
            # pallet count: a palletized line's real pallets ARE that
            # count. Loose/mixed intake stays at actual_pallet_count=0 —
            # nothing here fabricates real pallets.
            if vals.get("handling_type", "palletized") == "palletized" \
                    and "actual_pallet_count" not in vals \
                    and vals.get("pallets") is not None:
                vals["actual_pallet_count"] = vals.get("pallets") or 0
        return super().create(vals_list)

    # ── Dispatch vocabulary mapping (TODO 7) ──────────────────────────

    def _dispatch_load_unit_type(self):
        """Map this line's original handling_type onto the dispatch-item
        load_unit_type vocabulary (prema.dispatch.item):

            palletized          → 'pallet'
            loose_floor_loaded  → 'loose'
            mixed               → 'pallet' only when every unit is
                                  palletized (no loose cases/packages
                                  recorded), else 'loose' — a mixed line
                                  then rides on capacity_equivalent as one
                                  item carrying the whole footprint.

        Used by the booking→dispatch conversion when it creates the
        1:1 dispatch item for this line."""
        self.ensure_one()
        if self.handling_type == "palletized":
            return "pallet"
        if self.handling_type == "loose_floor_loaded":
            return "loose"
        # mixed — only a fully palletized line is a real pallet item.
        loose_units = (self.package_quantity or 0) + (self.case_count or 0)
        if not loose_units and (self.actual_pallet_count or self.pallets):
            return "pallet"
        return "loose"

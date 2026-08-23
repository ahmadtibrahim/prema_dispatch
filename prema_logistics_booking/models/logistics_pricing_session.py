import re
import uuid

from odoo import api, fields, models

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("reefer", "Reefer")]

# Hub role codes used internally by the routing engine (shipment_routing_service)
# for multi-leg hub-and-spoke itineraries. Never shown to customers verbatim.
TRANSFER_LEG_TYPES = {
    "feeder_to_hub", "final_mile", "linehaul", "transfer", "hub_transfer",
}

_LEG_TYPE_RE = re.compile(r"\(([^()]+)\)\s*$")


def _customer_safe_leg_label(label, leg_index, total_legs):
    """Customer-safe display label for a frozen price_snapshot leg line.

    Internal routing language (leg numbers, corridor names, hub role codes
    like feeder_to_hub / final_mile) never reaches the portal. The frozen
    snapshot keeps its raw labels untouched — this function only maps them
    for display:
    - A single-leg itinerary → "Scheduled LTL Transportation".
    - Multi-leg (hub-and-spoke) → the first leg keeps the plain label and
      every transfer leg reads "Via PremaFirm Hub".
    Non-"Leg …" lines (adjustments like "Volume discount (10%)") are already
    customer-safe and pass through unchanged."""
    if not str(label).startswith("Leg "):
        return label
    if total_legs == 1 or leg_index == 1:
        return "Scheduled LTL Transportation"
    match = _LEG_TYPE_RE.search(label)
    leg_type = (match.group(1) if match else "").strip().lower()
    if leg_type in TRANSFER_LEG_TYPES:
        return "Via PremaFirm Hub"
    return "Scheduled LTL Transportation"


class LogisticsPricingSession(models.TransientModel):
    """Short-lived, server-authoritative price result.

    TransientModel is the correct native fit here — Odoo auto-vacuums old
    rows on its own schedule, so there is no cron to write. The customer's
    browser only ever holds `token`, never the price itself as an
    authoritative value; every confirm re-reads/re-validates server-side.
    """

    _name = "logistics.pricing.session"
    _description = "Short-lived, server-authoritative freight price + schedule result"

    token = fields.Char(default=lambda self: uuid.uuid4().hex, index=True, required=True, copy=False)
    partner_id = fields.Many2one("res.partner", required=True, index=True)
    pickup_fsa_id = fields.Many2one("logistics.fsa")
    delivery_fsa_id = fields.Many2one("logistics.fsa")
    corridor_id = fields.Many2one(
        "logistics.corridor", string="Priced Corridor", readonly=True,
        help="Primary operational corridor frozen by the active quote.",
    )
    departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Selected Departure", readonly=True,
        help="The EXACT scheduled departure this quote is bound to — the "
             "same departure the customer selected on the booking calendar. "
             "Confirmation re-validates it (corridor, date, active, status).",
    )
    service_offering_id = fields.Many2one("logistics.service.offering")
    rate_plan_id = fields.Many2one("logistics.rate.plan")

    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, required=True, default="ltl")
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, required=True, default="dry")
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        help="Numeric required temperature for Reefer quotes. 0.0 is a valid value.",
    )
    pallets = fields.Integer(required=True)
    physical_pallets = fields.Integer(
        string="Physical Pallets", default=1, required=True,
        help="Actual physical handling units on the truck. May differ from "
             "pallets (sum of per-stop) when pallets are shared across stops.",
    )
    shared_pallet_mode = fields.Boolean(
        string="Shared Pallet Mode", default=False,
        help="True when one or more physical pallets are shared across multiple delivery stops.",
    )
    weight_lbs = fields.Float(required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean()

    pickup_saved_location_id = fields.Many2one(
        "logistics.saved.location", string="Pickup Saved Location",
        help="Frozen from Step 1 selection — used for Step 3 display.",
    )
    delivery_saved_location_id = fields.Many2one(
        "logistics.saved.location", string="Delivery Saved Location",
        help="Frozen from Step 1 selection — used for Step 3 display.",
    )

    delivery_stop_ids = fields.One2many(
        "logistics.pricing.session.stop", "session_id",
        string="Delivery Stops",
    )
    stop_ids = fields.One2many(
        "logistics.pricing.session.stop", "session_id",
        string="Route Stops",
        help="ALL ordered route stops (pickups + deliveries) for "
             "generalized milk-run sessions; legacy sessions only carry "
             "delivery rows.",
    )

    pickup_date = fields.Date()
    delivery_date_estimate = fields.Date()

    price_snapshot = fields.Json()
    route_snapshot = fields.Json(string="Route Snapshot",
        help="Immutable corridor legs, exact departures, trucks, distance and frozen prices.")
    calculated_price = fields.Float()

    state = fields.Selection(
        [("priced", "Priced"), ("not_available", "Not Available"), ("converted", "Converted")],
        default="priced", required=True,
    )
    expires_at = fields.Datetime(required=True)

    _sql_constraints = [
        ("token_uniq", "unique(token)", "Pricing session token must be unique."),
    ]

    def is_expired(self):
        self.ensure_one()
        return fields.Datetime.now() > self.expires_at

    def _get_pallet_allocations(self):
        """Extract pallet_allocations from price_snapshot (zero-migration
        approach, same convention as logistics.booking). The quote page
        renders session.pallet_allocations directly."""
        return self.env["logistics.booking"]._extract_pallet_allocs_from_snapshot(
            self.price_snapshot,
        )

    def customer_safe_snapshot_lines(self):
        """Snapshot lines with internal routing language removed, for
        customer-facing pricing display. Amounts are untouched — the frozen
        snapshot remains the authoritative pricing record; only labels are
        mapped. The sum of the returned amounts equals calculated_price by
        construction (no line is added, dropped, or re-priced)."""
        self.ensure_one()
        snapshot = self.price_snapshot or []
        total_legs = len([
            line for line in snapshot
            if isinstance(line, dict)
            and str(line.get("label", "")).startswith("Leg ")
        ])
        leg_index = 0
        lines = []
        for line in snapshot:
            if not isinstance(line, dict):
                continue
            label = line.get("label", "")
            if str(label).startswith("Leg "):
                leg_index += 1
                label = _customer_safe_leg_label(label, leg_index, total_legs)
            lines.append({"label": label, "amount": line.get("amount", 0.0) or 0.0})
        return lines

    pallet_allocations = fields.Json(compute="_compute_pallet_allocations")

    @api.depends("price_snapshot")
    def _compute_pallet_allocations(self):
        for session in self:
            session.pallet_allocations = session._get_pallet_allocations()

    @api.autovacuum
    def _gc_expired_sessions(self):
        # Belt-and-suspenders on top of Odoo's own TransientModel vacuum —
        # expired sessions are useless the moment they expire regardless of
        # the vacuum cadence.
        self.search([("expires_at", "<", fields.Datetime.now())]).sudo().unlink()

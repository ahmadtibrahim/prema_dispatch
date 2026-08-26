# ════════════════════════════════════════════════════════════════════
# Phase 13.2 — Carrier LANE RATE (BUY pricing only).
#   One lightweight operational model for subcontract lanes. NEVER mixed
#   with customer SELL pricing (separate models/authorities — RULE 3).
# ════════════════════════════════════════════════════════════════════
from datetime import date

from odoo import api, fields, models


class LogisticsCarrierLaneRate(models.Model):
    _name = "logistics.carrier.lane.rate"
    _description = "Carrier Lane Rate (BUY)"
    _order = "carrier_id, origin_region_id, destination_region_id, " \
             "effective_from desc"

    name = fields.Char(string="Rate", compute="_compute_name", store=True)
    carrier_id = fields.Many2one(
        "res.partner", string="Carrier", required=True,
        ondelete="cascade", index=True)
    origin_region_id = fields.Many2one(
        "logistics.region", string="Origin Region", required=True,
        index=True)
    destination_region_id = fields.Many2one(
        "logistics.region", string="Destination Region", required=True,
        index=True)
    equipment_type = fields.Selection([
        ("dry", "Dry"), ("reefer", "Reefer"), ("chilled", "Chilled"),
        ("frozen", "Frozen"),
    ], string="Equipment", default="dry")
    pricing_method = fields.Selection([
        ("flat_rate", "Flat Rate"),
        ("rate_per_km", "Rate per km"),
    ], string="Pricing Method", default="flat_rate")
    rate = fields.Float(string="Rate", digits=(12, 2))
    minimum_charge = fields.Float(
        string="Minimum Charge", digits=(12, 2),
        help="Minimum payable even when a per-km rate computes lower.")
    effective_from = fields.Date(string="Effective From")
    effective_to = fields.Date(string="Effective To")
    active = fields.Boolean(string="Active", default=True)
    # Optional buy-side accessorials (kept separate from customer SELL).
    detention_free_minutes = fields.Integer(
        string="Carrier Detention Free (min)", default=30)
    detention_rate_per_increment = fields.Float(
        string="Carrier Detention Rate", digits=(12, 2),
        help="BUY-side detention rate — what PREMAFIRM pays, independent "
             "from the customer SELL detention rate.")
    extra_stop_charge = fields.Float(string="Extra Stop Charge", digits=(12, 2))
    currency_id = fields.Many2one(
        "res.currency", readonly=True,
        default=lambda self: self.env.company.currency_id)

    @api.depends("carrier_id", "origin_region_id", "destination_region_id",
                 "equipment_type")
    def _compute_name(self):
        for rate in self:
            rate.name = "%s %s→%s (%s)" % (
                rate.carrier_id.name or rate.carrier_id.id,
                rate.origin_region_id.code, rate.destination_region_id.code,
                rate.equipment_type)

    @api.model
    def _match(self, carrier_id, origin_region_id, destination_region_id,
               equipment_type="dry", on_date=None):
        """Return the most recent effective BUY lane rate for a carrier,
        or False. The carrier rate CARD is one BUY estimation authority —
        the hierarchy in the scenario engine is: accepted offer →
        lane rate → quote → configured market authority."""
        on_date = on_date or date.today()
        # A card entry without effective dates is open-ended (always
        # effective) — NULL dates must not silently exclude it.
        rates = self.search([
            ("carrier_id", "=", carrier_id),
            ("origin_region_id", "=", origin_region_id),
            ("destination_region_id", "=", destination_region_id),
            ("equipment_type", "=", equipment_type),
            ("active", "=", True),
            "|", ("effective_from", "=", False),
            ("effective_from", "<=", on_date),
            "|", ("effective_to", "=", False), ("effective_to", ">=", on_date),
        ], limit=1)
        return rates[:1] if rates else self.env[self._name]

    def _buy_cost(self, distance_km=0.0):
        """Compute the BUY cost from this rate card entry."""
        self.ensure_one()
        if self.pricing_method == "rate_per_km":
            cost = (self.rate or 0.0) * (distance_km or 0.0)
        else:
            cost = self.rate or 0.0
        if self.minimum_charge and cost < self.minimum_charge:
            cost = self.minimum_charge
        return cost

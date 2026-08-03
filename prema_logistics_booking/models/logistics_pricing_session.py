import uuid

from odoo import api, fields, models

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("reefer", "Reefer")]


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
    pickup_fsa_id = fields.Many2one("logistics.fsa", required=True)
    delivery_fsa_id = fields.Many2one("logistics.fsa", required=True)
    service_offering_id = fields.Many2one("logistics.service.offering")
    rate_plan_id = fields.Many2one("logistics.rate.plan")

    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, required=True, default="ltl")
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, required=True, default="dry")
    pallets = fields.Integer(required=True)
    weight_lbs = fields.Float(required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean()

    pickup_date = fields.Date()
    delivery_date_estimate = fields.Date()

    price_snapshot = fields.Json()
    route_snapshot = fields.Json(string="Route Snapshot",
        help="Immutable route details: legs, rate plans, prices, origin/destination regions.")
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

    @api.autovacuum
    def _gc_expired_sessions(self):
        # Belt-and-suspenders on top of Odoo's own TransientModel vacuum —
        # expired sessions are useless the moment they expire regardless of
        # the vacuum cadence.
        self.search([("expires_at", "<", fields.Datetime.now())]).sudo().unlink()

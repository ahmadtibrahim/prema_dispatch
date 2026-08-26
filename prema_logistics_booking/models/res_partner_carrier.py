# ════════════════════════════════════════════════════════════════════
# Phase 13.1/13.7 — Carrier network on res.partner (the ONE vendor
#   authority). No second carrier master; no duplicate contacts. Carrier
#   performance stats are computed from executed subcontract legs.
# ════════════════════════════════════════════════════════════════════
from odoo import api, fields, models


class ResPartnerCarrier(models.Model):
    _inherit = "res.partner"

    # ── Carrier profile ─────────────────────────────────────────────
    is_transport_carrier = fields.Boolean(
        string="Transport Carrier", default=False, tracking=True,
        help="Mark this partner as a subcontract transport carrier "
             "(owner-operator / carrier).")
    carrier_status = fields.Selection([
        ("active", "Active"),
        ("inactive", "Inactive"),
        ("pending_compliance", "Pending Compliance"),
        ("blocked", "Blocked"),
    ], string="Carrier Status", default="pending_compliance", tracking=True)
    carrier_home_region_id = fields.Many2one(
        "logistics.region", string="Home Region / Base")
    carrier_equipment_ids = fields.Many2many(
        "logistics.equipment.profile", string="Equipment Capabilities",
        help="Straight truck / reefer / dry / liftgate etc.")
    carrier_service_region_ids = fields.Many2many(
        "logistics.region", string="Service Regions / Preferred Lanes")
    carrier_max_pallets = fields.Integer(string="Max Pallets")
    carrier_max_weight_lbs = fields.Float(string="Max Weight (lb)")
    cross_border_capable = fields.Boolean(
        string="Cross-Border Capable",
        help="Compliance capability only — cross-border subcontract "
             "execution still requires the configured authority.")
    carrier_insurance_expiry = fields.Date(string="Insurance Expiry")
    carrier_notes = fields.Text(string="Carrier Notes")

    # ── Performance history (13.7 — simple derived stats) ───────────
    carrier_loads_completed = fields.Integer(
        string="Loads Completed", compute="_compute_carrier_stats")
    carrier_on_time_pct = fields.Float(
        string="On-Time %", compute="_compute_carrier_stats",
        help="Delivered subcontract legs with POD received, as a "
             "percentage of delivered legs.")
    carrier_invoice_variance_count = fields.Integer(
        string="Invoice Variances", compute="_compute_carrier_stats")
    carrier_avg_buy_rate = fields.Float(
        string="Avg Accepted Buy Rate", digits=(12, 2),
        compute="_compute_carrier_stats")

    @api.depends("is_transport_carrier")
    def _compute_carrier_stats(self):
        Leg = self.env["logistics.booking.leg"]
        for partner in self:
            if not partner.is_transport_carrier:
                partner.carrier_loads_completed = 0
                partner.carrier_on_time_pct = 0.0
                partner.carrier_invoice_variance_count = 0
                partner.carrier_avg_buy_rate = 0.0
                continue
            legs = Leg.search([
                ("executing_carrier_id", "=", partner.id),
                ("execution_mode", "=", "subcontracted"),
            ])
            delivered = legs.filtered(
                lambda l: l.execution_status == "delivered")
            podded = delivered.filtered(lambda l: l.pod_received)
            varied = legs.filtered(
                lambda l: l.carrier_invoice_variance and
                abs(l.carrier_invoice_variance) > 0.009)
            rated = legs.filtered(lambda l: l.accepted_buy_rate)
            partner.carrier_loads_completed = len(delivered)
            partner.carrier_on_time_pct = round(
                len(podded) / len(delivered) * 100.0, 1) if delivered else 0.0
            partner.carrier_invoice_variance_count = len(varied)
            partner.carrier_avg_buy_rate = round(
                sum(l.accepted_buy_rate for l in rated) / len(rated), 2) \
                if rated else 0.0

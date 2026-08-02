from odoo import fields, models


class LogisticsCustomerRate(models.Model):
    _name = "logistics.customer.rate"
    _description = "Customer-specific contract discount/override, effective-dated on its own calendar"
    _order = "partner_id, effective_from desc"

    partner_id = fields.Many2one("res.partner", required=True, index=True)
    lane_id = fields.Many2one("logistics.lane", help="Leave empty to apply to all lanes for this customer.")
    service_offering_id = fields.Many2one(
        "logistics.service.offering",
        help="Leave empty to apply to all offerings on the selected lane (or all lanes, if lane is also empty).",
    )
    discount_pct = fields.Float(help="Applied after base+tiers+surcharges, before the minimum-charge floor.")
    override_rate_plan_id = fields.Many2one("logistics.rate.plan")
    effective_from = fields.Date(required=True, default=fields.Date.context_today)
    effective_to = fields.Date()
    active = fields.Boolean(default=True)

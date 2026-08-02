from odoo import fields, models


class LogisticsServiceLevel(models.Model):
    _name = "logistics.service.level"
    _description = "Reusable service-level catalog (SAME_DAY / NEXT_DAY / etc.)"
    _order = "sequence, code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    max_transit_hours = fields.Float()
    reefer_food_eligible = fields.Boolean(
        help="If checked, this service level may be offered for chilled/frozen "
             "shipments. Gates the reefer/perishable scheduling restriction."
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Service level code must be unique."),
    ]

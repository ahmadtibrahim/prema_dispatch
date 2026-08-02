from odoo import api, fields, models

TEMPERATURE_MODE_SELECTION = [
    ("dry", "Dry"),
    ("chilled", "Chilled"),
    ("frozen", "Frozen"),
]

SHIPMENT_TYPE_SELECTION = [
    ("ltl", "LTL"),
    ("ftl", "FTL"),
    ("both", "LTL & FTL"),
]


class LogisticsServiceOffering(models.Model):
    _name = "logistics.service.offering"
    _description = "One bookable service on one lane (e.g. R1->R7 Next Day Chilled)"
    _order = "lane_id, service_level_id"

    lane_id = fields.Many2one("logistics.lane", required=True, index=True, ondelete="cascade")
    service_level_id = fields.Many2one("logistics.service.level", required=True, index=True)
    active = fields.Boolean(default=True)
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, default="dry", required=True)
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, default="ltl", required=True)
    name = fields.Char(compute="_compute_name", store=True)

    _sql_constraints = [
        (
            "offering_uniq",
            "unique(lane_id, service_level_id, temperature_mode, shipment_type)",
            "This exact lane/service-level/temperature/shipment-type offering already exists.",
        ),
    ]

    @api.depends("lane_id.name", "service_level_id.name", "temperature_mode", "shipment_type")
    def _compute_name(self):
        for rec in self:
            temp = dict(TEMPERATURE_MODE_SELECTION).get(rec.temperature_mode, "")
            rec.name = f"{rec.lane_id.name or '?'} {rec.service_level_id.name or '?'} ({temp})"

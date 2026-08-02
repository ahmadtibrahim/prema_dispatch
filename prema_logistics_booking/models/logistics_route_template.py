"""Route Template — DEPRECATED. All fields migrated to logistics.corridor.

This model is kept for backwards compatibility only. New code should use
logistics.corridor directly. The phase, truck_slot, weekday, start_time,
overnight, conditional, min_departure_revenue, equipment_profile_id, and
temperature_capability fields now live on logistics.corridor.

Route templates that were used to generate route.run instances should be
migrated to corridor + corridor.departure records instead.
"""
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

WEEKDAY_SELECTION = [
    ("0","Monday"),("1","Tuesday"),("2","Wednesday"),("3","Thursday"),("4","Friday"),
    ("5","Saturday"),("6","Sunday"),
]


class LogisticsRouteTemplate(models.Model):
    _name = "logistics.route.template"
    _description = "Route Template [DEPRECATED — use logistics.corridor]"
    _order = "phase, truck_slot, weekday"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)

    phase = fields.Integer(string="Network Phase", required=True, default=1,
                           help="Phase 1-4. Template activates when fleet reaches this phase.")
    truck_slot = fields.Integer(string="Truck Slot", default=1,
                                help="Which truck in the fleet this template belongs to (1-4).")
    weekday = fields.Selection(WEEKDAY_SELECTION, required=True, string="Operating Day")

    corridor_name = fields.Char(string="Corridor", help="e.g. Southwest Feeder, Eastbound Quebec")
    direction = fields.Selection([
        ("eastbound","Eastbound"),("westbound","Westbound"),
        ("bidirectional","Bidirectional"),("loop","Loop"),
    ], default="bidirectional")

    start_hub_id = fields.Many2one("logistics.region", string="Start Hub", index=True)
    end_hub_id = fields.Many2one("logistics.region", string="End Hub")
    via_hub_id = fields.Many2one("logistics.region", string="Via Hub")

    stop_regions = fields.Text(string="Ordered Stop Regions",
                                help="Comma-separated region codes in route order, e.g. R1,R8,R10,R11,R13")

    start_time = fields.Float(string="Start Time", default=7.0, help="24h float, e.g. 7.0 = 7:00 AM")
    overnight = fields.Boolean(string="Overnight", help="Driver rests overnight before return.")
    conditional = fields.Boolean(string="Conditional",
                                  help="Only dispatched if minimum revenue or bookings met.")
    min_departure_revenue = fields.Float(string="Min Departure Revenue", default=0.0,
                                          help="If conditional, the minimum booked revenue to dispatch.")

    equipment_profile_id = fields.Many2one(
        "logistics.equipment.profile", string="Equipment Requirement",
        domain="[('is_requirement_class','=',True)]",
    )
    temperature_capability = fields.Selection([
        ("dry","Dry Only"),("chilled","Dry + Chilled"),("all","Dry + Chilled + Frozen"),
    ], default="all")

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    _sql_constraints = [
        ("phase_truck_weekday_uniq",
         "unique(phase, truck_slot, weekday)",
         "A template already exists for this phase/truck/weekday combination."),
    ]

    @api.model
    def create(self, vals):
        _logger.warning(
            "logistics.route.template is DEPRECATED — use logistics.corridor instead. "
            "Creating template '%s'", vals.get('name', 'unnamed')
        )
        return super().create(vals)

    def write(self, vals):
        _logger.warning(
            "logistics.route.template is DEPRECATED — use logistics.corridor instead. "
            "Updating template ids=%s", self.ids
        )
        return super().write(vals)

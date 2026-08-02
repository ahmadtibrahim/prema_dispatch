from odoo import api, fields, models

DELIVERY_OFFSET_SELECTION = [
    ("same_day", "Same Day"),
    ("next_day", "Next Day"),
    ("next_business_day", "Next Business Day"),
    ("scheduled_days", "Fixed Number of Days"),
]


class LogisticsLaneSchedule(models.Model):
    """Structure only for this phase — no production rows are seeded here.

    Business-approved pickup weekdays/cutoffs/transit offsets must come from
    the operations team before any row is created for a real lane. See
    CLAUDE.md 'Pending Schedule Rules'.
    """

    _name = "logistics.lane.schedule"
    _description = "Operational pickup/cutoff/delivery-offset rule for one service offering"
    _order = "service_offering_id"

    service_offering_id = fields.Many2one(
        "logistics.service.offering", required=True, index=True, ondelete="cascade"
    )
    active = fields.Boolean(default=True)

    # Pickup weekdays — 7 booleans, same convention Odoo itself uses for
    # weekly recurrence (e.g. calendar.recurrence's mo/tu/we/.../su fields).
    pickup_monday = fields.Boolean(string="Mon")
    pickup_tuesday = fields.Boolean(string="Tue")
    pickup_wednesday = fields.Boolean(string="Wed")
    pickup_thursday = fields.Boolean(string="Thu")
    pickup_friday = fields.Boolean(string="Fri")
    pickup_saturday = fields.Boolean(string="Sat")
    pickup_sunday = fields.Boolean(string="Sun")

    cutoff_time = fields.Float(help="24h float time, e.g. 14.5 = 2:30 PM.")
    pickup_window_start = fields.Float()
    pickup_window_end = fields.Float()

    delivery_offset_type = fields.Selection(DELIVERY_OFFSET_SELECTION, default="next_day", required=True)
    delivery_offset_days = fields.Integer(
        help="Used only when Delivery Offset Type = Fixed Number of Days."
    )
    delivery_window_start = fields.Float()
    delivery_window_end = fields.Float()

    # Holidays AND company blackout dates both live here as calendars — a
    # blackout is just a calendar named e.g. "Company Blackouts" with its own
    # lines, so there is one date-exclusion mechanism, not two.
    holiday_calendar_ids = fields.Many2many("logistics.holiday.calendar", string="Holiday/Blackout Calendars")

    effective_from = fields.Date()
    effective_to = fields.Date()

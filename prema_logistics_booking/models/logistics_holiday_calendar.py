from odoo import fields, models


class LogisticsHolidayCalendar(models.Model):
    _name = "logistics.holiday.calendar"
    _description = "Shared holiday/blackout-date calendar referenced by Corridors"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    line_ids = fields.One2many("logistics.holiday.calendar.line", "calendar_id", string="Dates")


class LogisticsHolidayCalendarLine(models.Model):
    _name = "logistics.holiday.calendar.line"
    _description = "A single excluded date (holiday or company blackout) on a calendar"
    _order = "date"

    calendar_id = fields.Many2one(
        "logistics.holiday.calendar", required=True, ondelete="cascade", index=True
    )
    date = fields.Date(required=True)
    description = fields.Char()

    _sql_constraints = [
        (
            "calendar_date_uniq",
            "unique(calendar_id, date)",
            "This date is already on this calendar.",
        ),
    ]

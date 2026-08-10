"""Saved Location Operating Hours — structured weekly schedule."""
from odoo import api, fields, models


class LogisticsSavedLocationHours(models.Model):
    _name = "logistics.saved.location.hours"
    _description = "Saved Location Operating Hours"
    _order = "saved_location_id, day_of_week, service_scope, sequence"

    saved_location_id = fields.Many2one(
        "logistics.saved.location", required=True, ondelete="cascade", index=True,
    )
    day_of_week = fields.Selection([
        ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
        ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
    ], required=True, string="Day")
    service_scope = fields.Selection([
        ("general", "General Hours"),
        ("pickup", "Pickup Hours"),
        ("delivery", "Receiving Hours"),
    ], default="general", required=True)
    status = fields.Selection([
        ("closed", "Closed"),
        ("open_24h", "Open 24 Hours"),
        ("custom", "Custom Hours"),
    ], required=True, default="open_24h", string="Status")
    sequence = fields.Integer(default=10)
    open_time = fields.Float(string="Open", help="Hours as float (e.g. 8.5 = 08:30)")
    close_time = fields.Float(string="Close", help="Hours as float (e.g. 17.0 = 17:00)")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("unique_day_scope_seq", "unique(saved_location_id, day_of_week, service_scope, sequence)",
         "Duplicate operating period for this day and scope."),
    ]

    def name_get(self):
        days = dict(self._fields["day_of_week"].selection)
        scopes = dict(self._fields["service_scope"].selection)
        result = []
        for r in self:
            day = days.get(r.day_of_week, "?")
            scope = scopes.get(r.service_scope, "")
            if r.status == "closed":
                detail = "Closed"
            elif r.status == "open_24h":
                detail = "24h"
            else:
                ot = f"{int(r.open_time or 0):02d}:{int((r.open_time or 0) % 1 * 60):02d}"
                ct = f"{int(r.close_time or 0):02d}:{int((r.close_time or 0) % 1 * 60):02d}"
                detail = f"{ot}–{ct}"
            result.append((r.id, f"{day} {scope} {detail}"))
        return result


class LogisticsSavedLocationException(models.Model):
    _name = "logistics.saved.location.exception"
    _description = "Saved Location Schedule Exception"
    _order = "date, id"

    saved_location_id = fields.Many2one(
        "logistics.saved.location", required=True, ondelete="cascade", index=True,
    )
    date = fields.Date(required=True, index=True)
    name = fields.Char(string="Label", required=True)
    exception_type = fields.Selection([
        ("closed", "Closed"),
        ("special_hours", "Special Hours"),
        ("open_24h", "Open 24 Hours"),
    ], required=True, default="closed")
    open_time = fields.Float(string="Open")
    close_time = fields.Float(string="Close")
    service_scope = fields.Selection([
        ("general", "General"), ("pickup", "Pickup"), ("delivery", "Receiving"),
    ], default="general")
    notes = fields.Text()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("unique_date_scope", "unique(saved_location_id, date, service_scope)",
         "An exception already exists for this date and scope."),
    ]

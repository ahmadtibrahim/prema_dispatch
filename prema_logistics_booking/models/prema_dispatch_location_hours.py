"""Canonical Facility Operating Hours — structured weekly schedule.

The facility master (prema.dispatch.location) is the physical authority;
its structured operating hours live here, never on a customer copy.
`logistics.saved.location.hours` remains the legacy twin (historical rows
preserved, no hard deletes); the canonical model is the scheduling
authority for new data and the migration target.
"""

from odoo import api, fields, models


def hours_summary_from_rows(rows):
    """Compact weekly-hours label from structured general-scope rows.

    Same algorithm as the legacy saved-location summary so the two
    surfaces read identically: 'Open 24h' / 'Mon–Fri 07:00–17:00 · Sat–Sun
    Closed' / 'Closed' / 'See hours' / 'Hours not set'.
    """
    if not rows:
        return "Hours not set"
    by_day = {}
    for row in rows:
        by_day.setdefault(row.day_of_week, row)
    if len(by_day) == 7:
        statuses = {r.status for r in by_day.values()}
        if statuses == {"open_24h"}:
            return "Open 24h"
        if statuses == {"closed"}:
            return "Closed"

    def _label(row):
        if row.status == "open_24h":
            return "24h"
        if row.status == "closed":
            return "Closed"
        return "%s–%s" % (
            _float_to_hhmm(row.open_time or 0.0),
            _float_to_hhmm(row.close_time or 24.0),
        )

    weekday_rows = [by_day.get(str(d)) for d in range(5) if str(d) in by_day]
    weekend_rows = [by_day.get(str(d)) for d in (5, 6) if str(d) in by_day]
    parts = []
    if len(weekday_rows) == 5 and len({_label(r) for r in weekday_rows}) == 1:
        parts.append("Mon–Fri %s" % _label(weekday_rows[0]))
    if len(weekend_rows) == 2 and len({_label(r) for r in weekend_rows}) == 1:
        parts.append("Sat–Sun %s" % _label(weekend_rows[0]))
    if len(parts) == 2:
        return " · ".join(parts)
    if len(parts) == 1:
        return parts[0]
    return "See hours"


def _float_to_hhmm(value):
    value = float(value or 0.0) % 24.0
    return "%02d:%02d" % (int(value), int(round((value % 1) * 60)) % 60)


class PremaDispatchLocationHours(models.Model):
    _name = "prema.dispatch.location.hours"
    _description = "Facility Operating Hours"
    _order = "facility_id, day_of_week, service_scope, sequence"

    facility_id = fields.Many2one(
        "prema.dispatch.location", string="Facility",
        required=True, ondelete="cascade", index=True,
    )
    day_of_week = fields.Selection([
        ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
        ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
    ], required=True, string="Day")
    service_scope = fields.Selection([
        ("general", "General Hours"),
        ("pickup", "Pickup Hours"),
        ("receiving", "Receiving Hours"),
        ("shipping", "Shipping Hours"),
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
        ("unique_day_scope_seq",
         "unique(facility_id, day_of_week, service_scope, sequence)",
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
                ot = _float_to_hhmm(r.open_time or 0.0)
                ct = _float_to_hhmm(r.close_time or 24.0)
                detail = f"{ot}–{ct}"
            result.append((r.id, f"{day} {scope} {detail}"))
        return result

    @api.model
    def summary(self, rows):
        """Public alias so callers can compute a label from a recordset."""
        return hours_summary_from_rows(rows)

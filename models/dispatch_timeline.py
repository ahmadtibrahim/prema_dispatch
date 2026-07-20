from odoo import fields, models

TIMELINE_EVENTS = [
    ("booking_created",    "Booking Created"),
    ("imported_from",      "Imported from Quote / Invoice / Template"),
    ("stage_changed",      "Stage Changed"),
    ("truck_assigned",     "Truck Assigned"),
    ("dispatch_confirmed", "Dispatch Confirmed"),
    ("arrived_stop",       "Arrived at Stop"),
    ("stop_completed",     "Stop Completed"),
    ("picked_up",          "Picked Up"),
    ("delivered",          "Delivered"),
    ("all_stops_done",     "All Stops Completed"),
    ("pod_uploaded",       "POD Uploaded"),
    ("invoice_completed",  "Invoice Confirmed"),
    ("job_reopened",       "Job Reopened"),
    ("exception",          "Exception / Issue"),
    ("note",               "Note"),
]


class PremaDispatchTimelineEvent(models.Model):
    _name = "prema.dispatch.timeline.event"
    _description = "Dispatch Job Timeline Event"
    _order = "occurred_at asc, id asc"

    job_id = fields.Many2one(
        "prema.dispatch.job", required=True, ondelete="cascade", index=True,
    )
    event_type = fields.Selection(TIMELINE_EVENTS, required=True)
    occurred_at = fields.Datetime(
        required=True, default=fields.Datetime.now, readonly=True,
    )
    user_id = fields.Many2one(
        "res.users", string="By", readonly=True,
        default=lambda self: self.env.user,
    )
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", ondelete="set null",
    )
    notes = fields.Char(string="Detail")
    event_type_label = fields.Char(
        compute="_compute_label", string="Event",
    )

    def _compute_label(self):
        labels = dict(TIMELINE_EVENTS)
        for rec in self:
            rec.event_type_label = labels.get(rec.event_type, rec.event_type)

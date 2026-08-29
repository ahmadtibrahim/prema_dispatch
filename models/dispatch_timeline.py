from odoo import fields, models

TIMELINE_EVENTS = [
    ("booking_created",    "Booking Created"),
    ("imported_from",      "Imported from Quote / Invoice / Template"),
    ("stage_changed",      "Stage Changed"),
    ("truck_assigned",     "Truck Assigned"),
    ("dispatch_confirmed", "Dispatch Confirmed"),
    ("route_started",      "Route Started"),
    ("arrived_stop",       "Arrived at Stop"),
    ("stop_completed",     "Stop Completed"),
    ("picked_up",          "Picked Up"),
    ("delivered",          "Delivered"),
    ("all_stops_done",     "All Stops Completed"),
    ("stop_skipped",       "Stop Skipped"),
    ("issue_reported",     "Issue Reported"),
    ("pickup_confirmed",   "Pickup Confirmed"),
    ("evidence_uploaded",  "Evidence Uploaded"),
    ("pod_uploaded",       "POD Uploaded"),
    ("popp_captured",      "POPP Captured"),
    ("popp_override",      "No Access Override"),
    ("document_scanned",   "Document Scanned"),
    ("pallet_assigned",    "Pallet Assigned"),
    ("day_ended",          "Day Ended"),
    ("invoice_completed",  "Invoice Confirmed"),
    ("job_reopened",       "Job Reopened"),
    ("exception",          "Exception / Issue"),
    ("note",               "Note"),
    ("temperature",        "Reefer / Temperature"),
    ("temperature_conflict", "Temperature Conflict"),
    ("temperature_override", "Temperature Override"),
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
    # ── §10 timeline identifiers: every event names the physical visit,
    # pallet/item and evidence it refers to — a shared physical visit
    # must never combine evidence into an ambiguous shared bucket.
    visit_id = fields.Many2one(
        "prema.dispatch.route.visit", string="Physical Visit",
        ondelete="set null", index=True,
    )
    pallet_id = fields.Many2one(
        "prema.dispatch.item", string="Pallet / Item",
        ondelete="set null", index=True,
    )
    evidence_id = fields.Many2one(
        "prema.dispatch.evidence", string="Evidence",
        ondelete="set null", index=True,
    )
    customer_id = fields.Many2one(
        "res.partner", string="Customer",
        related="job_id.partner_id", readonly=True,
    )
    # booking_id is intentionally NOT here: its source (job_id.
    # logistics_booking_id) is defined by prema_logistics_booking, which
    # loads AFTER prema_dispatch — a related or computed definition here
    # would fail setup or recompute before the extension field exists.
    # prema_logistics_booking adds it via models/dispatch_timeline_extension.py.
    notes = fields.Char(string="Detail")
    event_type_label = fields.Char(
        compute="_compute_label", string="Event",
    )

    def _compute_label(self):
        labels = dict(TIMELINE_EVENTS)
        for rec in self:
            rec.event_type_label = labels.get(rec.event_type, rec.event_type)

"""Milk-run bridge fields on the dispatch stop + §15 stale-trigger hooks.

These Many2one bridges point UP at logistics.booking.stop, which lives in
prema_logistics_booking (this module) while prema.dispatch.stop lives in
prema_dispatch (a dependency). A field with an upward comodel defined
directly in prema_dispatch breaks the registry when prema_dispatch is
upgraded alone — the comodel is not in the pool yet at field-setup time
and Odoo degrades the field to `_unknown`. Defining it here, via
_inherit, keeps the same proven pattern as dispatch_job_extension.py.

The write/create overrides below power the "safe replanning" rule of the
day-trip optimizer (§15, D-B4): any change to a stop that an OPEN
proposal covers (window, load, status, sequence, timing type, …) marks
that proposal stale, so a proposal is never silently re-applied over a
changed day. Apply itself flips its own state to applied before touching
stops, so only OTHER open proposals stale — no context flag needed.
"""
from odoo import fields, models


class PremaDispatchStop(models.Model):
    _inherit = "prema.dispatch.stop"

    logistics_booking_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Booking Stop",
        ondelete="set null", index=True,
        help="Stable bridge to the commercial booking stop (idempotency, "
             "sync, audit).")

    def write(self, vals):
        result = super().write(vals)
        # Only changes that actually reshape the day plan invalidate open
        # proposals (ETA advisory/actual-* writes never do).
        trigger = set(vals) & {
            "sequence", "status", "stop_type", "scheduled_time",
            "time_window_type", "earliest_time", "latest_time", "exact_time",
            "deadline_time", "hard_deadline", "appointment_confirmed",
            "facility_open_time", "facility_close_time",
            "service_time_minutes", "pallets_in", "pallets_out",
            "weight_in_lbs", "weight_out_lbs", "route_locked",
            "planning_only",
        }
        if trigger and not self.env.context.get("_day_route_silent"):
            # @api.model helpers must be invoked through a recordset —
            # class-level calls bypass the binder and hand the raw env
            # in as ``self`` (AttributeError on self.env.user.id).
            self.env["prema.dispatch.day.route.proposal"] \
                ._mark_stale_for_stops(
                    self.ids,
                    "A stop of this day changed (%s)."
                    % ", ".join(sorted(trigger)))
        return result

    def create(self, vals_list):
        records = super().create(vals_list)
        # A stop ADDED to a covered job changes the day's load/scope.
        job_ids = list({r.job_id.id for r in records if r.job_id})
        if job_ids and not self.env.context.get("_day_route_silent"):
            self.env["prema.dispatch.day.route.proposal"] \
                ._mark_stale_for_jobs(
                    job_ids,
                    "A stop was added to one of this day's jobs.")
        return records

    def unlink(self):
        # A stop REMOVED from a covered day changes the day's scope. Run
        # BEFORE the delete — the proposal lines still point at the stops.
        if not self.env.context.get("_day_route_silent"):
            self.env["prema.dispatch.day.route.proposal"] \
                ._mark_stale_for_stops(
                    self.ids, "A stop was removed from this day.")
        return super().unlink()


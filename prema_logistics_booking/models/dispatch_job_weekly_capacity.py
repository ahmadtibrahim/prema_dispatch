"""Weekly Capacity board — server RPC layer (TODO 9-13 of the work
order). Inherits prema.dispatch.job with NO new fields; everything the
board shows comes from the canonical models, read through
WeeklyCapacityService.

RPCs (all @api.model, no recordset needed):
  • weekly_capacity_board_data(week_start, truck_id)     — week payload
    (7 day columns × trucks, real job cards, per-day capacity cells,
    holidays, unassigned cards). One call per week — the board keeps the
    payload in memory and renders locally on nav/drop/refresh.
  • weekly_capacity_evaluate_load(payload)               — the TODO 12
    availability tool: per truck/day AVAILABLE / AVAILABLE WITH WARNING
    / UNAVAILABLE for a proposed load. Capacity answers only — pricing
    and creation stay on the canonical phone-booking / custom-quote
    flows, which the board OPENS, never auto-confirms.
  • weekly_capacity_move_job(job_id, date, time, truck_id) — date/time
    move (drop on another day, time adjust). Mirrors the Dispatch
    Planner's own mutation semantics:
      - refuses corridor/departure-controlled jobs (departure_controlled,
        the same key the extension's assign/unassign guards use),
      - refuses jobs whose execution started (started stops),
      - validates the target truck/day with the SAME conflict domains as
        the extension's assign guard (active scheduled departure on the
        truck+date, another auto_scheduled_ltl job on truck+date) →
        truck_day_blocked,
      - shifts the real stops' scheduled_time by the day delta (their
        ETA recompute triggers run exactly as for any stop write), then
        writes scheduled_pickup through job.write so the canonical
        dispatch_job_extension.write auto-syncs operation_date and marks
        day-route proposals stale.
    The response carries {updated_job_id, calendar_week_delta} and the
    board refreshes its single week payload locally — one server
    notification per action (the RPC response IS the notification); no
    new bus infrastructure.

Assignment / unassignment are NOT re-implemented here: the board calls
the canonical assign_job_to_truck / unassign_truck (with the planner's
feasibility_blocked/can_override confirm flow) exactly like
dispatch_board.js does. The time helpers below only convert between
naive-UTC Odoo datetimes and business-local clocks (same zone rules as
WeeklyCapacityService._tz_name).
"""
import logging
from datetime import date as _date
from datetime import datetime, time as _time, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

try:
    from ..services.weekly_capacity_service import WeeklyCapacityService
except Exception:  # pragma: no cover — import-time guard
    WeeklyCapacityService = None


class DispatchJobWeeklyCapacity(models.Model):
    _inherit = "prema.dispatch.job"

    # ── Board data + evaluate-load (read-only) ───────────────────────

    @api.model
    def weekly_capacity_board_data(self, week_start=None, truck_id=None):
        """Single weekly data RPC — see WeeklyCapacityService.get_week_board."""
        if WeeklyCapacityService is None:  # pragma: no cover
            return {"error": "WeeklyCapacityService unavailable."}
        service = WeeklyCapacityService(self.env)
        try:
            return service.get_week_board(week_start, truck_id)
        except Exception:
            _logger.exception("weekly capacity board data failed")
            return {"error": "Could not build the week payload. "
                             "See the server log."}

    @api.model
    def weekly_capacity_evaluate_load(self, payload):
        """TODO 12 availability tool — see WeeklyCapacityService.evaluate_load."""
        if WeeklyCapacityService is None:  # pragma: no cover
            return {"error": "WeeklyCapacityService unavailable."}
        service = WeeklyCapacityService(self.env)
        try:
            return service.evaluate_load(payload or {})
        except Exception:
            _logger.exception("weekly capacity evaluate failed")
            return {"error": "Could not evaluate the load. "
                             "See the server log."}

    # ── Job date/time move (TODO 13) ─────────────────────────────────

    @api.model
    def weekly_capacity_move_job(self, job_id, date_str, time_str=None,
                                 truck_id=None):
        """Date/time move with the planner's guard semantics.

        Returns {"success": True, "job_id", "updated_pickup" (iso),
        "operation_date", "updated_job_id", "calendar_week_delta"} or
        {"success": False, "<key>": True, "error": ...} — keys are the
        planner/extension ones (departure_controlled, truck_day_blocked,
        stage_locked, execution_started)."""
        if WeeklyCapacityService is None:  # pragma: no cover
            return {"success": False, "error": "Service unavailable."}
        service = WeeklyCapacityService(self.env)
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid job id."}
        job = self.browse(job_id).sudo()
        if not job.exists():
            return {"success": False, "error": "Job not found.",
                    "job_not_found": True}
        stage = job.stage_id
        if stage and (stage.is_cancelled or stage.is_completed):
            return {"success": False,
                    "stage_locked": True,
                    "error": "Job %s is %s — archived jobs cannot be "
                             "moved." % (job.name, stage.name or "closed")}
        if job.corridor_departure_id:
            return {
                "success": False,
                "departure_controlled": True,
                "error": ("This LTL load belongs to %s — move it on the "
                          "departure, not the board."
                          % job.corridor_departure_id.display_name),
            }
        started = job.stop_ids.filtered(
            lambda s: s.status in ("completed", "arrived", "en_route")
            or s.actual_arrival_time or s.actual_departure_time)
        if started:
            return {"success": False,
                    "execution_started": True,
                    "error": "Job %s has already started (stop %s is %s) — "
                             "running jobs cannot be moved."
                             % (job.name, started[0].sequence,
                                started[0].status)}

        try:
            new_date = _date.fromisoformat(date_str) if isinstance(
                date_str, str) else fields.Date.to_date(date_str)
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid date."}
        if not new_date:
            return {"success": False, "error": "Invalid date."}

        tz_name = service._tz_name()

        # Target truck: current vehicle unless the caller names another.
        target = job.vehicle_id
        if truck_id:
            try:
                truck_id = int(truck_id)
            except (TypeError, ValueError):
                return {"success": False, "error": "Invalid truck id."}
            Truck = self.env["fleet.vehicle"]
            target = Truck.browse(truck_id).sudo()
            if not target.exists():
                return {"success": False, "error": "Truck not found."}

        if target and (not job.vehicle_id or target.id != job.vehicle_id.id):
            # Truck reassignment rides the canonical assign/unassign
            # guards (feasibility, departures, reservations) — never a
            # side effect of a date move.
            return {
                "success": False,
                "truck_change": True,
                "error": ("Job %s sits on another truck — drag it to the "
                          "unassigned zone first, then drop it on the "
                          "target day." % job.name),
            }

        # Conflict scan on the target truck/day — the SAME domains as the
        # extension's assign guard (dispatch_job_extension.assign_job_
        # to_truck): an active scheduled departure occupies truck+date;
        # another auto_scheduled_ltl operation reserves truck+date.
        if target:
            conflict = self.env["logistics.corridor.departure"].sudo() \
                .search([
                    ("vehicle_id", "=", target.id),
                    ("departure_date", "=", new_date),
                    ("active", "=", True),
                    ("status", "not in", ("cancelled", "completed")),
                ], limit=1)
            if conflict:
                return {
                    "success": False,
                    "truck_day_blocked": True,
                    "error": ("This truck is booked for %s on %s. Add "
                              "freight to that LTL departure or choose "
                              "another day."
                              % (conflict.corridor_id.display_name,
                                 new_date)),
                }
            ltl_operation = self.sudo().search([
                ("id", "!=", job.id),
                ("vehicle_id", "=", target.id),
                ("operation_date", "=", new_date),
                ("auto_scheduled_ltl", "=", True),
                ("stage_id.stage_type", "not in",
                 ("cancelled", "completed")),
            ], limit=1)
            if ltl_operation:
                return {
                    "success": False,
                    "truck_day_blocked": True,
                    "error": ("This truck is reserved for LTL operation "
                              "%s on %s. Choose another day."
                              % (ltl_operation.display_name, new_date)),
                }

        stops = job.stop_ids.filtered(
            lambda s: not s.planning_only and s.status != "cancelled"
        ).sorted("sequence")
        anchor = next((s for s in stops if s.scheduled_time), False)
        old_pickup = job.scheduled_pickup
        old_op_date = job.operation_date or (
            fields.Date.to_date(old_pickup) if old_pickup else False)
        old_date = old_op_date or (
            service._local_date_of(tz_name, old_pickup)
            if old_pickup else
            (service._local_date_of(anchor.tz_name or tz_name,
                                    anchor.scheduled_time)
             if anchor else False))
        if not old_date:
            return {"success": False,
                    "error": "Job %s has no operation date to move."
                             % job.name}

        # Clock resolution: explicit time_str wins; else the pickup's own
        # business-local clock; else the first timed stop's local clock;
        # else 06:00 local (the planner's working default).
        minutes = self._parse_clock(time_str)
        if minutes is None:
            if old_pickup:
                local = service._as_local(tz_name, old_pickup)
                minutes = local.hour * 60 + local.minute
            elif anchor:
                local = service._as_local(
                    anchor.tz_name or tz_name, anchor.scheduled_time)
                minutes = local.hour * 60 + local.minute
            else:
                minutes = 6 * 60

        delta_days = (new_date - old_date).days
        if delta_days == 0 and self._parse_clock(time_str) is None:
            return {"success": True,
                    "job_id": job.id,
                    "updated_job_id": job.id,
                    "updated_pickup": service._iso(old_pickup),
                    "operation_date": str(old_op_date) if old_op_date
                    else False,
                    "calendar_week_delta": 0}

        # Day shift: every stop keeps its relative spacing; the anchor
        # stop additionally snaps to the requested clock (delta_minutes
        # applies only to the anchor).
        delta_minutes = 0
        if self._parse_clock(time_str) is not None and anchor:
            anchor_local = service._as_local(
                anchor.tz_name or tz_name, anchor.scheduled_time)
            delta_minutes = minutes - (
                anchor_local.hour * 60 + anchor_local.minute)
        for s in stops:
            if not s.scheduled_time:
                continue
            shift = delta_days * 1440 + (
                delta_minutes if s.id == anchor.id else 0)
            if shift:
                s.write({"scheduled_time": s.scheduled_time
                         + timedelta(minutes=shift)})

        # New pickup datetime = new local date + resolved clock (naive-UTC).
        new_pickup = self._local_clock_to_utc(service, tz_name, new_date,
                                              minutes)
        # Canonical write — the extension auto-syncs operation_date and
        # marks day-route proposals stale.
        job.write({"scheduled_pickup": new_pickup})
        op_date = job.operation_date or fields.Date.to_date(new_pickup)
        return {
            "success": True,
            "job_id": job.id,
            "updated_job_id": job.id,
            "updated_pickup": service._iso(new_pickup),
            "operation_date": str(op_date) if op_date else False,
            "calendar_week_delta": delta_days,
        }

    # ── Local helpers ────────────────────────────────────────────────

    @staticmethod
    def _parse_clock(time_str):
        """'HH:MM' (business-local clock) → minutes past midnight, or None."""
        if not time_str:
            return None
        try:
            hour, minute = (int(part) for part in str(time_str).split(":", 1))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            return hour * 60 + minute
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _local_clock_to_utc(service, tz_name, local_date, minutes):
        """naive-UTC datetime for a local date + local clock minutes."""
        zone = service._zone(tz_name)
        local = datetime.combine(
            local_date, _time(minutes // 60, minutes % 60))
        if hasattr(zone, "localize"):
            aware = zone.localize(local, is_dst=None)
            if aware is None:  # pragma: no cover — ambiguous/gap hour
                aware = zone.localize(local)
        else:
            aware = local.replace(tzinfo=zone)
        return aware.astimezone(service._zone("UTC")).replace(tzinfo=None)

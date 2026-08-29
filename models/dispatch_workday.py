# -*- coding: utf-8 -*-
"""Driver workday tracking — START WORK / END DAY / persisted daily summary.

Phase 2 of the full-correction pass. One record per (driver, work_date):
the day-level counterpart to the per-job ``route_started_at`` handshake.

- ``action_start_work`` records when the driver actually starts the day
  (timestamp + GPS) and syncs every still-open job of that day so the
  Booking Board shows the driver has begun (per-job routes that were
  already explicitly started keep their original timestamp).
- ``action_end_day`` validates the day (no open stops, no unresolved
  issues, transfers done, required proof attached), records
  ``work_finished_at``, persists the daily summary metrics (stops,
  pallets, drive/wait/load/unload minutes — computed server-side from
  the actual arrival/departure timestamps and pins), and auto-completes
  any jobs whose stops are all done.

The summary metrics are the persisted source of truth for the Driver
App's DAILY SUMMARY card, and feed Phase 6's stop-timing learning.
"""
import logging
import math

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DriverWorkday(models.Model):
    _name = "prema.dispatch.driver.workday"
    _description = "Driver Workday"
    _order = "work_date desc, id desc"

    driver_id = fields.Many2one(
        "res.partner", string="Driver", required=True, ondelete="cascade",
        index=True,
    )
    work_date = fields.Date(string="Work Date", required=True, index=True)
    state = fields.Selection([
        ("not_started", "Not Started"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
    ], string="Day State", default="not_started", required=True)

    work_started_at = fields.Datetime(string="Work Started At")
    work_started_by = fields.Many2one("res.users", string="Work Started By")
    start_gps_lat = fields.Float(string="Start GPS Latitude", digits=(9, 6))
    start_gps_lng = fields.Float(string="Start GPS Longitude", digits=(9, 6))
    work_finished_at = fields.Datetime(string="Work Finished At")
    work_finished_by = fields.Many2one("res.users", string="Work Finished By")

    # ── Daily summary (persisted at END DAY) ─────────────────────
    stops_count = fields.Integer(string="Stops Completed")
    pickup_count = fields.Integer(string="Pickups")
    delivery_count = fields.Integer(string="Deliveries")
    pallets_handled = fields.Integer(string="Pallets Handled")
    distance_km = fields.Float(string="Distance (km)")
    total_minutes = fields.Integer(string="Total Minutes")
    driving_minutes = fields.Integer(string="Driving Minutes")
    waiting_minutes = fields.Integer(string="Waiting Minutes")
    loading_minutes = fields.Integer(string="Loading Minutes")
    unloading_minutes = fields.Integer(string="Unloading Minutes")

    _sql_constraints = [
        ("driver_date_uniq", "UNIQUE (driver_id, work_date)",
         "A driver can only have one workday record per date."),
    ]

    # ── Helpers ──────────────────────────────────────────────────

    def _driver_tz(self):
        self.ensure_one()
        import pytz
        user = self.env["res.users"].search(
            [("partner_id", "=", self.driver_id.id)], limit=1)
        return pytz.timezone(user.tz or "America/Toronto")

    @api.model
    def _get_or_create_for(self, driver_id, work_date):
        """Fetch (or lazily create) the workday for a driver on a date."""
        rec = self.search([("driver_id", "=", driver_id),
                           ("work_date", "=", work_date)], limit=1)
        if rec:
            return rec
        return self.create({"driver_id": driver_id, "work_date": work_date})

    def _day_stops(self):
        """All of this driver's stops on ``work_date`` — same selection rule
        the Driver App uses (stop.scheduled_time, else job.scheduled_pickup),
        so the day view and the app agree on what belongs to the day."""
        self.ensure_one()
        from datetime import date, datetime, timedelta
        import pytz

        Job = self.env["prema.dispatch.job"]
        user_tz = self._driver_tz()
        check_d = self.work_date
        utc_start = user_tz.localize(datetime.combine(
            check_d - timedelta(days=2), datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
        utc_end = user_tz.localize(datetime.combine(
            check_d + timedelta(days=2), datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)

        jobs = Job.search([
            ("driver_id", "=", self.driver_id.id),
            ("stage_id.is_cancelled", "=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start),
            ("scheduled_pickup", "<=", utc_end),
        ])

        stop_ids = []
        for s in Job.combined_vehicle_day_stops(jobs, check_d):
            sd = Job._stop_local_date(s, s.job_id, user_tz)
            if sd == check_d:
                stop_ids.append(s.id)
        # Return a real recordset — action_end_day / _compute_summary_metrics
        # use filtered()/sorted() (recordset ops, not list ops).
        return jobs, self.env["prema.dispatch.stop"].browse(stop_ids)

    # ── START ROUTE (day-level, spec §8) ─────────────────────────

    def action_start_work(self, lat=None, lng=None):
        """Record the driver starting the day's route (timestamp + GPS).

        The driver's "Start Route" tap on Home. The day-level workday
        record (unique per driver+date) is the single authority: it stamps
        work_started_at + GPS once, idempotently, and syncs EVERY open job
        of the day (route_started_at + tracking timeline entry, if not
        already set) so the Booking Board and the customer tracking page
        show the route as started. There is no separate per-job "Start
        Route" entry point in the app — starting the route starts the day.
        """
        self.ensure_one()
        if self.state != "completed":
            if not self.work_started_at:
                self.write({
                    "work_started_at": fields.Datetime.now(),
                    "work_started_by": self.env.user.id,
                    "start_gps_lat": float(lat or 0),
                    "start_gps_lng": float(lng or 0),
                    "state": "in_progress",
                })
            jobs, _stops = self._day_stops()
            for job in jobs.filtered(
                lambda j: not j.stage_id.is_completed and not j.stage_id.is_cancelled
                and not j.route_started_at
            ):
                job.write({
                    "route_started_at": self.work_started_at,
                    "route_started_by": self.env.user.id,
                })
                # Tracking timeline: the tracking page is the service-day
                # ETA authority — post the same route_started event the
                # (removed) per-job endpoint used to post.
                job._post_timeline(
                    job, "route_started",
                    notes=f"Route started by {self.env.user.name}.")
            # §9 feed: day-level start, anchored to the driver's first
            # active job of the day (the feed rows always name a job).
            active = jobs.filtered(
                lambda j: not j.stage_id.is_completed
                and not j.stage_id.is_cancelled)
            if active:
                active[0]._emit_feed(
                    "workday_start", driver=self.driver_id,
                    message=f"Driver started work — {self.work_started_at and fields.Datetime.context_timestamp(self, self.work_started_at).strftime('%I:%M %p') or ''}")
        return self._payload()

    def action_end_day(self):
        """Validate and close the workday, persist the daily summary.

        Validation (mirrors the Driver App spec): every assigned stop must
        be completed (or explicitly skipped), no stop may be left in the
        ``issue`` state, transfers must be finished, and stops whose
        commercial booking requires proof must carry it. Returns a
        ``{success: True, workday: payload}`` dict on success or a
        ``{success: False, error: ...}`` dict with the first blocker.
        """
        self.ensure_one()
        # Idempotent, same contract as action_start_work: re-running END DAY
        # returns the stored payload without shifting the recorded finish
        # time or recomputing (the persisted summary is the source of truth).
        if self.state == "completed":
            return {"success": True, "workday": self._payload()}

        jobs, stops = self._day_stops()

        # 1. No open stops (pending/en_route/arrived/issue).
        open_stops = stops.filtered(
            lambda s: not s.planning_only and s.status not in ("completed", "skipped", "cancelled"))
        if open_stops:
            first = open_stops[0]
            return {"success": False, "error": (
                "Cannot end the day — stop still open: %s (%s)."
                % (first.address or first.stop_type, first.status))}

        # 2. Unresolved issues acknowledged.
        issue_stops = stops.filtered(lambda s: s.status == "issue")
        if issue_stops:
            first = issue_stops[0]
            return {"success": False, "error": (
                "Cannot end the day — unresolved issue at %s. Resolve or "
                "acknowledge it first." % (first.address or first.stop_type))}

        # 3. Transfers completed.
        pending_transfer = stops.filtered(
            lambda s: s.status == "completed" and s.stop_type == "cross_dock_drop"
            and s.transfer_to_vehicle_id)
        if pending_transfer:
            first = pending_transfer[0]
            return {"success": False, "error": (
                "Cannot end the day — transfer at %s not completed."
                % (first.address or first.stop_type))}

        # 4. Mandatory proof (POP/POD) on completed stops.
        for s in stops.filtered(lambda s: s.status == "completed"):
            try:
                s._check_required_proof()
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        metrics = self._compute_summary_metrics(stops)
        if not self.work_started_at:
            metrics["work_started_at"] = fields.Datetime.now()

        self.write({
            "state": "completed",
            "work_finished_at": fields.Datetime.now(),
            "work_finished_by": self.env.user.id,
            "stops_count": metrics["stops_count"],
            "pickup_count": metrics["pickup_count"],
            "delivery_count": metrics["delivery_count"],
            "pallets_handled": metrics["pallets_handled"],
            "distance_km": metrics["distance_km"],
            "total_minutes": metrics["total_minutes"],
            "driving_minutes": metrics["driving_minutes"],
            "waiting_minutes": metrics["waiting_minutes"],
            "loading_minutes": metrics["loading_minutes"],
            "unloading_minutes": metrics["unloading_minutes"],
        })
        if not self.work_started_at:
            self.work_started_at = self.work_finished_at

        # 5. Auto-complete every job whose stops are all done.
        for job in jobs:
            if not job.stage_id.is_completed and not job.stage_id.is_cancelled:
                try:
                    job._check_all_stops_done()
                except Exception:
                    _logger.exception("end_day: job %s auto-complete failed", job.id)
            # Spec §34: the day closing propagates to every job's timeline.
            job._post_timeline(
                job, "day_ended",
                notes=f"Workday ended by {self.env.user.name} — "
                      f"{metrics['stops_count']} stops, "
                      f"{metrics['pallets_handled']} pallets handled",
            )
        # §9 feed: day-level end, anchored to the driver's first job.
        if jobs:
            jobs[0]._emit_feed(
                "workday_ended", driver=self.driver_id,
                message=f"Workday ended by {self.env.user.name} — "
                        f"{metrics['stops_count']} stops, "
                        f"{metrics['pallets_handled']} pallets handled")
        return {"success": True, "workday": self._payload()}

    # ── Summary metrics ──────────────────────────────────────────

    def _compute_summary_metrics(self, stops):
        """Daily summary from actual stop timestamps (naive-UTC Datetimes
        converted through the driver's timezone) and stop pins.

        - dwell per stop = departure − arrival; the service-time portion
          counts as loading (pickup) / unloading (delivery); any excess
          counts as waiting.
        - driving = positive gaps between consecutive stops' departure →
          next arrival.
        - distance = haversine over the stop pins in route order.
        - pallets = actuals (or plan when no actuals were captured),
          counting pickup pallets_in and delivery pallets_out.
        """
        self.ensure_one()
        import pytz
        from datetime import datetime
        user_tz = self._driver_tz()

        def minutes(a, b):
            if not a or not b or b <= a:
                return 0
            return int(round((b - a).total_seconds() / 60.0))

        completed = stops.filtered(
            lambda s: not s.planning_only and s.status == "completed")
        completed = completed.sorted(
            key=lambda s: (s.actual_arrival_time or s.scheduled_time or
                           s.actual_departure_time or fields.Datetime.now()))

        pickup_count = delivery_count = pallets_handled = 0
        loading_min = unloading_min = waiting_min = 0
        prev_departure = None
        driving_min = 0
        distance_km = 0.0
        prev_lat = prev_lng = None

        for s in completed:
            arrival = s.actual_arrival_time
            departure = s.actual_departure_time
            if not arrival and departure:
                arrival = departure
            if not departure and arrival:
                departure = arrival

            if arrival and departure and departure >= arrival:
                dwell = minutes(arrival, departure)
                service = s.service_time_minutes or 0
                if s.stop_type == "pickup":
                    pickup_count += 1
                    handled = s.actual_pallets_in or s.pallets_in or 0
                    loading_min += min(dwell, service) if service else dwell
                    waiting_min += max(dwell - service, 0) if service else 0
                elif s.stop_type in ("dropoff", "return"):
                    delivery_count += 1
                    handled = s.actual_pallets_out or s.pallets_out or 0
                    unloading_min += min(dwell, service) if service else dwell
                    waiting_min += max(dwell - service, 0) if service else 0
                else:
                    handled = 0
                pallets_handled += handled

            # Driving leg: previous stop's departure → this stop's arrival.
            if prev_departure and arrival and arrival >= prev_departure:
                driving_min += minutes(prev_departure, arrival)
            if arrival and departure and departure >= arrival:
                prev_departure = departure

            # Distance from consecutive pins (haversine, km).
            lat = s.pin_lat if s.pin_set and s.pin_lat else s.latitude
            lng = s.pin_lng if s.pin_set and s.pin_lng else s.longitude
            if lat and lng:
                if prev_lat is not None and prev_lng is not None:
                    distance_km += self._haversine_km(prev_lat, prev_lng, lat, lng)
                prev_lat, prev_lng = lat, lng

        if self.work_started_at and self.work_finished_at:
            total_min = minutes(self.work_started_at, self.work_finished_at)
        else:
            total_min = driving_min + loading_min + unloading_min + waiting_min

        return {
            "stops_count": len(completed),
            "pickup_count": pickup_count,
            "delivery_count": delivery_count,
            "pallets_handled": pallets_handled,
            "distance_km": round(distance_km, 1),
            "total_minutes": total_min,
            "driving_minutes": driving_min,
            "waiting_minutes": waiting_min,
            "loading_minutes": loading_min,
            "unloading_minutes": unloading_min,
        }

    @staticmethod
    def _haversine_km(lat1, lng1, lat2, lng2):
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lng2 - lng1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    # ── Payload ──────────────────────────────────────────────────

    def _payload(self):
        self.ensure_one()
        return {
            "driver_id": self.driver_id.id,
            "date": self.work_date.isoformat(),
            "state": self.state,
            "work_started_at": self._dt_iso_utc(self.work_started_at),
            "work_started_by": self.work_started_by.name or "",
            "work_finished_at": self._dt_iso_utc(self.work_finished_at),
            "work_finished_by": self.work_finished_by.name or "",
            "summary": {
                "stops": self.stops_count,
                "pickups": self.pickup_count,
                "deliveries": self.delivery_count,
                "pallets": self.pallets_handled,
                "distance_km": self.distance_km,
                "total_minutes": self.total_minutes,
                "driving_minutes": self.driving_minutes,
                "waiting_minutes": self.waiting_minutes,
                "loading_minutes": self.loading_minutes,
                "unloading_minutes": self.unloading_minutes,
            },
        }

    @staticmethod
    def _dt_iso_utc(dt):
        """Same convention as dispatch_job._dt_iso_utc — naive Odoo Datetime
        fields are UTC; appending 'Z' makes the frontend parse them as UTC."""
        return (dt.isoformat() + "Z") if dt else ""

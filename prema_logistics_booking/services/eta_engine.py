"""EtaEngine — the ONE server-side ETA authority (work order Section C).

Replaces the four scattered estimate algorithms (route_service Google
walk, the dead live-route pipeline, ItineraryPlanner.recommend_route
write-on-demand, optimizer greedy) with a single deterministic walk:

  start profile → travel → facility-hours/window arrival plan → service →
  departure, propagated stop by stop.

Eight distinct time values per operational stop:
  travel_arrival_at         — physical arrival (before window waiting)
  facility_service_start_at — when service actually begins (window-adjusted)
  planned_departure_at      — service start + service minutes
  customer_eta_at           — the customer-facing promise (tracking page)
  actual_service_start      — set from actual arrival on arrival/completion
  eta_live                  — the operative forward ETA (board/driver)
  eta_delay_minutes         — ETA − scheduled time (positive = late)
  eta_source / eta_confidence — provenance + data-quality grade

Plus job-level recommended_driver_leave_home_at when the driver start
profile mode is "driver_home" (private home coords live on res.partner,
staff-only).

Contracts:
  • NEVER writes scheduled_time — that stays the schedule authority
    (finalizer / dispatcher / driver edit). This engine only ESTIMATES.
  • Completed/arrived stops anchor on their ACTUAL times; en_route stops
    HOLD their ETA and pass the delay forward; dispatcher eta_override
    wins whole for its stop.
  • Facility hours always take precedence over an early arrival: the
    truck waits at the door (arrival_plan). Unverified facility hours
    surface as "HOURS NOT VERIFIED" via hours_verified and drop
    eta_confidence to low.
  • Deterministic: Google drive times are used when present
    (drive_time_from_prev_minutes), straight-line ×1.4 @ 50 km/h
    otherwise — identical math everywhere.
"""
import logging
import math
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

# Fields the engine writes per stop. Excluded from the stop write() recalc
# guard so engine writes never re-trigger a recompute (no recursion).
ENGINE_FIELDS = (
    "travel_arrival_at", "facility_service_start_at", "planned_departure_at",
    "customer_eta_at", "actual_service_start", "eta_live",
    "eta_delay_minutes", "eta_source", "eta_confidence",
)

# Stop states whose writes must shift downstream ETAs (the recalc guard).
# eta_override included: a dispatcher override must re-render the ETA
# fields immediately (the engine's override branch then wins whole).
RECALC_TRIGGER_FIELDS = (
    "sequence", "status", "scheduled_time", "address", "latitude",
    "longitude", "pin_lat", "pin_lng", "saved_location_id",
    "service_time_minutes", "time_window_type", "earliest_time",
    "latest_time", "exact_time", "deadline_time", "operating_hours_snapshot",
    "eta_override",
)


class EtaEngine:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Public entry points ──────────────────────────────────────────

    def compute_job_eta(self, job, anchor=None, source=None):
        """Unified forward ETA walk for one job's confirmed stops.

        source: "scheduled" (pre-service planning), "live" (recomputed
        during execution), or None → derived from the stop states.
        Writes the eight ETA values onto every non-planning stop and the
        job-level recommended_driver_leave_home_at. Returns the ordered
        recordset."""
        job = job.sudo()
        stops = job.stop_ids.filtered(lambda s: not s.planning_only)
        if not stops:
            return stops
        ordered = stops.sorted("sequence")

        profile = self._start_profile(job, anchor, source)
        anchor_dt = profile["anchor"]
        position = profile["position"]
        leave_home = profile.get("leave_home_at")
        if leave_home:
            job.sudo().write({"recommended_driver_leave_home_at": leave_home})
        elif job.recommended_driver_leave_home_at:
            job.sudo().write({"recommended_driver_leave_home_at": False})

        for stop in ordered:
            times = self._stop_times(job, stop, anchor_dt, position,
                                     profile["mode"], profile["origin"],
                                     source)
            self._write_stop_eta(stop, times)
            # The walk anchor is the planned departure — a completed stop
            # anchors on its ACTUAL departure, an arrived stop on its
            # actual arrival (+ remaining service). A stop with no
            # coordinates keeps the previous position (last known).
            if stop.status in ("completed",):
                anchor_dt = stop.actual_departure_time or anchor_dt
                position = self._stop_position(stop) or position
            elif stop.status == "arrived":
                anchor_dt = stop.actual_arrival_time or anchor_dt
                position = self._stop_position(stop) or position
            elif stop.status == "en_route":
                # Hold semantics: the ETA stands; delay passes forward.
                anchor_dt = times["facility_service_start_at"] or anchor_dt
                position = self._stop_position(stop) or position
            else:
                anchor_dt = times["planned_departure_at"] or anchor_dt
                position = self._stop_position(stop) or position
        return ordered

    def recompute_job(self, job, source=None):
        """Live recalculation entry — used by the stop write() guard and
        every transition (arrive / complete / restore / driver update /
        service-time learning). Never raises: ETA is advisory.

        Source is DERIVED from execution state: a job with any executed
        stop (completed / arrived / en_route) recomputes "live"; a pure
        planning edit stays "scheduled" so the label never overclaims."""
        if source is None:
            executed = job.stop_ids.filtered(
                lambda s: not s.planning_only and s.status in
                ("completed", "arrived", "en_route"))
            source = "live" if executed else None
        try:
            self.compute_job_eta(job, source=source)
        except Exception:
            _logger.exception("ETA recompute failed for job %s", job.id)

    # ── Start profile ────────────────────────────────────────────────

    def _start_profile(self, job, anchor=None, source=None):
        """Resolve (mode, origin, anchor, leave_home_at) for the walk.

        mode: driver_start_mode on the vehicle (depot / hub / driver_home /
        prior_day_end). anchor: for live recalc = the caller's anchor or
        now (bounded by the last executed stop); for planning = dispatcher
        planned_operational_start → backward recommendation → first-stop
        schedule → job scheduled_pickup. leave_home_at exists only in
        driver_home mode: route start − pretrip − home→hub."""
        vehicle = job.vehicle_id.sudo()
        mode = vehicle.driver_start_mode or "depot" if vehicle else "depot"
        driver = job.driver_id.sudo()
        pretrip = int(vehicle.driver_pretrip_minutes or 10) if vehicle else 10
        home_to_hub = int(vehicle.driver_home_to_hub_minutes or 30) if vehicle else 30

        if source == "live":
            locked = job.stop_ids.filtered(
                lambda s: not s.planning_only and s.status in
                ("completed", "arrived")).sorted("sequence")
            if locked:
                last = locked[-1]
                return {
                    "mode": "locked", "origin": "locked_stop",
                    "position": self._stop_position(last),
                    "anchor": (last.actual_departure_time
                               or last.actual_arrival_time
                               or anchor or fields.Datetime.now()),
                }
            if not anchor:
                # Nothing executed yet: a first-stop transition (e.g. mark
                # en route) must NOT reset all ETAs to now — anchor on the
                # plan instead.
                source = "scheduled"
            anchor_dt = anchor or (
                job.planned_operational_start
                or job.recommended_operational_start
                or self._first_stop_schedule(job))
            if not anchor_dt:
                anchor_dt = fields.Datetime.now()
        else:
            anchor_dt = (job.planned_operational_start
                         or job.recommended_operational_start
                         or anchor)
            if not anchor_dt:
                first = job.stop_ids.filtered(
                    lambda s: not s.planning_only).sorted("sequence")[:1]
                anchor_dt = (first.scheduled_time if first else False) \
                    or job.scheduled_pickup or anchor or fields.Datetime.now()

        if mode == "driver_home":
            if driver and driver.home_latitude and driver.home_longitude:
                hub_pos = self._hub_position(job, vehicle)
                leave_home = anchor_dt - timedelta(
                    minutes=pretrip + home_to_hub)
                return {
                    "mode": mode, "origin": "driver_home",
                    "position": hub_pos, "anchor": anchor_dt,
                    "leave_home_at": leave_home,
                }
            # No home coords → fall through to depot.
            mode = "depot"
        if mode == "hub":
            return {
                "mode": mode, "origin": "hub",
                "position": self._hub_position(job, vehicle),
                "anchor": anchor_dt,
            }
        if mode == "prior_day_end":
            prior = self._prior_day_end(job)
            if prior:
                return {
                    "mode": mode, "origin": "prior_day_end",
                    "position": self._stop_position(prior),
                    "anchor": anchor_dt,
                }
        return {
            "mode": "depot", "origin": "depot",
            "position": self._depot_position(vehicle), "anchor": anchor_dt,
        }

    def _hub_position(self, job, vehicle):
        hub = job.hub_id if "hub_id" in job._fields else False
        if hub:
            lat = hub.latitude if "latitude" in hub._fields else False
            lng = hub.longitude if "longitude" in hub._fields else False
            if lat and lng:
                return (lat, lng)
        return self._depot_position(vehicle)

    @staticmethod
    def _depot_position(vehicle):
        if vehicle and vehicle.x_home_base_lat and vehicle.x_home_base_lng:
            return (vehicle.x_home_base_lat, vehicle.x_home_base_lng)
        return (43.648621, -79.659983)

    def _prior_day_end(self, job):
        """Last completed stop of this truck's previous day (cross-day
        positioning — same rule as the live-route service)."""
        if not job.vehicle_id or not job.scheduled_pickup:
            return False
        Stop = self.env["prema.dispatch.stop"]
        return Stop.search([
            ("job_id.vehicle_id", "=", job.vehicle_id.id),
            ("job_id.scheduled_pickup", "<", job.scheduled_pickup),
            ("status", "=", "completed"),
        ], order="actual_departure_time desc", limit=1)[:1]

    @staticmethod
    def _stop_position(stop):
        lat = stop.pin_lat if stop.pin_set and stop.pin_lat \
            else (stop.latitude or 0)
        lng = stop.pin_lng if stop.pin_set and stop.pin_lng \
            else (stop.longitude or 0)
        return (lat, lng) if lat and lng else None

    def _first_stop_schedule(self, job):
        """Earliest confirmed stop's scheduled time — the anchor of last
        resort when the dispatcher never set a start profile."""
        first = job.stop_ids.filtered(
            lambda s: not s.planning_only).sorted("sequence")[:1]
        return (first.scheduled_time if first else False) or job.scheduled_pickup

    # ── Per-stop computation ─────────────────────────────────────────

    def _stop_times(self, job, stop, anchor_dt, position, mode, origin,
                    source=None):
        """One stop's eight ETA values. Override / actual / en_route hold /
        pending walk — in that precedence order."""
        if stop.eta_override:
            override = stop.eta_override
            return {
                "travel_arrival_at": override,
                "facility_service_start_at": override,
                "planned_departure_at": override,
                "customer_eta_at": override,
                "eta_live": override,
                "eta_delay_minutes": self._delay_minutes(override, stop),
                "eta_source": "override",
                "eta_confidence": "high",
            }
        if stop.status in ("completed", "arrived"):
            actual = stop.actual_arrival_time or anchor_dt
            depart = stop.actual_departure_time or (
                actual + timedelta(
                    minutes=self._service_minutes(stop)))
            return {
                "travel_arrival_at": actual,
                "facility_service_start_at": actual,
                "planned_departure_at": depart,
                "customer_eta_at": actual,
                "eta_live": actual,
                "eta_delay_minutes": self._delay_minutes(actual, stop),
                "eta_source": "actual",
                "eta_confidence": "high",
            }
        if stop.status == "en_route" and (
                stop.eta_live or stop.facility_service_start_at):
            # Hold: the truck is already on its way — the ETA stands, the
            # delay it carries passes forward unchanged. The live ETA
            # (driver/GPS-updated) is the operative value when present;
            # the planned service start backs it up otherwise.
            held = stop.eta_live or stop.facility_service_start_at
            return {
                "travel_arrival_at": stop.travel_arrival_at or held,
                "facility_service_start_at": held,
                "planned_departure_at": stop.planned_departure_at or (
                    held + timedelta(minutes=self._service_minutes(stop))),
                "customer_eta_at": stop.customer_eta_at or held,
                "eta_live": held,
                "eta_delay_minutes": self._delay_minutes(held, stop),
                "eta_source": "live",
                "eta_confidence": self._confidence(stop, held),
            }

        travel = self._travel_minutes(job, stop, position)
        travel_arrival = anchor_dt + timedelta(minutes=travel)
        feasible, _waiting, service_start, departure = self._arrival_plan(
            stop, travel_arrival)
        if not feasible:
            # Facility closed or arrival after close: the estimate still
            # respects hours — never before an opening time. Roll to the
            # next open slot (bounded scan, same rule as _facility_eta).
            service_start = self._roll_to_next_open(stop, travel_arrival) \
                or travel_arrival
            departure = service_start + timedelta(
                minutes=self._service_minutes(stop))
            feasible = True
        confidence = self._confidence(stop, service_start)
        return {
            "travel_arrival_at": travel_arrival,
            "facility_service_start_at": service_start,
            "planned_departure_at": departure,
            "customer_eta_at": service_start,
            "eta_live": service_start,
            "eta_delay_minutes": self._delay_minutes(service_start, stop),
            "eta_source": source or (
                "live" if mode == "locked" else "scheduled"),
            "eta_confidence": confidence,
        }

    def _planner_stop(self, stop):
        """Adapt a dispatch stop record to the planner's dict shape — the
        SAME field mapping _recommended_operational_start uses (timing
        types + hour-float windows evaluated in the stop's tz)."""
        tz_name = stop.tz_name or "America/Toronto"

        def _hour(dt):
            if not dt:
                return None
            from pytz import timezone as tz
            local = dt.astimezone(tz(tz_name))
            return round(local.hour + local.minute / 60.0, 4)

        timing = "flexible"
        if stop.time_window_type == "window":
            timing = "time_window"
        elif stop.time_window_type == "exact" and stop.exact_time:
            timing = "exact_appointment"
        elif stop.time_window_type == "deadline" and stop.deadline_time:
            timing = "deadline"
        return {
            "timezone": tz_name,
            "operating_hours_snapshot": stop.operating_hours_snapshot,
            "timing_type": timing,
            "window_start": _hour(stop.earliest_time),
            "window_end": _hour(stop.latest_time),
            "appointment_time": _hour(stop.exact_time),
            "service_time_minutes": self._service_minutes(stop),
        }

    def _arrival_plan(self, stop, arrival_dt):
        """(feasible, waiting, service_start, departure) via the planner —
        facility hours + timing windows evaluated in the stop's tz."""
        from ..services.itinerary_planner import ItineraryPlanner
        return ItineraryPlanner(self.env).arrival_plan(
            self._planner_stop(stop), arrival_dt)

    def _roll_to_next_open(self, stop, arrival_dt):
        """Closed day or after close → next day the facility opens
        (bounded, never an endless scan)."""
        from ..services.itinerary_planner import ItineraryPlanner
        from pytz import timezone as tz
        planner = ItineraryPlanner(self.env)
        plan_stop = self._planner_stop(stop)
        probe = arrival_dt
        tz_name = stop.tz_name or "America/Toronto"
        for _ in range(7):
            probe = probe + timedelta(days=1)
            window = planner.effective_window(plan_stop, probe)
            if window is None:
                continue
            local = probe.astimezone(tz(tz_name))
            open_dt = local.replace(
                hour=int(window[0]), minute=int((window[0] % 1) * 60),
                second=0)
            return open_dt.astimezone(tz("UTC")).replace(tzinfo=None)
        return False

    # ── Travel + service + confidence ────────────────────────────────

    def _travel_minutes(self, job, stop, position):
        """Google estimate when stored, straight-line ×1.4 @ 50 km/h
        otherwise — the deterministic fallback, identical math to the
        planner's."""
        if stop.drive_time_from_prev_minutes:
            return max(1, int(stop.drive_time_from_prev_minutes))
        pos = position or self._stop_position(stop)
        if not pos:
            # No coordinates anywhere (ungeocoded stop, no anchor):
            # deterministic fallback leg — never crash the walk.
            return 10.0
        lat1, lng1 = pos
        lat2, lng2 = self._stop_position(stop) or pos
        if not (lat1 and lat2 and lng1 and lng2):
            return 10.0
        dx = (lng2 - lng1) * 111.32 * math.cos(
            math.radians((lat1 + lat2) / 2))
        dy = (lat2 - lat1) * 111.32
        km = math.sqrt(dx * dx + dy * dy) * 1.4
        return max(1, int(round(km / 50.0 * 60.0)))

    def _service_minutes(self, stop):
        """Stop service time, else the facility's planning authority
        (manual → history → class default → 15) — ONE hierarchy."""
        if stop.service_time_minutes:
            return max(1, int(stop.service_time_minutes))
        loc = stop.saved_location_id.sudo()
        if loc and hasattr(loc, "planning_service_time_minutes"):
            return max(1, int(loc.planning_service_time_minutes() or 15))
        return 15

    def _confidence(self, stop, service_start):
        """eta_confidence: high (exact appointment + verified hours),
        medium (hours verified — the wait prediction is trustworthy),
        low (facility hours UNVERIFIED — the 'HOURS NOT VERIFIED' grade)."""
        loc = stop.saved_location_id.sudo()
        if loc and "hours_verified" in loc._fields:
            verified = bool(loc.hours_verified)
        else:
            verified = True
        exact = stop.time_window_type == "exact" and stop.exact_time
        if exact and verified:
            return "high"
        return "medium" if verified else "low"

    @staticmethod
    def _delay_minutes(eta, stop):
        if not eta or not stop.scheduled_time:
            return 0.0
        return round((eta - stop.scheduled_time).total_seconds() / 60.0, 1)

    def _write_stop_eta(self, stop, values):
        """Write only fields that changed (avoids churn in write() guards
        and chatter), always under sudo (drivers hold no ETA rights)."""
        values = {k: v for k, v in values.items() if k in ENGINE_FIELDS}
        current = {
            k: getattr(stop, k) for k in values if k in stop._fields
        }
        changed = {
            k: v for k, v in values.items()
            if k in stop._fields and v != current.get(k)
        }
        if changed:
            stop.sudo().with_context(_eta_engine_write=True).write(changed)

    # ── Job-level helpers ────────────────────────────────────────────

    def jobs_touching_location(self, location, day):
        """Active jobs with a stop at `location` on `day` — used by the
        service-time learning hook (bounded recompute)."""
        Stop = self.env["prema.dispatch.stop"]
        stops = Stop.search([
            ("saved_location_id", "=", location.id),
            ("planning_only", "=", False),
        ], limit=200)
        jobs = stops.mapped("job_id").filtered(
            lambda j: j.scheduled_pickup
            and j.scheduled_pickup.date() == day
            and not j.all_stops_completed)
        return jobs[:20]

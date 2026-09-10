"""DayRouteService — §15 daily multi-booking trip optimizer (work package D-B4).

Combines every accepted/confirmed load assigned to one truck on one
operating day into a single PROPOSED stop sequence:

  * precedence: pickup-before-delivery per physical load unit (dispatch
    items when present; per-job pickup→delivery chains inferred from the
    dispatch stops' pallet fields for legacy jobs without items);
  * windows: each stop's real timing fields (time_window_type
    window/exact/deadline + earliest/latest/exact/deadline_time), facility
    hours snapshot, and service_time_minutes — evaluated through the same
    ItineraryPlanner arrival_plan the milk-run adviser uses, in the stop's
    own timezone;
  * capacity: running pallet-position + weight timeline after every stop,
    checked against the vehicle's canonical layout capacity;
  * driving: the codebase's own travel estimator (Google-first,
    straight-line ×1.4 @ 50 km/h fallback) — no invented ELD model;
  * anchors: executed history (completed stops) pins the start clock and
    position; en-route/arrived/route-locked stops are pinned in place and
    the optimizer plans the movable stops around them;
  * FTL-dedicated jobs are excluded from shared-day mixing (same rule as
    suggest_consolidated_route).

PROPOSAL-ONLY: nothing in this service ever writes a stop/job. It builds
the payload that prema.dispatch.day.route.proposal persists; applying is
an explicit dispatcher action guarded by a fingerprint + version (see
models/dispatch_day_route_proposal.py).
"""
import hashlib
import json
import logging
from datetime import date as _date, datetime, time, timedelta

from odoo.exceptions import UserError
from pytz import timezone as pytz_timezone

from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
    ItineraryPlanner,
)

_logger = logging.getLogger(__name__)

DEFAULT_TZ = "America/Toronto"
OPEN_ALL = {str(day): [0.0, 24.0] for day in range(7)}
FALLBACK_KMH = 50.0

# Stop statuses that mean "already physically done" — never re-planned.
DONE_STATUSES = ("completed", "skipped", "cancelled")
# Statuses that pin a stop in its current slot (execution in progress or
# the dispatcher locked it).
PINNED_STATUSES = ("en_route", "arrived")
# Stop fields that, when a stop changes, invalidate an open proposal.
STALE_TRIGGER_FIELDS = (
    "sequence", "status", "stop_type", "scheduled_time",
    "time_window_type", "earliest_time", "latest_time", "exact_time",
    "deadline_time", "hard_deadline", "appointment_confirmed",
    "facility_open_time", "facility_close_time",
    "service_time_minutes", "pallets_in", "pallets_out",
    "weight_in_lbs", "weight_out_lbs", "route_locked", "planning_only",
)


def _lazy_adviser():
    from odoo.addons.prema_dispatch.services.route_adviser_service import (
        RouteAdviserService,
        DEFAULT_TZ as _ADV_DEFAULT_TZ,
        OPEN_ALL as _ADV_OPEN_ALL,
    )
    return RouteAdviserService, _ADV_DEFAULT_TZ, _ADV_OPEN_ALL


def _lazy_route_service():
    from odoo.addons.prema_dispatch.services.route_service import (
        DispatchRouteService,
    )
    return DispatchRouteService


def _lazy_capacity():
    from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
        VehicleCapacityService,
    )
    return VehicleCapacityService


class DayRouteService:
    """Deterministic day-level sequencing for one truck / operating day."""

    def __init__(self, env):
        self.env = env
        self._travel_min_cache = {}
        self._travel_km_cache = {}
        self._adviser = None
        self._route_svc = None

    # ── Plumbing ────────────────────────────────────────────────────

    def _user_tz(self):
        import pytz
        return pytz.timezone(self.env.user.tz or DEFAULT_TZ)

    def _adviser(self):
        if self._adviser is None:
            from odoo.addons.prema_dispatch.services.route_adviser_service import (
                RouteAdviserService,
            )
            self._adviser = RouteAdviserService(self.env)
        return self._adviser

    def _route_svc(self):
        if self._route_svc is None:
            cls = _lazy_route_service()
            self._route_svc = cls(self.env)
        return self._route_svc

    def _stop_local_date(self, stop, job=None):
        """Same rule the board/planner use: a stop belongs to its own
        scheduled_time's local date, else its job's pickup day (dropoff
        legs of a multi-day job fall back to the delivery day)."""
        job = job or stop.job_id
        ref = stop.scheduled_time
        if not ref:
            if stop.stop_type == "pickup":
                ref = job.scheduled_pickup
            else:
                ref = job.scheduled_delivery or job.scheduled_pickup
        if not ref:
            return None
        return pytz_timezone("UTC").localize(ref).astimezone(
            self._user_tz()).date()

    # ── Day scope ───────────────────────────────────────────────────

    def day_scope(self, vehicle_id, operating_date, exclude_ftl=True):
        """The stops that make up truck/day: active jobs on the vehicle,
        kept per local day. Returns
        {jobs, movable, pinned, executed, date} where movable = status
        pending/issue (reorderable), pinned = en-route/arrived/route_locked
        (keep their slot), executed = completed (anchor only)."""
        if isinstance(operating_date, str):
            operating_date = _date.fromisoformat(operating_date)
        jobs = self.env["prema.dispatch.job"].search([
            ("vehicle_id", "=", vehicle_id),
            ("stage_id.is_completed", "=", False),
            ("stage_id.is_cancelled", "=", False),
        ])
        # FTL-dedicated freight never mixes with other customers' loads on
        # a shared day proposal (parity with suggest_consolidated_route).
        if exclude_ftl:
            kept, _excluded = [], []
            for job in jobs:
                if "logistics_booking_id" not in job._fields or not job.logistics_booking_id:
                    kept.append(job)
                    continue
                booking = job.logistics_booking_id
                is_ftl = booking.shipment_type == "ftl"
                if not is_ftl and booking.corridor_id:
                    corridor = booking.corridor_id
                    is_ftl = bool(
                        corridor.enable_ftl
                        and corridor.ftl_behavior == "auto_price"
                        and corridor.ftl_threshold_pallets
                        and booking.pallets >= corridor.ftl_threshold_pallets
                    )
                if is_ftl:
                    _excluded.append(job)
                else:
                    kept.append(job)
            jobs = self.env["prema.dispatch.job"].browse([j.id for j in kept])

        movable = self.env["prema.dispatch.stop"]
        pinned = self.env["prema.dispatch.stop"]
        executed = self.env["prema.dispatch.stop"]
        scope_jobs = self.env["prema.dispatch.job"]
        for job in jobs:
            day_stops = job.stop_ids.filtered(
                lambda s, j=job: self._stop_local_date(s, j) == operating_date
            )
            if not day_stops:
                continue
            scope_jobs |= job
            movable |= day_stops.filtered(
                lambda s: s.status not in DONE_STATUSES + PINNED_STATUSES
                and not s.route_locked
            )
            pinned |= day_stops.filtered(
                lambda s: s.status in PINNED_STATUSES or s.route_locked
            )
            executed |= day_stops.filtered(lambda s: s.status in DONE_STATUSES)
        return {
            "jobs": scope_jobs,
            "movable": movable.sorted(lambda s: (s.sequence or 0, s.id)),
            "pinned": pinned.sorted(lambda s: (s.sequence or 0, s.id)),
            "executed": executed.sorted(lambda s: (s.sequence or 0, s.id)),
            "date": operating_date,
        }

    # ── Load units ──────────────────────────────────────────────────

    def _job_units_from_items(self, job, scope_ids):
        """Item movements for one job restricted to the day scope. Returns
        (planned, preloaded): planned units have their pickup inside the
        movable set; preloaded units were picked before the plan (pickup
        executed / earlier day) and start on the truck."""
        adviser = self._adviser()
        units = []
        for m in adviser.movements(job):
            pickup_stop = self.env["prema.dispatch.stop"].browse(
                int(m["pickup_stop_key"][2:]))
            deliveries = [
                self.env["prema.dispatch.stop"].browse(int(k[2:]))
                for k in m.get("delivery_stop_keys") or []
            ]
            deliveries = [d for d in deliveries if d.id in scope_ids]
            if not deliveries or not pickup_stop.exists():
                continue
            pickup_done = pickup_stop.status in DONE_STATUSES
            planned = pickup_stop.id in scope_ids and not pickup_done
            if planned and pickup_stop.status in PINNED_STATUSES:
                planned = False
            units.append({
                "pickup_stop_id": pickup_stop.id if planned else False,
                "delivery_stop_ids": [d.id for d in deliveries],
                "pallets": 1,
                "weight_lbs": float(m.get("weight_lbs") or 0.0),
                "shared": bool(m.get("shared") or len(deliveries) > 1),
                "label": m.get("label") or "Item",
                "preloaded": not planned,
            })
        planned_units = [u for u in units if not u["preloaded"]]
        preloaded_units = [u for u in units if u["preloaded"]]
        return planned_units, preloaded_units

    def _job_units_from_stops(self, job, scope_ids, day_stops):
        """Legacy fallback (no dispatch items): build load units from the
        stops' own pallet/weight fields. Each pickup yields one unit per
        following pending delivery (portion = that delivery's pallets_out),
        so shared-pallet subtleties do not apply to pallet counts."""
        units = []
        ordered = day_stops.sorted(lambda s: (s.sequence or 0, s.id))
        stop_ids = [s.id for s in ordered if s.id in scope_ids]
        pending = {s.id for s in ordered if s.status not in DONE_STATUSES}
        pickup_indices = [
            (i, s) for i, s in enumerate(ordered)
            if s.stop_type in ("pickup", "cross_dock_pickup")
        ]
        for idx, (i, pickup) in enumerate(pickup_indices):
            nxt = pickup_indices[idx + 1][0] if idx + 1 < len(pickup_indices) else len(ordered)
            following = [s for s in ordered[i + 1:nxt]
                         if s.stop_type in ("dropoff", "return", "transfer",
                                            "cross_dock_drop")
                         and s.pallets_out > 0 and s.id in stop_ids]
            if not following:
                continue
            load_pallets = pickup.pallets_in or sum(
                s.pallets_out for s in following)
            if load_pallets <= 0:
                continue
            total_out = sum(s.pallets_out for s in following) or load_pallets
            pickup_done = pickup.id not in pending
            for s in following:
                portion = load_pallets * s.pallets_out / total_out
                weight = 0.0
                if s.weight_out_lbs:
                    weight = (pickup.weight_in_lbs or load_pallets) \
                        * s.weight_out_lbs / total_out
                units.append({
                    "pickup_stop_id": pickup.id if not pickup_done else False,
                    "delivery_stop_ids": [s.id],
                    "pallets": max(1, round(portion)),
                    "weight_lbs": weight,
                    "shared": False,
                    "label": "Load %s" % pickup.id,
                    "preloaded": pickup_done,
                })
        planned = [u for u in units if not u["preloaded"]]
        preloaded = [u for u in units if u["preloaded"]]
        # Preloaded deliveries whose pickup executed with explicit
        # pallets_in and NO remaining pending dropoffs contribute nothing.
        return planned, preloaded

    def load_units(self, scope):
        """(planned_units, preloaded_units) across every scope job."""
        planned, preloaded = [], []
        scope_ids = set(scope["movable"].ids) | set(scope["pinned"].ids)
        for job in scope["jobs"]:
            has_items = bool(
                "item_ids" in job._fields and job.item_ids
                and job.item_ids.filtered(
                    lambda i: i.pickup_stop_id
                    and (i.delivery_stop_id or i.stop_allocation_ids)))
            day_stops = job.stop_ids.filtered(
                lambda s: self._stop_local_date(s, job) == scope["date"])
            if has_items:
                p, pre = self._job_units_from_items(job, scope_ids)
            else:
                p, pre = self._job_units_from_stops(
                    job, scope_ids, day_stops)
            planned.extend(p)
            preloaded.extend(pre)
        return planned, preloaded

    # ── Travel (Google-first, cached, km + minutes) ────────────────

    def _pos_lat_lng(self, pos):
        if pos is None:
            return None, None
        if isinstance(pos, dict):
            return pos.get("latitude") or 0, pos.get("longitude") or 0
        lat = pos.latitude or 0
        lng = pos.longitude or 0
        if hasattr(pos, "x_last_location_lat") and pos.x_last_location_lat:
            lat = pos.x_last_location_lat or lat
            lng = pos.x_last_location_lng or lng
        return lat, lng

    def _travel(self, from_pos, to_stop):
        """(drive_minutes, distance_km) between a position and a stop —
        identical pair-cache discipline as the route adviser."""
        lat1, lng1 = self._pos_lat_lng(from_pos)
        lat2 = (to_stop.get("latitude") or 0) if isinstance(to_stop, dict) \
            else (to_stop.latitude or 0)
        lng2 = (to_stop.get("longitude") or 0) if isinstance(to_stop, dict) \
            else (to_stop.longitude or 0)
        if not (lat1 and lat2):
            return 10.0, 0.0
        pair = (round(lat1, 5), round(lng1, 5), round(lat2, 5), round(lng2, 5))
        if pair in self._travel_min_cache:
            return self._travel_min_cache[pair], self._travel_km_cache[pair]
        minutes, km = 10.0, 0.0
        try:
            legs = self._route_svc().get_sequential_travel(
                [pair[:2], pair[2:]])
            if legs:
                minutes = float(legs[0].get("drive_minutes") or 10.0)
                km = float(legs[0].get("distance_km") or 0.0)
        except Exception:
            _logger.exception("day route travel failed for pair %s", pair)
        if not minutes:
            minutes = 10.0
        self._travel_min_cache[pair] = minutes
        self._travel_km_cache[pair] = km
        return minutes, km

    def _travel_minutes_fn(self, from_pos, to_stop):
        return self._travel(from_pos, to_stop)[0]

    # ── Capacity walk ───────────────────────────────────────────────

    def simulate_day_capacity(self, ordered_stops, planned_units,
                              preloaded_units):
        """Running pallet/weight timeline over an ordered scope list, with
        preloaded freight on board at the start. Shared pallets leave the
        truck only at their last delivery still on the route."""
        stops = list(ordered_stops)
        route_position = {s.id: i for i, s in enumerate(stops)}
        onboard = sum(u["pallets"] for u in preloaded_units)
        onboard_w = sum(u["weight_lbs"] for u in preloaded_units)
        # Active planned units (picked up along the walk).
        active = set()
        deltas = []
        peak = onboard
        negative = False
        for i, stop in enumerate(stops):
            before = onboard
            before_w = onboard_w
            picked = 0
            for u in planned_units:
                if u["pickup_stop_id"] == stop.id and u["pickup_stop_id"]:
                    active.add(id(u))
                    onboard += u["pallets"]
                    onboard_w += u["weight_lbs"]
                    picked += u["pallets"]
            delivered = 0
            for u in planned_units + preloaded_units:
                deliveries = [d for d in u["delivery_stop_ids"]]
                if stop.id not in deliveries:
                    continue
                n = len(deliveries)
                weight_portion = (u["weight_lbs"] or 0.0) / n if n else 0.0
                onboard_w -= weight_portion
                remaining = [d for d in deliveries
                             if route_position.get(d, 10 ** 9) > i]
                if not remaining:
                    # Last in-route delivery frees the pallet position —
                    # but only when the freight was actually picked up; a
                    # delivery before its pickup is a precedence violation.
                    if id(u) in active or u["preloaded"]:
                        onboard -= u["pallets"]
                        delivered += u["pallets"]
                    else:
                        negative = True
            if onboard < 0:
                negative = True
            peak = max(peak, onboard)
            deltas.append({
                "stop_id": stop.id,
                "before": before,
                "picked": picked,
                "after": onboard,
                "weight_before": round(before_w, 1),
                "weight_after": round(onboard_w, 1),
            })
        return {"deltas": deltas, "peak": peak, "onboard_after": onboard,
                "weight_after": round(onboard_w, 1), "negative": negative}

    # ── Anchor ──────────────────────────────────────────────────────

    def day_anchor(self, scope, vehicle):
        """(start_dt, start_position) for the day walk: the last completed
        stop's actual departure when execution already started, else the
        earliest scheduled pickup of the day (falling back to 08:00 local,
        the operation-day convention used across the codebase)."""
        executed = scope["executed"]
        if executed:
            last = executed[-1]
            start_dt = (
                last.actual_departure_time
                or last.actual_arrival_time
                or last.estimated_departure
                or last.scheduled_time
                or datetime.utcnow()
            )
            return start_dt, last
        picks = [
            j.scheduled_pickup for j in scope["jobs"] if j.scheduled_pickup
            and self._stop_local_date(
                j.stop_ids.filtered(
                    lambda s, jj=j: s.stop_type == "pickup"
                )[:1] or j.stop_ids[:1], j) == scope["date"]
        ]
        if picks:
            return min(picks), None
        local = pytz_timezone(DEFAULT_TZ)
        start_local = local.localize(
            datetime.combine(scope["date"], time(hour=8)))
        start_dt = start_local.astimezone(
            pytz_timezone("UTC")).replace(tzinfo=None)
        position = vehicle if vehicle.x_last_location_lat else None
        return start_dt, position

    # ── Vehicle capacity bound ──────────────────────────────────────

    def vehicle_capacity(self, vehicle):
        """Position capacity from the canonical layout, then the legacy
        x_max_pallets field; 0 = unknown (no bound enforced)."""
        if not vehicle:
            return 0
        try:
            svc = _lazy_capacity()(self.env)
            result = svc.evaluate(vehicle, False, 0)
            if result.get("selected_layout"):
                return result["selected_layout"].get("max_pallets") or 0
        except Exception:
            pass
        return vehicle.x_max_pallets or 0

    # ── Stop dicts (planner input) ──────────────────────────────────

    def _stop_dicts(self, stops):
        adviser = self._adviser()
        return [adviser.stop_dict(s) for s in stops]

    # ── Full proposal build ─────────────────────────────────────────

    def build_proposal_payload(self, vehicle_id, operating_date):
        """Everything the proposal record persists. NO writes to jobs/stops."""
        if isinstance(operating_date, str):
            operating_date = _date.fromisoformat(operating_date)
        vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
        scope = self.day_scope(vehicle_id, operating_date)
        movable = scope["movable"]
        pinned = scope["pinned"]
        if not movable and not pinned:
            raise UserError(
                "Nothing to optimize: truck %s has no stops on %s."
                % (vehicle.name or vehicle_id, operating_date))
        if len(movable) < 2 and not pinned:
            raise UserError(
                "Only one pending stop on truck %s for %s — a single-stop "
                "day has nothing to sequence." % (
                    vehicle.name or vehicle_id, operating_date))

        planned_units, preloaded_units = self.load_units(scope)
        start_dt, start_position = self.day_anchor(scope, vehicle)
        vehicle_max = self.vehicle_capacity(vehicle)
        start_onboard = sum(u["pallets"] for u in preloaded_units)

        # Planner legality/capacity input = planned units only.
        planner_movements = []
        for u in planned_units:
            if not u["pickup_stop_id"]:
                continue
            deliveries = [d for d in u["delivery_stop_ids"]
                          if d in movable.ids or d in pinned.ids]
            if not deliveries:
                continue
            planner_movements.append({
                "key": "u%d-%d" % (u["pickup_stop_id"], len(planner_movements)),
                "pallet_id": False,
                "label": u["label"],
                "weight_lbs": u["weight_lbs"],
                "shared": u["shared"],
                "pickup_stop_key": "ds%d" % u["pickup_stop_id"],
                "delivery_stop_keys": ["ds%d" % d for d in deliveries],
            })

        planner = ItineraryPlanner(self.env)
        bound = max(0, vehicle_max - start_onboard) if vehicle_max else 0
        dicts = self._stop_dicts(movable)
        start_pos_dict = None
        if start_position is not None:
            lat, lng = self._pos_lat_lng(start_position)
            start_pos_dict = {"stop_key": "start", "name": "Start",
                              "latitude": lat, "longitude": lng}
        result = planner.recommend_route(
            dicts, planner_movements, start_dt,
            vehicle_max=bound,
            start_position=start_pos_dict,
            travel_fn=self._travel_minutes_fn,
        )
        recommended_keys = result.get("recommended") or []
        recommended_ids = [int(k[2:]) for k in recommended_keys]
        reason_code = result.get("reason") or ""
        conflicts = list(result.get("reasons") or [])
        planned_feasible = bool(result.get("feasible"))

        # Merge: pinned stops keep their current slot, movables follow the
        # recommendation in order (adviser apply_recommended_route merge).
        current = (movable | pinned).sorted(
            lambda s: (s.sequence or 0, s.id))
        merged = []
        rec_iter = iter(recommended_ids)
        for stop in current:
            if stop.id in pinned.ids:
                merged.append(stop)
            else:
                try:
                    nxt = next(rec_iter)
                    merged.append(self.env["prema.dispatch.stop"].browse(nxt))
                except StopIteration:
                    merged.append(stop)

        # Final walk over the merged order → ETA/slack per stop.
        sim = self.simulate_day_capacity(
            merged, planned_units, preloaded_units)
        feasible = planned_feasible and not sim["negative"]
        lines = []
        walk_dt = start_dt
        walk_pos = start_position
        onboard_by_id = {}
        for i, delta in enumerate(sim["deltas"]):
            onboard_by_id[delta["stop_id"]] = delta
        entry_by_id = {s.id: i for i, s in enumerate(current)}
        rec_by_id = {s.id: i for i, s in enumerate(merged)}
        total_drive = 0.0
        total_km = 0.0
        total_waiting = 0.0
        stop_by_key = {"ds%d" % s.id: s for s in merged}
        for i, stop in enumerate(merged):
            info = self._stop_dicts([stop])[0]
            drive_min, km = 0.0, 0.0
            if walk_pos is not None:
                drive_min, km = self._travel(walk_pos, info)
            arrival = walk_dt + timedelta(minutes=drive_min)
            ok, waiting, service_start, departure = planner.arrival_plan(
                info, arrival)
            if not ok:
                feasible = False
                conflicts.append(
                    "Stop %s cannot be reached within its window/deadline."
                    % (stop.address or stop.id))
            total_drive += drive_min
            total_km += km
            total_waiting += waiting
            delta = onboard_by_id.get(stop.id, {})
            lines.append({
                "stop_id": stop.id,
                "job_id": stop.job_id.id,
                "entry_order": entry_by_id.get(stop.id, 0) + 1,
                "optimized_order": rec_by_id.get(stop.id, 0) + 1,
                "pinned": stop.id in pinned.ids,
                "eta": arrival,
                "waiting_minutes": round(waiting, 0),
                "service_start_at": service_start,
                "departure_at": departure,
                "onboard_before": delta.get("before", 0),
                "onboard_after": delta.get("after", 0),
                "weight_after": delta.get("weight_after", 0.0),
                "drive_minutes": round(drive_min, 0),
                "distance_km": round(km, 1),
            })
            walk_dt = departure
            walk_pos = info
        if not lines:
            raise UserError("The day proposal came out empty — no sequence.")

        finish_eta = lines[-1]["departure_at"]
        fingerprint = self.compute_fingerprint(
            vehicle_id, operating_date, scope)
        capacity_timeline = [
            {"stop_id": d["stop_id"], "before": d["before"],
             "after": d["after"],
             "weight_after": d["weight_after"]}
            for d in sim["deltas"]
        ]
        return {
            "vehicle_id": vehicle_id,
            "operating_date": operating_date,
            "state": "proposed",
            "feasible": feasible,
            "reason": reason_code,
            "conflicts_json": json.dumps(conflicts[:50]),
            "start_dt": start_dt,
            "vehicle_max_pallets": vehicle_max,
            "onboard_at_start": start_onboard,
            "total_drive_minutes": round(total_drive, 0),
            "total_distance_km": round(total_km, 1),
            "total_waiting_minutes": round(total_waiting, 0),
            "finish_eta": finish_eta,
            "peak_onboard": sim["peak"],
            "fingerprint": fingerprint,
            "capacity_timeline_json": json.dumps(capacity_timeline),
            "line_ids": [(0, 0, dict(line)) for line in lines],
        }

    def compute_fingerprint(self, vehicle_id, operating_date, scope=None):
        """sha1 over the whole day scope — every scope stop id plus the
        per-stop planning fields and the owning job's vehicle/stage. Any
        drift (a moved stop, edited window, added/removed load) changes the
        fingerprint, so a stale proposal can never be silently re-applied."""
        if scope is None:
            scope = self.day_scope(vehicle_id, operating_date)
        rows = []
        stops = (scope["movable"] | scope["pinned"] | scope["executed"]
                 ).sorted(lambda s: (s.sequence or 0, s.id))
        for s in stops:
            rows.append({
                "sid": s.id,
                "job": s.job_id.id,
                "job_stage": s.job_id.stage_id.id if s.job_id.stage_id else False,
                "job_vehicle": s.job_id.vehicle_id.id if s.job_id.vehicle_id else False,
                "seq": s.sequence,
                "status": s.status,
                "stop_type": s.stop_type,
                "twt": s.time_window_type,
                "earliest": s.earliest_time.isoformat() if s.earliest_time else None,
                "latest": s.latest_time.isoformat() if s.latest_time else None,
                "exact": s.exact_time.isoformat() if s.exact_time else None,
                "deadline": s.deadline_time.isoformat() if s.deadline_time else None,
                "hard": s.hard_deadline,
                "svc": s.service_time_minutes,
                "pin": s.pallets_in,
                "pout": s.pallets_out,
                "sched": s.scheduled_time.isoformat() if s.scheduled_time else None,
                "locked": s.route_locked,
                "planning_only": s.planning_only,
            })
        raw = json.dumps(rows, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    # ── Manual-order validation (Planner board drag guard, §16.6) ───

    def validate_day_stop_order(self, stop_order, vehicle_id=None,
                                operating_date=None):
        """Structural validation of a dispatcher-submitted day order before
        any write: same-truck active scope completeness, pinned/executed
        relative order preserved, pickup-before-delivery, capacity.
        Returns {valid, errors} — never writes."""
        stops = self.env["prema.dispatch.stop"].browse(stop_order).exists()
        errors = []
        by_id = {s.id: s for s in stops}
        if not by_id:
            return {"valid": False, "errors": ["No valid stops in the order."]}
        if len(stops) != len(set(stop_order)):
            errors.append("The order contains duplicate stops.")

        movable = stops.filtered(
            lambda s: s.status not in DONE_STATUSES + PINNED_STATUSES
            and not s.route_locked)
        anchored = stops.filtered(
            lambda s: s.status in DONE_STATUSES + PINNED_STATUSES
            or s.route_locked)

        # Movable stops must all sit on one active truck.
        for s in movable:
            job = s.job_id
            if job.stage_id and (job.stage_id.is_completed
                                 or job.stage_id.is_cancelled):
                errors.append("Stop %s belongs to a completed/cancelled job."
                              % s.id)
        vehicles = set(movable.mapped("job_id.vehicle_id.id")) - {False}
        if len(vehicles) > 1:
            errors.append("Order mixes stops from different trucks: %s"
                          % sorted(vehicles))
        vehicle_id = vehicle_id or next(iter(vehicles), False)
        if not vehicle_id:
            errors.append("No truck can be determined for this order.")

        # Derived day: movable stops must share one local day.
        dates = {self._stop_local_date(s) for s in movable}
        dates.discard(None)
        if len(dates) > 1:
            errors.append("Order spans multiple days: %s"
                          % sorted(str(d) for d in dates))
        if operating_date is None and dates:
            operating_date = next(iter(dates))

        # Anchored stops (executed/en-route/arrived/route-locked) keep
        # their exact slots: only still-pending stops may move. The board
        # panel shows the truck's whole day (incl. FTL freight), so the
        # scope is computed WITHOUT the optimizer's FTL exclusion here.
        scope = None
        if vehicle_id and operating_date:
            scope = self.day_scope(vehicle_id, operating_date,
                                   exclude_ftl=False)
            scope_order = (scope["pinned"] | scope["executed"]
                           | scope["movable"]).sorted(
                lambda s: (s.sequence or 0, s.id))
            anchored_scope_ids = [
                s.id for s in scope_order
                if s.status in DONE_STATUSES + PINNED_STATUSES
                or s.route_locked
            ]
            submitted_anchored = [
                sid for sid in anchored_scope_ids if sid in stop_order
            ]
            if submitted_anchored:
                sub_pos = [stop_order.index(sid)
                           for sid in submitted_anchored]
                scope_pos = [scope_order.ids.index(sid)
                             for sid in submitted_anchored]
                if sub_pos != scope_pos:
                    errors.append(
                        "Executed/en-route/locked stops must keep their "
                        "slots — the drag would rewrite history; only "
                        "pending stops can move (refresh the board).")

        # Completeness vs the current day scope (movable set equality).
        if vehicle_id and operating_date:
            scope = scope or self.day_scope(vehicle_id, operating_date,
                                            exclude_ftl=False)
            scope_movable = set(scope["movable"].ids)
            submitted_movable = set(movable.ids)
            if scope_movable != submitted_movable:
                missing = scope_movable - submitted_movable
                extra = submitted_movable - scope_movable
                if missing:
                    errors.append(
                        "Incomplete order: %d pending stop(s) of this "
                        "truck/day are missing (refresh the board)."
                        % len(missing))
                if extra:
                    errors.append("Order contains stops outside this "
                                  "truck/day: %s" % sorted(extra))

        # Precedence + capacity over the submitted movable sequence
        # (anchored stops pinned in the walk keep their slots).
        if not errors and vehicle_id and operating_date:
            scope = scope or self.day_scope(vehicle_id, operating_date)
            current = (scope["movable"] | scope["pinned"]).sorted(
                lambda s: (s.sequence or 0, s.id))
            merged = []
            movable_in_order = [
                by_id[sid] for sid in stop_order if sid in submitted_movable
            ]
            rec_iter = iter([s.id for s in movable_in_order])
            for s in current:
                if s.id in scope["pinned"].ids:
                    merged.append(s)
                else:
                    try:
                        merged.append(self.env["prema.dispatch.stop"].browse(
                            next(rec_iter)))
                    except StopIteration:
                        merged.append(s)
            planned_units, preloaded_units = self.load_units(scope)
            sim = self.simulate_day_capacity(
                merged, planned_units, preloaded_units)
            if sim["negative"]:
                errors.append(
                    "Invalid order: a delivery would happen before its "
                    "pickup (negative onboard pallets).")
            vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
            vmax = self.vehicle_capacity(vehicle)
            start_onboard = sum(u["pallets"] for u in preloaded_units)
            if vmax and sim["peak"] > vmax:
                errors.append(
                    "Peak onboard %s exceeds the truck's %s pallet "
                    "positions." % (sim["peak"], vmax))
        return {"valid": not errors, "errors": errors,
                "vehicle_id": vehicle_id,
                "operating_date": operating_date}

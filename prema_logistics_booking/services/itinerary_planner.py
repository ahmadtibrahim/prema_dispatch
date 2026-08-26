"""ItineraryPlanner — time-aware, precedence-constrained milk-run routing.

Deterministic business logic (no AI): builds a precedence graph from
physical pallet movements, simulates onboard pallets/weight segment by
segment, and sequences stops by a feasibility-first weighted score with
limited look-ahead so hard appointment/operating-hours windows are
protected.

All datetimes are Odoo UTC datetimes; windows are 24h floats evaluated
in each stop's timezone.
"""
import math
from datetime import datetime, timedelta

from pytz import timezone as tz

REASON_HARD_WINDOW = "hard_window_protected"
REASON_PRECEDENCE = "pickup_before_delivery"
REASON_HOURS = "facility_operating_hours"
REASON_APPOINTMENT = "appointment_window"
REASON_CAPACITY = "peak_capacity"

WEEKDAY_KEYS = [str(day) for day in range(7)]


def _snapshot_from_rows(rows, day, scope_chain):
    """Per-day window from structured hours rows: scope-specific rows
    (pickup scope for pickup stops, receiving scope for delivery stops) →
    general rows → first row. Closed/no row → None."""
    day_rows = rows.filtered(lambda r, d=day: r.day_of_week == d)
    if not day_rows:
        return None
    chosen = False
    for scope in scope_chain or ():
        chosen = day_rows.filtered(lambda r, s=scope: r.service_scope == s)
        if chosen:
            break
    chosen = chosen or day_rows.filtered(lambda r: r.service_scope == "general")
    chosen = chosen or day_rows[:1]
    row = chosen[0]
    if row.status == "closed":
        return None
    if row.status == "open_24h":
        return [0.0, 24.0]
    return [float(row.open_time or 0.0), float(row.close_time or 24.0)]


def snapshot_facility_hours(env, facility, stop_type="pickup"):
    """Snapshot a canonical facility's OWN hours into a planning snapshot
    {weekday: [open, close] or None}.

    Preference order per day: scope-specific rows (pickup+shipping hours
    for pickup stops, receiving hours for delivery stops) → general rows →
    first row. A day with no rows or status=closed maps to None (closed day).
    Once snapshotted, later facility-hour edits never change historical
    booking planning. (SAVED LOCATION CONSOLIDATION 18.0.13.25.0: the
    legacy snapshot_saved_location_hours bridge was retired — the
    facility record is the hours authority directly.)
    """
    snapshot = {key: None for key in WEEKDAY_KEYS}
    if not facility:
        return snapshot
    scope_chain = ("pickup", "shipping") if stop_type == "pickup" else ("receiving",)
    canonical = facility.facility_hours_ids.filtered(lambda r: r.active)
    if canonical:
        for day in WEEKDAY_KEYS:
            snapshot[day] = _snapshot_from_rows(canonical, day, scope_chain)
    return snapshot


class ItineraryPlanner:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Movement simulation ─────────────────────────────────────────

    def simulate_movements(self, ordered_stop_keys, pallet_movements):
        """Per-stop pallet/weight deltas and the peak onboard.

        pallet_movements: list of dicts {key, pickup_stop_key,
        delivery_stop_keys (list), shared, weight_lbs}.
        A shared pallet leaves the truck only at its FINAL delivery.
        """
        deltas = []
        onboard = 0
        onboard_weight = 0.0
        peak = 0
        active_deliveries = {}
        for movement in pallet_movements:
            deliveries = list(movement.get("delivery_stop_keys") or [])
            active_deliveries[movement["key"]] = deliveries[:]
        for key in ordered_stop_keys:
            pickup = 0
            pickup_w = 0.0
            delivery = 0
            delivery_w = 0.0
            for movement in pallet_movements:
                if movement.get("pickup_stop_key") == key:
                    pickup += 1
                    pickup_w += movement.get("weight_lbs") or 0.0
                    onboard += 1
                    onboard_weight += movement.get("weight_lbs") or 0.0
                remaining = active_deliveries.get(movement["key"]) or []
                if key in remaining:
                    if not movement.get("shared") or remaining.index(key) == len(remaining) - 1:
                        delivery += 1
                        delivery_w += movement.get("weight_lbs") or 0.0
                        onboard -= 1
                        onboard_weight -= movement.get("weight_lbs") or 0.0
            deltas.append({
                "stop_key": key,
                "before": onboard - pickup + delivery,
                "pickup": pickup,
                "delivery": delivery,
                "after": onboard,
                "weight_in": pickup_w,
                "weight_out": delivery_w,
                "weight_before": onboard_weight - pickup_w + delivery_w,
                "weight_after": onboard_weight,
            })
            peak = max(peak, onboard)
        return {"deltas": deltas, "peak": peak, "onboard_after": onboard,
                "weight_after": onboard_weight}

    # ── Time windows ─────────────────────────────────────────────────

    @staticmethod
    def _hours_for(stop, weekday):
        snapshot = stop.get("operating_hours_snapshot") if isinstance(stop, dict) else stop.operating_hours_snapshot
        snapshot = snapshot or {}
        day = snapshot.get(str(weekday)) or snapshot.get(weekday)
        return day if isinstance(day, list) else None

    def effective_window(self, stop, service_dt):
        """Effective [open, close] floats for a stop on a given UTC datetime,
        evaluated in the stop's timezone. Returns (open, close) or None."""
        timezone = stop.get("timezone") if isinstance(stop, dict) else stop.timezone
        local = service_dt.astimezone(tz(timezone or "America/Toronto"))
        hours = self._hours_for(stop, local.weekday())
        if hours is None:
            return None  # closed day
        facility_open, facility_close = hours[0], hours[1]
        window = [facility_open, facility_close]
        timing = stop.get("timing_type") if isinstance(stop, dict) else stop.timing_type
        if timing == "time_window":
            start = stop.get("window_start") if isinstance(stop, dict) else stop.window_start
            end = stop.get("window_end") if isinstance(stop, dict) else stop.window_end
            if start is not None:
                window[0] = max(window[0], start)
            if end is not None:
                window[1] = min(window[1], end)
        elif timing == "exact_appointment":
            appointment = stop.get("appointment_time") if isinstance(stop, dict) else stop.appointment_time
            if appointment is not None:
                window = [appointment, appointment]
        if window[1] < window[0]:
            return None
        return window

    def arrival_plan(self, stop, arrival_dt):
        """(feasible, waiting_minutes, service_start_dt, departure_dt)."""
        window = self.effective_window(stop, arrival_dt)
        if window is None:
            return False, 0, arrival_dt, arrival_dt
        timezone = stop.get("timezone") if isinstance(stop, dict) else stop.timezone
        tz_obj = tz(timezone or "America/Toronto")
        local = arrival_dt.astimezone(tz_obj)
        arrival_hour = local.hour + local.minute / 60.0 + local.second / 3600.0
        if arrival_hour > window[1]:
            return False, 0, arrival_dt, arrival_dt
        waiting = 0.0
        if arrival_hour < window[0]:
            waiting = (window[0] - arrival_hour) * 60.0
        service_start = arrival_dt + timedelta(minutes=waiting)
        service_minutes = stop.get("service_time_minutes", 15) if isinstance(stop, dict) else stop.service_time_minutes
        departure = service_start + timedelta(minutes=service_minutes or 15)
        return True, waiting, service_start, departure

    # ── Travel estimate ──────────────────────────────────────────────

    def _travel_minutes(self, from_stop, to_stop, travel_fn=None):
        """Minutes between two stops. `travel_fn` (injected by callers
        that own a road-routing client, e.g. Google-first) takes priority;
        the built-in straight-line ×1.4 @ 50 km/h estimate is the
        deterministic fallback — identical math everywhere."""
        if travel_fn is not None:
            return travel_fn(from_stop, to_stop)
        lat1 = (from_stop.get("latitude") if isinstance(from_stop, dict) else from_stop.latitude) or 0
        lng1 = (from_stop.get("longitude") if isinstance(from_stop, dict) else from_stop.longitude) or 0
        lat2 = (to_stop.get("latitude") if isinstance(to_stop, dict) else to_stop.latitude) or 0
        lng2 = (to_stop.get("longitude") if isinstance(to_stop, dict) else to_stop.longitude) or 0
        if not (lat1 and lat2):
            return 10.0
        dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
        dy = (lat2 - lat1) * 111.32
        km = math.sqrt(dx * dx + dy * dy) * 1.4
        return km / 50.0 * 60.0  # ~50 km/h average urban

    @staticmethod
    def _cluster_key(stop):
        """Normalized same-city key for consecutive-stop clustering
        (lowercased, whitespace-collapsed). Empty string = no cluster."""
        city = (stop.get("city") or "") if isinstance(stop, dict) else (stop.city or "")
        return " ".join(str(city).strip().lower().split())

    # ── Route adviser ────────────────────────────────────────────────

    def recommend_route(self, stops, pallet_movements, start_dt, vehicle_max=0,
                        start_position=None, travel_fn=None):
        """Deterministic time-aware sequencing.

        stops: ordered dicts/records with stop_key, stop_type, lat/lng,
               timing fields and operating_hours_snapshot. Stops with a
               `city` value are clustered: once a stop in a city is
               chosen, every legal stop in that same city goes next, so
               multi-stop towns (e.g. two Belleville deliveries on the
               Brampton → Belleville → Ottawa run) are never split by a
               far-away stop — the manual-UAT backtracking bug.
        travel_fn: optional callable(from_stop, to_stop) -> minutes.
                   Callers with a road-routing client (Google-first with
                   straight-line fallback) inject it here; the built-in
                   straight-line estimate is used otherwise.
        Returns dict with recommended keys, steps, reasons, feasibility."""
        stops_by_key = {}
        for stop in stops:
            key = stop["stop_key"] if isinstance(stop, dict) else stop.stop_key
            stops_by_key[key] = stop
        pending = set(stops_by_key)
        # Precedence: a delivery is legal only when all pallets destined
        # there are already picked up.
        picked_up = set()
        route = []
        steps = []
        reasons = []
        position = start_position or stops[0]
        current_dt = start_dt
        onboard = 0
        while pending:
            legal = []
            for key in list(pending):
                stop = stops_by_key[key]
                stop_type = stop["stop_type"] if isinstance(stop, dict) else stop.stop_type
                if stop_type == "delivery":
                    pallets_here = [
                        m for m in pallet_movements
                        if key in (m.get("delivery_stop_keys") or [])
                    ]
                    if any(m["pickup_stop_key"] in pending
                           for m in pallets_here):
                        continue  # its pickup has not happened yet
                legal.append(key)
            if not legal:
                return {"feasible": False, "reason": REASON_PRECEDENCE,
                        "recommended": route, "steps": steps,
                        "reasons": reasons}
            def _score_candidate(key):
                """(score, plan) for a legal candidate, or None when the
                stop is not time-feasible right now. plan is the tuple
                consumed after the loop: (feasible, waiting, service_start,
                departure, arrival, travel, stop_type, onboard_after)."""
                stop = stops_by_key[key]
                stop_type = stop["stop_type"] if isinstance(stop, dict) else stop.stop_type
                travel = self._travel_minutes(position, stop, travel_fn)
                arrival = current_dt + timedelta(minutes=travel)
                feasible, waiting, service_start, departure = self.arrival_plan(stop, arrival)
                if not feasible:
                    return None
                # look-ahead: would choosing this make another hard-window
                # stop miss its window?
                hard_risk = 0
                for other_key in legal:
                    if other_key == key:
                        continue
                    other = stops_by_key[other_key]
                    other_timing = other.get("timing_type") if isinstance(other, dict) else other.timing_type
                    if other_timing not in ("time_window", "exact_appointment"):
                        continue
                    other_arrival = departure + timedelta(
                        minutes=self._travel_minutes(stop, other, travel_fn))
                    ok, _, _, _ = self.arrival_plan(other, other_arrival)
                    if not ok:
                        hard_risk += 10000
                if stop_type == "pickup":
                    onboard_after = onboard + 1
                else:
                    onboard_after = onboard - 1
                capacity_risk = 0
                if vehicle_max and onboard_after > vehicle_max:
                    capacity_risk += 50000
                score = (
                    capacity_risk + hard_risk + waiting * 2 + travel
                    + (departure - start_dt).total_seconds() / 3600.0
                )
                plan = (feasible, waiting, service_start, departure,
                        arrival, travel, stop_type, onboard_after)
                return score, plan

            best_key = None
            best_score = None
            best_plan = None
            for key in legal:
                scored = _score_candidate(key)
                if scored is None:
                    continue
                score, plan = scored
                if best_key is None or score < best_score:
                    best_key = key
                    best_score = score
                    best_plan = plan
            # Same-city clustering: when the best stop shares a city with
            # other still-legal stops, restrict the choice to that cluster
            # so same-town stops are served consecutively (never split by
            # a far-away stop). Only time-feasible candidates qualify —
            # window protection is never traded away for clustering.
            if best_key is not None:
                cluster = self._cluster_key(stops_by_key[best_key])
                if cluster:
                    cluster_keys = [
                        key for key in legal
                        if self._cluster_key(stops_by_key[key]) == cluster
                    ]
                    if len(cluster_keys) > 1:
                        cluster_best = None
                        cluster_score = None
                        cluster_plan = None
                        for key in cluster_keys:
                            scored = _score_candidate(key)
                            if scored is None:
                                continue
                            score, plan = scored
                            if cluster_best is None or score < cluster_score:
                                cluster_best = key
                                cluster_score = score
                                cluster_plan = plan
                        if cluster_best is not None and cluster_plan is not None:
                            best_key = cluster_best
                            best_score = cluster_score
                            best_plan = cluster_plan
            if best_key is None:
                return {"feasible": False, "reason": REASON_HOURS,
                        "recommended": route, "steps": steps,
                        "reasons": reasons}
            (_, waiting, service_start, departure, arrival, travel,
             stop_type, onboard_after) = best_plan
            chosen = stops_by_key[best_key]
            name = chosen.get("location_name") or chosen.get("name", "") if isinstance(chosen, dict) else (chosen.location_name or chosen.name or "")
            if stop_type == "pickup":
                picked_up.add(best_key)
                onboard = onboard_after
            else:
                onboard = onboard_after
            steps.append({
                "stop_key": best_key,
                "name": name,
                "stop_type": stop_type,
                "eta": arrival.isoformat(),
                "waiting_minutes": round(waiting, 0),
                "service_start": service_start.isoformat(),
                "departure": departure.isoformat(),
                "onboard_after": onboard,
            })
            reasons.append("%s scheduled for %s" % (
                name, "pickup" if stop_type == "pickup" else "delivery"))
            route.append(best_key)
            pending.remove(best_key)
            position = chosen
            current_dt = departure
        simulation = self.simulate_movements(route, pallet_movements)
        return {
            "feasible": True,
            "recommended": route,
            "steps": steps,
            "reasons": reasons,
            "peak": simulation["peak"],
            "onboard_after": simulation["onboard_after"],
            "finish_eta": steps[-1]["departure"] if steps else start_dt.isoformat(),
        }

    # ── Recommended operational departure (Phase 6) ──────────────────

    DEFAULT_START_BUFFER_MINUTES = 15

    def recommended_departure(self, stops, pallet_movements, corridor_start_dt,
                              start_position=None, travel_fn=None,
                              buffer_minutes=None):
        """Recommended OPERATIONAL start for the actual confirmed freight.

        corridor.start_time stays the recurring/default scheduled time —
        this helper never rewrites it. It works backward from the
        earliest binding constraint:

          * hard stops (exact appointment / time-window start) bound the
            start: the truck must reach them by their window start, so
            start = window_start − cumulative travel − buffer
          * otherwise the first stop's opening time bounds the start: the
            driver should not sit for hours at a closed facility, so
            start ≈ opening − travel − buffer
          * the start never violates any stop's window CLOSE (the route
            must stay feasible), and never goes absurdly earlier than the
            scheduled corridor start (floor = start − 24h)
          * an optional start_position (lat/lng) replaces the first stop
            as the travel origin — e.g. the truck's actual overnight
            position from the previous day's run

        Deterministic, no AI, no hardcoded HOS. Returns
        {recommended_start, feasible, reason, binding_stop,
         start_position_used} — feasible=False only when the route itself
        is infeasible (caller keeps the corridor default then).
        """
        buffer = buffer_minutes if buffer_minutes is not None \
            else self.DEFAULT_START_BUFFER_MINUTES
        # start_position must be a stop-like dict — callers may hand us a
        # raw (lat, lng) tuple (e.g. a truck's live GPS position).
        if start_position and not isinstance(start_position, dict):
            start_position = {
                "latitude": float(start_position[0]),
                "longitude": float(start_position[1]),
            }
        # Odoo Datetimes are naive UTC — normalize so every window
        # comparison below is aware-vs-aware (naive+aware min() raises).
        if corridor_start_dt.tzinfo is None:
            corridor_start_dt = corridor_start_dt.replace(tzinfo=tz("UTC"))
        base = self.recommend_route(
            stops, pallet_movements, corridor_start_dt,
            start_position=start_position, travel_fn=travel_fn,
        )
        if not base.get("feasible"):
            return {
                "recommended_start": corridor_start_dt.isoformat(),
                "feasible": False,
                "reason": base.get("reason") or "route_infeasible",
                "binding_stop": None,
                "start_position_used": start_position is not None,
            }
        by_key = {}
        for stop in stops:
            key = stop["stop_key"] if isinstance(stop, dict) else stop.stop_key
            by_key[key] = stop
        position = start_position
        cum = 0.0
        # Latest start that still reaches every stop before its close.
        latest_start = corridor_start_dt
        # Hard-window start bound (exact appointment / time-window start).
        hard_start = None
        # Ideal start: first stop opened, minus travel and a small buffer.
        ideal_start = None
        ideal_stop = None
        for key in base["recommended"]:
            stop = by_key[key]
            travel = self._travel_minutes(position, stop, travel_fn) \
                if position is not None else 0.0
            cum += travel
            position = stop
            probe = corridor_start_dt + timedelta(minutes=cum)
            window = self.effective_window(stop, probe)
            if window is None:
                continue  # no usable window — not a constraint
            name = stop.get("location_name") or stop.get("name", "") \
                if isinstance(stop, dict) else (stop.location_name or stop.name or "")
            timezone = stop.get("timezone") if isinstance(stop, dict) \
                else (stop.timezone or "America/Toronto")
            tz_obj = tz(timezone or "America/Toronto")
            local_day = probe.astimezone(tz_obj).date()
            def _window_to_utc(hour_float):
                local_dt = datetime(
                    local_day.year, local_day.month, local_day.day,
                    int(hour_float), int((hour_float % 1) * 60),
                )
                return tz_obj.localize(local_dt).astimezone(tz("UTC"))
            open_utc = _window_to_utc(window[0])
            close_utc = _window_to_utc(window[1])
            latest_start = min(latest_start, close_utc - timedelta(minutes=cum))
            timing = stop.get("timing_type") if isinstance(stop, dict) else stop.timing_type
            if timing in ("time_window", "exact_appointment"):
                bound = open_utc - timedelta(minutes=cum + buffer)
                if hard_start is None or bound < hard_start:
                    hard_start = bound
                    hard_binding = name
            if ideal_start is None:
                ideal_start = open_utc - timedelta(minutes=cum + buffer)
                ideal_stop = name
        candidate = hard_start if hard_start is not None else ideal_start
        if candidate is None:
            # No hours/windows at all — nothing to optimize.
            return {
                "recommended_start": corridor_start_dt.isoformat(),
                "feasible": True,
                "reason": "no_constraints",
                "binding_stop": None,
                "start_position_used": start_position is not None,
            }
        floor = corridor_start_dt - timedelta(hours=24)
        recommended = min(candidate, latest_start)
        recommended = max(recommended, floor)
        binding = (hard_binding if hard_start is not None else ideal_stop)
        return {
            "recommended_start": recommended.isoformat(),
            "feasible": True,
            "reason": "hard_constraint" if hard_start is not None else "facility_hours",
            "binding_stop": binding,
            "start_position_used": start_position is not None,
        }

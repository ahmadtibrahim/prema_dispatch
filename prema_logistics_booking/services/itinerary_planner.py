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

    def _travel_minutes(self, from_stop, to_stop):
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

    # ── Route adviser ────────────────────────────────────────────────

    def recommend_route(self, stops, pallet_movements, start_dt, vehicle_max=0,
                        start_position=None):
        """Deterministic time-aware sequencing.

        stops: ordered dicts/records with stop_key, stop_type, lat/lng,
               timing fields and operating_hours_snapshot.
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
            best_key = None
            best_score = None
            for key in legal:
                stop = stops_by_key[key]
                stop_type = stop["stop_type"] if isinstance(stop, dict) else stop.stop_type
                travel = self._travel_minutes(position, stop)
                arrival = current_dt + timedelta(minutes=travel)
                feasible, waiting, service_start, departure = self.arrival_plan(stop, arrival)
                if not feasible:
                    continue
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
                        minutes=self._travel_minutes(stop, other))
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
                if best_key is None or score < best_score:
                    best_key = key
                    best_score = score
                    best_plan = (feasible, waiting, service_start, departure,
                                 arrival, travel, stop_type, onboard_after)
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

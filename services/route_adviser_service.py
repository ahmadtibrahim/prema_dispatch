"""RouteAdviserService — time-aware milk-run route advice for dispatch jobs.

Deterministic planning only (no AI decisions). For a route job it:
- builds the canonical pallet movement list from dispatch items
  (booking pallet links when present, item pickup/delivery stops
  otherwise),
- simulates the CURRENT stop order (distance, drive time, waiting,
  finish ETA, peak onboard, warnings),
- computes the RECOMMENDED order via the ItineraryPlanner (feasibility-
  first, hard windows/deadlines protected, low waiting, limited look-
  ahead),
- validates MANUAL orders: hard-invalid routes are blocked, valid-but-
  worse routes pass with quantified warnings.

Facility hours come from each stop's operating_hours_snapshot (frozen
from the commercial booking stop) or the booking stop's snapshot via
the bridge; stops without any hours data are treated as open 00:00–24:00
so legacy jobs are never blocked retroactively.
"""
import logging
from datetime import datetime, timedelta

from odoo import fields
from pytz import timezone as pytz_timezone

_logger = logging.getLogger(__name__)

DEFAULT_TZ = "America/Toronto"
OPEN_ALL = {str(day): [0.0, 24.0] for day in range(7)}
URBAN_KMH = 50.0


def _lazy_planner():
    from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
        ItineraryPlanner,
    )
    return ItineraryPlanner


def _lazy_snapshot_helper():
    from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
        snapshot_saved_location_hours,
    )
    return snapshot_saved_location_hours


class RouteAdviserService:
    def __init__(self, env):
        self.env = env

    # ── Context extraction ───────────────────────────────────────────

    def stop_dict(self, stop):
        """Dispatch stop → planner stop dict (stable keys = dispatch ids)."""
        hours = stop.operating_hours_snapshot or None
        if not hours and "logistics_booking_stop_id" in stop._fields:
            bstop = stop.logistics_booking_stop_id
            hours = bstop.operating_hours_snapshot if bstop else None
        if not hours:
            hours = OPEN_ALL
        # Map dispatch timing fields to planner timing_type.
        timing = "flexible"
        window_start = window_end = appointment_time = None
        if stop.time_window_type == "window":
            timing = "time_window"
            if stop.earliest_time:
                window_start = self._local_hour_float(stop, stop.earliest_time)
            if stop.latest_time:
                window_end = self._local_hour_float(stop, stop.latest_time)
        elif stop.time_window_type == "exact" and stop.exact_time:
            timing = "exact_appointment"
            appointment_time = self._local_hour_float(stop, stop.exact_time)
        elif stop.time_window_type == "deadline" and stop.deadline_time:
            timing = "deadline"
            window_end = self._local_hour_float(stop, stop.deadline_time)
        return {
            "stop_key": "ds%d" % stop.id,
            "stop_id": stop.id,
            "name": stop.address or stop.saved_location_id.name or "Stop",
            "stop_type": "pickup" if stop.stop_type == "pickup" else "delivery",
            "latitude": stop.latitude or 0.0,
            "longitude": stop.longitude or 0.0,
            "timing_type": timing,
            "window_start": window_start,
            "window_end": window_end,
            "appointment_time": appointment_time,
            "service_time_minutes": stop.service_time_minutes or 15,
            "operating_hours_snapshot": hours,
            "timezone": stop.tz_name or DEFAULT_TZ,
        }

    def _local_hour_float(self, stop, utc_dt):
        local = utc_dt.astimezone(pytz_timezone(stop.tz_name or DEFAULT_TZ))
        return local.hour + local.minute / 60.0

    def movements(self, job):
        """Canonical movements from dispatch items: pickup stop key + one
        delivery key per active allocation; shared pallets keep custody
        until their FINAL allocation."""
        items = job.item_ids.filtered(
            lambda i: i.pickup_stop_id and (i.delivery_stop_id or i.stop_allocation_ids)
        )
        movements = []
        for item in items:
            deliveries = [a.stop_id for a in item.stop_allocation_ids]
            if not deliveries and item.delivery_stop_id:
                deliveries = [item.delivery_stop_id]
            if not deliveries:
                continue
            movements.append({
                "key": "i%d" % item.id,
                "pallet_id": item.id,
                "label": item.name or "Item",
                "weight_lbs": item.weight_lbs or 0.0,
                "shared": item.shared_skid or len(deliveries) > 1,
                "pickup_stop_key": "ds%d" % item.pickup_stop_id.id,
                "delivery_stop_keys": ["ds%d" % s.id for s in deliveries],
            })
        return movements

    def start_context(self, job):
        """(start_dt, start_position) for the route simulation.

        The anchor is the job's operation day at 08:00 local (LTL
        departure convention). scheduled_pickup is used only when it
        falls on that operation day — otherwise planning would anchor
        on the creation timestamp (e.g. a Sunday while facilities are
        closed)."""
        start_dt = None
        if ("operation_date" in job._fields and job.operation_date
                and job.scheduled_pickup
                and fields.Date.to_date(job.scheduled_pickup) == job.operation_date):
            start_dt = job.scheduled_pickup
        if not start_dt and "operation_date" in job._fields and job.operation_date:
            start_dt = datetime.combine(
                job.operation_date, datetime.min.time(),
            ) + timedelta(hours=8)
            local = pytz_timezone(DEFAULT_TZ)
            start_dt = local.localize(start_dt).astimezone(
                pytz_timezone("UTC")).replace(tzinfo=None)
        if not start_dt:
            start_dt = job.scheduled_pickup or datetime.utcnow()
        position = None
        stops = job.stop_ids.sorted("sequence")
        if stops:
            position = stops[0]
        if job.vehicle_id and job.vehicle_id.x_last_location_lat:
            position = job.vehicle_id
        return start_dt, position

    def vehicle_max(self, job):
        """The truck's maximum simultaneous onboard positions — from the
        canonical capacity layout when assigned, then the legacy vehicle
        field. 0 = no assigned truck, no limit known. The job's own
        max_onboard_pallets is its PEAK, not a limit — never use it as
        the capacity bound."""
        vehicle = job.vehicle_id
        if vehicle:
            try:
                from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
                    VehicleCapacityService,
                )
                result = VehicleCapacityService(self.env).evaluate(
                    vehicle, False, 0,
                )
                if result.get("selected_layout"):
                    return result["selected_layout"].get("max_pallets") or 0
            except Exception:
                pass
            if vehicle.x_max_pallets:
                return vehicle.x_max_pallets
        return 0

    def locked_stop_ids(self, job):
        """Stops that cannot be moved: locked or already executed."""
        return job.stop_ids.filtered(
            lambda s: s.route_locked or s.status in ("en_route", "arrived", "completed")
        ).ids

    # ── Simulation over a given order ─────────────────────────────────

    def simulate_order(self, job, ordered_stops, start_dt, start_position):
        planner_cls = _lazy_planner()
        planner = planner_cls(self.env)
        stops_by_key = {s["stop_key"]: s for s in
                        [self.stop_dict(s) for s in ordered_stops]}
        keys = [self.stop_dict(s)["stop_key"] for s in ordered_stops]
        movements = self.movements(job)
        simulation = planner.simulate_movements(keys, movements)
        # Walk the order accumulating travel/waiting/service times.
        steps = []
        position = start_position
        current_dt = start_dt
        total_waiting = 0.0
        total_drive = 0.0
        total_distance_km = 0.0
        feasible = True
        for stop in ordered_stops:
            stop_info = self.stop_dict(stop)
            travel_min = self._travel_minutes(position, stop_info)
            arrival = current_dt + timedelta(minutes=travel_min)
            ok, waiting, service_start, departure = planner.arrival_plan(
                stop_info, arrival,
            )
            if not ok:
                feasible = False
                waiting = 0.0
                service_start = departure = arrival
            total_drive += travel_min
            total_waiting += waiting
            total_distance_km += travel_min / 60.0 * URBAN_KMH
            steps.append({
                "stop_key": stop_info["stop_key"],
                "stop_id": stop.id,
                "name": stop_info["name"],
                "stop_type": stop_info["stop_type"],
                "eta": arrival.isoformat(),
                "waiting_minutes": round(waiting, 0),
                "service_start": service_start.isoformat(),
                "departure": departure.isoformat(),
                "feasible": ok,
            })
            current_dt = departure
            position = stop_info
        # Precedence sanity: any stop with negative onboard (unloading
        # freight that was never picked up) makes the order infeasible.
        if any(d["after"] < 0 for d in simulation["deltas"]):
            feasible = False
        return {
            "feasible": feasible,
            "steps": steps,
            "distance_km": round(total_distance_km, 1),
            "drive_minutes": round(total_drive, 0),
            "waiting_minutes": round(total_waiting, 0),
            "finish_eta": steps[-1]["departure"] if steps else start_dt.isoformat(),
            "peak": simulation["peak"],
            "onboard_after": simulation["onboard_after"],
            "weight_after": simulation["weight_after"],
            "deltas": simulation["deltas"],
        }

    def _travel_minutes(self, from_pos, to_stop):
        """Straight-line estimate ×1.4 road factor @ 50 km/h (urban)."""
        lat1 = lng1 = lat2 = lng2 = None
        if isinstance(from_pos, dict):
            lat1, lng1 = from_pos.get("latitude") or 0, from_pos.get("longitude") or 0
        else:
            lat1 = from_pos.latitude or (from_pos.x_last_location_lat if hasattr(from_pos, "x_last_location_lat") else 0)
            lng1 = from_pos.longitude or (from_pos.x_last_location_lng if hasattr(from_pos, "x_last_location_lng") else 0)
        lat2 = to_stop.get("latitude") or 0
        lng2 = to_stop.get("longitude") or 0
        if not (lat1 and lat2):
            return 10.0
        import math
        dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
        dy = (lat2 - lat1) * 111.32
        km = math.sqrt(dx * dx + dy * dy) * 1.4
        return km / URBAN_KMH * 60.0

    # ── Adviser report ────────────────────────────────────────────────

    def adviser_report(self, job):
        """{current, recommended, comparison, warnings, feasible}."""
        start_dt, start_position = self.start_context(job)
        ordered = job.stop_ids.sorted("sequence")
        current = self.simulate_order(job, ordered, start_dt, start_position)

        planner_cls = _lazy_planner()
        planner = planner_cls(self.env)
        stop_dicts = [self.stop_dict(s) for s in ordered]
        movements = self.movements(job)
        vehicle_max = self.vehicle_max(job)
        start_pos_dict = None
        if start_position is not None:
            if isinstance(start_position, dict):
                start_pos_dict = start_position
            else:
                lat = start_position.latitude or 0.0
                lng = start_position.longitude or 0.0
                if hasattr(start_position, "x_last_location_lat"):
                    lat = start_position.x_last_location_lat or lat
                    lng = start_position.x_last_location_lng or lng
                start_pos_dict = {
                    "stop_key": "start", "name": "Start",
                    "latitude": lat, "longitude": lng,
                }
        result = planner.recommend_route(
            stop_dicts, movements, start_dt,
            vehicle_max=vehicle_max,
            start_position=start_pos_dict,
        )
        warnings = []
        recommended = {}
        if result.get("feasible"):
            recommended_order = [
                ordered.filtered(lambda s, k=key: ("ds%d" % s.id) == k)
                for key in result["recommended"]
            ]
            recommended = self.simulate_order(
                job, recommended_order, start_dt, start_position,
            )
        else:
            warnings.append(result.get("reason") or "No feasible route")

        comparison = {
            "added_km": round(max(0.0, recommended.get("distance_km", current["distance_km"]) - current["distance_km"]), 1) if recommended else 0.0,
            "added_minutes": round(max(0.0, recommended.get("drive_minutes", current["drive_minutes"]) - current["drive_minutes"]), 0) if recommended else 0.0,
            "added_waiting": round(max(0.0, recommended.get("waiting_minutes", current["waiting_minutes"]) - current["waiting_minutes"]), 0) if recommended else 0.0,
        }
        if recommended and not current["feasible"]:
            warnings.append("Current order is infeasible — apply the recommended route.")
        if recommended and current["peak"] > (vehicle_max or current["peak"]):
            warnings.append("Current order exceeds vehicle capacity (%s peak on %s positions)." % (
                current["peak"], vehicle_max,
            ))
        return {
            "current": current,
            "recommended": recommended or {},
            "recommended_keys": result.get("recommended") or [],
            "reasons": result.get("reasons") or [],
            "comparison": comparison,
            "warnings": warnings,
            "feasible": bool(result.get("feasible")),
            "vehicle_max": vehicle_max,
        }

    def apply_recommended_route(self, job):
        """Apply the recommended order, preserving completed/locked stops
        in their exact slots (mid-day reoptimization never rewrites
        already-driven history)."""
        report = self.adviser_report(job)
        if not report["feasible"] or not report["recommended_keys"]:
            return {
                "success": False,
                "error": "No feasible recommended route available.",
            }
        ordered = job.stop_ids.sorted("sequence")
        locked_ids = set(self.locked_stop_ids(job))
        # Stable merge: walk the current order; locked stops keep their
        # slots, unlocked stops are filled from the recommendation.
        recommended_ids = []
        for key in report["recommended_keys"]:
            stop = ordered.filtered(lambda s: ("ds%d" % s.id) == key)
            if stop and stop.id not in locked_ids:
                recommended_ids.append(stop.id)
        recommended_iter = iter(recommended_ids)
        new_ids = []
        for stop in ordered:
            if stop.id in locked_ids:
                new_ids.append(stop.id)
            else:
                try:
                    new_ids.append(next(recommended_iter))
                except StopIteration:
                    new_ids.append(stop.id)
        sequence = 10
        for stop_id in new_ids:
            stop = ordered.filtered(lambda s: s.id == stop_id)
            if stop:
                stop.write({"sequence": sequence})
                sequence += 10
        return {"success": True, "applied": len(new_ids)}

    # ── Manual route validation ───────────────────────────────────────

    def validate_manual_route(self, job, ordered_stop_ids):
        """Hard-invalid routes are BLOCKED; valid-but-worse routes return
        quantified warnings (added km/minutes/waiting)."""
        ordered = job.stop_ids.browse(ordered_stop_ids)
        errors = []
        warnings = []
        ordered_by_id = {s.id: s for s in ordered}
        missing = set(job.stop_ids.ids) - set(ordered_by_id)
        if missing:
            errors.append("Route is missing %d stop(s)." % len(missing))
            return {"valid": False, "errors": errors, "warnings": []}

        # Completed/locked history must never move.
        locked_ids = set(self.locked_stop_ids(job))
        current_order_ids = job.stop_ids.sorted("sequence").ids
        new_order_ids = list(ordered.ids)
        moved_diff = [
            sid for sid in locked_ids
            if sid in new_order_ids
            and current_order_ids.index(sid) != new_order_ids.index(sid)
        ]
        if moved_diff:
            errors.append("Completed or locked stops cannot be re-sequenced: %s" % moved_diff)

        # Pickup-before-delivery + capacity + negative onboard.
        planner_cls = _lazy_planner()
        planner = planner_cls(self.env)
        movements = self.movements(job)
        keys = ["ds%d" % s.id for s in ordered]
        simulation = planner.simulate_movements(keys, movements)
        if simulation["onboard_after"] < 0:
            errors.append("Negative onboard pallets after the final stop — a delivery happens before its pickup.")
        for delta in simulation["deltas"]:
            if delta["after"] < 0:
                errors.append("Stop %s would unload pallets that are not onboard." % delta["stop_key"])
        vehicle_max = self.vehicle_max(job)
        if vehicle_max and simulation["peak"] > vehicle_max:
            errors.append("Peak onboard %s exceeds the vehicle's %s pallet positions." % (
                simulation["peak"], vehicle_max,
            ))

        # Time feasibility: hard appointments, closed facilities.
        start_dt, start_position = self.start_context(job)
        simulated = self.simulate_order(job, ordered, start_dt, start_position)
        overrides = {
            o.stop_id.id for o in self.env["prema.dispatch.hours.override"].search([
                ("job_id", "=", job.id), ("active", "=", True),
            ])
        }
        for step in simulated["steps"]:
            stop = ordered_by_id[step["stop_id"]]
            if step["feasible"]:
                continue
            if stop.id in overrides:
                warnings.append("Stop %s serviced outside facility hours under authorized override." % step["name"])
                continue
            has_booking_hours = bool(stop.operating_hours_snapshot) or (
                "logistics_booking_stop_id" in stop._fields
                and stop.logistics_booking_stop_id
            )
            if stop.time_window_type in ("window", "exact", "deadline"):
                errors.append("Impossible hard appointment for stop %s." % step["name"])
            elif has_booking_hours:
                errors.append("Closed facility with no valid window for stop %s." % step["name"])
            else:
                warnings.append("Stop %s falls outside facility hours." % step["name"])

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings}

        # Valid: quantify the delta versus the current route.
        current = self.simulate_order(job, job.stop_ids.sorted("sequence"), start_dt, start_position)
        warnings.append(
            "Manual order adds %s km / %s min / %s min waiting vs current order." % (
                round(max(0.0, simulated["distance_km"] - current["distance_km"]), 1),
                round(max(0.0, simulated["drive_minutes"] - current["drive_minutes"]), 0),
                round(max(0.0, simulated["waiting_minutes"] - current["waiting_minutes"]), 0),
            ),
        )
        return {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "metrics": {
                "distance_km": simulated["distance_km"],
                "drive_minutes": simulated["drive_minutes"],
                "waiting_minutes": simulated["waiting_minutes"],
                "finish_eta": simulated["finish_eta"],
                "peak": simulated["peak"],
            },
        }

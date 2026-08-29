"""
Route optimization and mid-day insertion.
"""
import logging
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class DispatchOptimizationService:
    def __init__(self, env):
        self.env = env

    def rank_trucks_for_job(self, job_id):
        """
        Rank all available trucks for a specific dispatch job.
        Returns list of {truck_id, name, score, reason, distance_km, eta_pickup}.
        """
        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService

        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            return []

        check_date = (job.scheduled_pickup or datetime.utcnow()).date()
        avail_svc = DispatchAvailabilityService(self.env)
        route_svc = DispatchRouteService(self.env)

        pickup_addr = None
        pickup_stops = job.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")
        if pickup_stops:
            first = pickup_stops[0]
            if first.latitude and first.longitude:
                pickup_addr = (first.latitude, first.longitude)
            else:
                pickup_addr = first.address

        trucks = avail_svc.get_truck_day_schedule(check_date)
        scored = []

        for t in trucks:
            if job.requires_reefer and not t["has_reefer"]:
                continue
            if job.requires_liftgate and not t["has_liftgate"]:
                continue
            if job.max_onboard_pallets and t["pallet_capacity"] and job.max_onboard_pallets > t["pallet_capacity"]:
                continue

            dist_km = 0
            if pickup_addr and (t["lat"] or t["lng"]):
                legs = route_svc.get_sequential_travel(
                    [(t["lat"], t["lng"]), pickup_addr]
                )
                dist_km = legs[0]["distance_km"] if legs else 0

            # Score: lower is better
            # Penalise: distance to pickup, existing job load, GPS staleness
            base_score = dist_km
            if t["status"] == "partial":
                base_score += 20  # penalty for partially busy truck
            if t.get("gps_age_minutes") and t["gps_age_minutes"] > 60:
                base_score += 10  # penalty for stale GPS

            scored.append({
                "truck_id": t["truck_id"],
                "name": t["name"],
                "driver_name": t["driver_name"],
                "score": round(base_score, 1),
                "status": t["status"],
                "distance_to_pickup_km": dist_km,
                "available_from": t["available_from"],
                "available_capacity": t["available_capacity"],
                "has_reefer": t["has_reefer"],
                "has_liftgate": t["has_liftgate"],
                "existing_jobs": len(t["jobs"]),
            })

        scored.sort(key=lambda x: x["score"])
        return scored

    def optimize_route(self, job_id, inserted_job_id=None):
        """
        Re-order pending stops on a job to minimize total drive time.

        Canonical path (jobs whose stops carry pallet movements — the
        Booking-185-style milk runs): delegates to RouteAdviserService,
        which feeds the ItineraryPlanner — the single sequencing engine —
        with Google road routing (straight-line fallback), movement
        precedence, facility windows, capacity and same-city clustering
        (the Belleville-after-Ottawa backtracking fix).

        Legacy path (jobs without item movements): nearest-neighbour
        with the same same-city clustering, per-candidate point-to-point
        distances (never chained-leg misreads), linked_load_group
        precedence and the urgent-deadline override.

        Rules:
        - Completed/arrived stops are locked in place.
        - route_locked stops keep their position.
        - Pickup must always precede its linked drop-offs.
        - Returns dict: {new_order, added_distance_km, added_minutes,
          stop_count, missed_deadlines}
        """
        from odoo.addons.prema_dispatch.services.route_adviser_service import (
            RouteAdviserService,
        )
        from odoo.addons.prema_dispatch.services.route_service import (
            DispatchRouteService,
        )

        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists() or not job.stop_ids:
            return {}

        # Canonical path: movement-bearing jobs → planner-backed adviser.
        adviser = RouteAdviserService(self.env)
        if adviser.movements(job):
            delegated = self._delegate_route_to_adviser(job, adviser)
            if delegated is not None:
                return delegated

        route_svc = DispatchRouteService(self.env)
        all_stops = list(job.stop_ids.sorted("sequence"))

        # Locked stops (completed, arrived, route_locked)
        locked = [s for s in all_stops if s.status in ("completed", "arrived", "en_route") or s.route_locked]
        pending = [s for s in all_stops if s not in locked]

        if len(pending) < 2:
            return {"new_order": [s.id for s in all_stops], "added_distance_km": 0, "added_minutes": 0}

        # Build group constraints: for each linked_load_group, pickup must come before dropoffs
        groups = {}
        for s in pending:
            if s.linked_load_group:
                groups.setdefault(s.linked_load_group, {"pickups": [], "dropoffs": []})
                if s.stop_type == "pickup":
                    groups[s.linked_load_group]["pickups"].append(s)
                else:
                    groups[s.linked_load_group]["dropoffs"].append(s)

        # Nearest-neighbour heuristic respecting group constraints
        optimized = list(locked)
        remaining = list(pending)
        current_loc = None

        if locked:
            last_locked = locked[-1]
            if last_locked.latitude and last_locked.longitude:
                current_loc = (last_locked.latitude, last_locked.longitude)
            elif last_locked.address:
                current_loc = last_locked.address
        elif job.vehicle_id:
            if job.vehicle_id.x_last_location_lat:
                current_loc = (job.vehicle_id.x_last_location_lat, job.vehicle_id.x_last_location_lng)

        def _can_add(stop, already_added):
            """Check group constraint: if this is a dropoff, its pickup must already be added."""
            if stop.linked_load_group and stop.stop_type in ("dropoff", "return"):
                g = groups.get(stop.linked_load_group, {})
                for pickup in g.get("pickups", []):
                    if pickup not in already_added:
                        return False
            return True

        def _legs_to(current_loc, stop):
            """Point-to-point travel from current_loc to one candidate.
            Google road routing primary (region=ca), straight-line
            fallback inside the route service — never chained legs."""
            dest = (stop.latitude, stop.longitude) if stop.latitude else (stop.address or "")
            legs = route_svc.get_sequential_travel([current_loc, dest])
            distance = legs[0]["distance_km"] if legs else 0.0
            drive_min = legs[0].get("drive_minutes") if legs else 0
            if not drive_min:
                drive_min = distance / 0.8  # rough: 0.8 km/min avg city speed
            return distance, drive_min

        # Running clock used to decide when a deadline is genuinely at risk —
        # started from the last locked stop's actual/estimated departure, or
        # now if nothing is locked yet.
        clock = datetime.utcnow()
        if locked:
            last_locked = locked[-1]
            clock = last_locked.actual_departure_time or last_locked.estimated_departure or clock

        already_added = set(locked)
        while remaining:
            candidates = [s for s in remaining if _can_add(s, already_added)]
            if not candidates:
                # Fallback: take first remaining to avoid infinite loop
                candidates = remaining[:1]

            if current_loc:
                distances = []
                drive_minutes = []
                for cand in candidates:
                    distance, drive_min = _legs_to(current_loc, cand)
                    distances.append(distance)
                    drive_minutes.append(drive_min)
                best_idx = distances.index(min(distances))
                chosen = candidates[best_idx]
                chosen_drive_min = drive_minutes[best_idx]

                # Same-city clustering: when the best stop shares a city
                # with other still-legal candidates, the nearest same-city
                # stop goes next — same-town stops are served consecutively
                # (the Belleville-after-Ottawa backtracking fix also applies
                # to legacy jobs without item movements).
                chosen_city = adviser.stop_city(chosen)
                if chosen_city:
                    same_city = [
                        s for s in remaining
                        if s is not chosen and adviser.stop_city(s) == chosen_city
                        and _can_add(s, already_added)
                    ]
                    if same_city:
                        city_distances = []
                        for cand in same_city:
                            distance, drive_min = _legs_to(current_loc, cand)
                            city_distances.append((distance, drive_min, cand))
                        if city_distances:
                            city_distances.sort(key=lambda x: x[0])
                            chosen = city_distances[0][2]
                            chosen_drive_min = city_distances[0][1]

                # Deadline override: if some OTHER candidate has a hard
                # deadline that arriving-after-the-nearest-stop-first would
                # blow, and going there now would still make it, prefer it —
                # a simple "urgent stop jumps the queue" rule rather than a
                # full constraint solver. Deadlines always beat clustering.
                for i, cand in enumerate(candidates):
                    if cand is chosen or not cand.hard_deadline:
                        continue
                    deadline = cand.deadline_time or cand.latest_time
                    if not deadline:
                        continue
                    eta_if_now = clock + timedelta(minutes=drive_minutes[i])
                    eta_if_after_chosen = clock + timedelta(minutes=chosen_drive_min + (chosen.service_time_minutes or 15) + drive_minutes[i])
                    if eta_if_after_chosen > deadline and eta_if_now <= deadline:
                        chosen = cand
                        chosen_drive_min = drive_minutes[i]
                        break
            else:
                chosen = candidates[0]
                chosen_drive_min = 0

            optimized.append(chosen)
            already_added.add(chosen)
            remaining.remove(chosen)
            clock = clock + timedelta(minutes=chosen_drive_min + (chosen.service_time_minutes or 15))

            if chosen.latitude and chosen.longitude:
                current_loc = (chosen.latitude, chosen.longitude)
            elif chosen.address:
                current_loc = chosen.address

        # Compute original vs new sequence distance
        def _route_distance(stop_list):
            locs = []
            for s in stop_list:
                if s.latitude and s.longitude:
                    locs.append((s.latitude, s.longitude))
                elif s.address:
                    locs.append(s.address)
            if len(locs) < 2:
                return 0
            legs = route_svc.get_sequential_travel(locs)
            return sum(l["distance_km"] for l in legs)

        orig_dist = _route_distance(all_stops)
        new_dist = _route_distance(optimized)
        added_km = round(new_dist - orig_dist, 1)

        # Write new sequences
        for i, stop in enumerate(optimized):
            stop.write({"sequence": (i + 1) * 10})

        # Re-estimate route ETAs
        route_svc.estimate_job_route(job)

        # Deadline check against the now-updated ETAs — surfaced to the
        # dispatcher as an "Impossible Assignment" style warning rather than
        # silently accepting a route that can't make a hard deadline.
        missed_deadlines = []
        for stop in optimized:
            if not stop.hard_deadline or stop.status in ("completed", "skipped", "cancelled"):
                continue
            deadline = stop.deadline_time or stop.latest_time
            if deadline and stop.estimated_arrival and stop.estimated_arrival > deadline:
                late_by = int((stop.estimated_arrival - deadline).total_seconds() / 60)
                missed_deadlines.append({
                    "stop_id": stop.id,
                    "stop_name": stop.name,
                    "deadline": deadline.isoformat(),
                    "estimated_arrival": stop.estimated_arrival.isoformat(),
                    "late_by_minutes": late_by,
                })
        if missed_deadlines:
            job.write({"feasibility_status": "risky"})

        return {
            "new_order": [s.id for s in optimized],
            "added_distance_km": added_km,
            "added_minutes": round(added_km / 0.8),  # rough: 0.8 km/min avg city speed
            "stop_count": len(optimized),
            "missed_deadlines": missed_deadlines,
        }

    def _delegate_route_to_adviser(self, job, adviser):
        """Canonical route optimization: the planner-backed adviser
        computes and APPLIES the recommended order (movement precedence,
        facility windows, capacity, same-city clustering, Google road
        routing). Returns the optimize_route() result shape, or None when
        the adviser has no feasible recommendation (caller falls back to
        the legacy nearest-neighbour)."""
        report = adviser.adviser_report(job)
        if not report["feasible"] or not report["recommended_keys"]:
            return None
        applied = adviser.apply_recommended_route(job)
        if not applied.get("success"):
            return None

        ordered = job.stop_ids.sorted("sequence")
        recommended_steps = {
            s["stop_id"]: s
            for s in (report.get("recommended") or {}).get("steps", [])
        }
        missed_deadlines = []
        for stop in ordered:
            if not stop.hard_deadline or stop.status in ("completed", "skipped", "cancelled"):
                continue
            deadline = stop.deadline_time or stop.latest_time
            step = recommended_steps.get(stop.id)
            if not deadline or not step or not step.get("eta"):
                continue
            eta = datetime.fromisoformat(step["eta"])
            if eta > deadline:
                missed_deadlines.append({
                    "stop_id": stop.id,
                    "stop_name": stop.name,
                    "deadline": deadline.isoformat(),
                    "estimated_arrival": eta.isoformat(),
                    "late_by_minutes": int((eta - deadline).total_seconds() / 60),
                })
        if missed_deadlines:
            job.write({"feasibility_status": "risky"})

        recommended = report.get("recommended") or {}
        current = report.get("current") or {}
        added_km = round(
            recommended.get("distance_km", 0.0) - current.get("distance_km", 0.0), 1)
        added_min = round(
            recommended.get("drive_minutes", 0) - current.get("drive_minutes", 0), 0)
        return {
            "new_order": [s.id for s in ordered],
            "added_distance_km": added_km,
            "added_minutes": added_min,
            "stop_count": len(ordered),
            "missed_deadlines": missed_deadlines,
            "basis": "planner",
        }

    @staticmethod
    def _find_cross_dock_hub(chains):
        """A chain that visits the same Allow-Cross-Dock saved location 2+
        times is a 'hub' for this truck/day — the natural shape of a
        repeat-pickup route (pickup part of the load, deliver nearby,
        return for the rest). Returns the first such chain found, with the
        location and the index of its first/second visit, or None."""
        for chain in chains:
            visits = {}
            for idx, s in enumerate(chain["stops"]):
                loc = s.saved_location_id
                if loc and loc.allow_cross_dock:
                    visits.setdefault(loc.id, []).append(idx)
            for loc_id, idxs in visits.items():
                if len(idxs) >= 2:
                    return {
                        "chain": chain, "location_id": loc_id,
                        "first_idx": idxs[0], "second_idx": idxs[1],
                    }
        return None

    @staticmethod
    def _build_cross_dock_merge(chains, hub):
        """Order stops so that every OTHER job's freight is temporarily set
        down at the hub location before the hub chain's own round-trip work,
        and reloaded once the hub chain returns to it — instead of being
        carried the whole time or forcing that other job's own (possibly
        far) delivery to happen before the local hub work.

        Returns a list of (job, stop_or_None, cross_dock_info_or_None)
        tuples. A None stop with cross_dock_info set means "materialize a
        new stop here" — nothing is written by this method itself.
        """
        hub_chain = hub["chain"]
        hub_stops = hub_chain["stops"]
        carriers = [c for c in chains if c is not hub_chain]

        order = []
        # 1. Every carrier's own pickup first.
        for c in carriers:
            order.append((c["job"], c["stops"][0], None))
        # 2. Temporarily drop each carrier's freight at the hub, before the
        #    hub chain's own work begins.
        for c in carriers:
            pickup = c["stops"][0]
            if pickup.stop_type != "pickup" or not (pickup.pallets_in or 0):
                continue
            order.append((c["job"], None, {
                "type": "drop", "pallets": pickup.pallets_in, "origin_stop": pickup,
            }))
        # 3. Hub chain's own stops through its second hub visit (its first
        #    pickup round + whatever it delivers in between).
        for s in hub_stops[:hub["second_idx"] + 1]:
            order.append((hub_chain["job"], s, None))
        # 4. Reload each carrier's freight — the hub chain is back at the
        #    same location for its second round anyway.
        for c in carriers:
            pickup = c["stops"][0]
            if pickup.stop_type != "pickup" or not (pickup.pallets_in or 0):
                continue
            order.append((c["job"], None, {
                "type": "pickup", "pallets": pickup.pallets_in, "origin_stop": pickup,
            }))
        # 5. Hub chain's remaining stops.
        for s in hub_stops[hub["second_idx"] + 1:]:
            order.append((hub_chain["job"], s, None))
        # 6. Each carrier's own remaining stops (its real delivery(ies)) —
        #    naturally last, same as a dispatcher saving the far drop for
        #    the end of the route.
        for c in carriers:
            for s in c["stops"][1:]:
                order.append((c["job"], s, None))

        return order

    def suggest_consolidated_route(self, vehicle_id, date_str):
        """
        Suggest (but do not write) a combined stop order across ALL non-
        completed jobs sharing this truck on this day. optimize_route()
        above only ever looks at one job at a time — it has no way to
        interleave a second job's stops (the inserted_job_id param on it is
        declared but never used anywhere). This treats each job's own stop
        order as a fixed chain (so an existing multi-round pickup pattern
        within a job is preserved).

        If one chain is a cross-dock "hub" (visits the same Allow-Cross-Dock
        location twice — see _find_cross_dock_hub), other chains' freight is
        interleaved through it via temporary drop/reload legs
        (_build_cross_dock_merge) instead of being carried the whole route
        or forcing a rigid job-by-job order. Otherwise, chains are merged by
        picking whichever job's next stop is geographically nearest at each
        step — same nearest-neighbour idea as optimize_route, just across
        jobs too.

        Returns a proposal for a wizard (or Auto Plan) to apply; nothing is
        written until then.
        """
        from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
        from datetime import date as _date, timedelta as _timedelta

        try:
            check_date = _date.fromisoformat(date_str)
        except (TypeError, ValueError):
            return {"error": "Invalid date."}

        jobs = self.env["prema.dispatch.job"].search([
            ("vehicle_id", "=", vehicle_id),
            ("stage_id.is_completed", "=", False),
            ("stage_id.is_cancelled", "=", False),
        ]).filtered(lambda j: j.scheduled_pickup and j.scheduled_pickup.date() == check_date)

        # FTL exclusivity: a job whose commercial booking is Full-Truckload
        # (sold as FTL, or auto-priced FTL at the corridor threshold — e.g.
        # corridor 9: >= 10 pallets with ftl_behavior "auto_price") is a
        # dedicated direct service. Its stops must never be interleaved
        # with other customers' freight on the same truck; only true LTL
        # chains participate in the merged proposal.
        ltl_jobs = []
        excluded_ftl = []
        for job in jobs:
            # prema_logistics_booking adds logistics_booking_id to
            # prema.dispatch.job and loads after dispatch in the module
            # graph (booking depends on dispatch) — at_install tests can
            # run before the field exists. Same guard as dispatch_stop.py.
            booking = job.logistics_booking_id if "logistics_booking_id" in job._fields else False
            is_ftl = bool(booking and booking.shipment_type == "ftl")
            if not is_ftl and booking and booking.corridor_id:
                corridor = booking.corridor_id
                is_ftl = bool(
                    corridor.enable_ftl
                    and corridor.ftl_behavior == "auto_price"
                    and corridor.ftl_threshold_pallets
                    and booking.pallets >= corridor.ftl_threshold_pallets
                )
            if is_ftl:
                excluded_ftl.append(job)
            else:
                ltl_jobs.append(job)
        if excluded_ftl:
            _logger.info(
                "Consolidation excluded %d FTL job(s) from the shared-route "
                "proposal for vehicle %s on %s: %s",
                len(excluded_ftl), vehicle_id, date_str,
                ", ".join(j.name for j in excluded_ftl),
            )

        chains = []
        locked_prefix = []
        for job in ltl_jobs:
            ordered_stops = job.stop_ids.filtered(
                lambda s: not s.planning_only and s.stop_type not in ("cross_dock_drop", "cross_dock_pickup")
            ).sorted("sequence")
            locked_prefix.extend([s for s in ordered_stops if s.status in ("completed", "arrived", "en_route")])
            stops = [s for s in ordered_stops if s.status not in ("completed", "cancelled", "skipped", "arrived", "en_route")]
            if stops:
                chains.append({"job": job, "stops": stops, "idx": 0})

        if len(chains) < 2:
            return {"error": "Only one job (or none) has pending stops on this truck for this day — nothing to consolidate."}

        route_svc = DispatchRouteService(self.env)
        vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
        current_loc = (vehicle.x_last_location_lat, vehicle.x_last_location_lng) \
            if vehicle.x_last_location_lat else None
        clock = min((j.scheduled_pickup for j in jobs if j.scheduled_pickup), default=datetime.utcnow())

        if locked_prefix:
            locked_prefix = sorted(
                locked_prefix,
                key=lambda s: (
                    s.actual_departure_time or s.actual_arrival_time or s.scheduled_time or s.job_id.scheduled_pickup or datetime.utcnow(),
                    s.sequence or 0,
                    s.id,
                ),
            )
            last_locked = locked_prefix[-1]
            if last_locked.latitude and last_locked.longitude:
                current_loc = (last_locked.latitude, last_locked.longitude)
            elif last_locked.address:
                current_loc = last_locked.address
            clock = (
                last_locked.actual_departure_time
                or last_locked.actual_arrival_time
                or last_locked.estimated_departure
                or last_locked.scheduled_time
                or clock
            )

        hub = self._find_cross_dock_hub(chains)
        raw_order = self._build_cross_dock_merge(chains, hub) if hub else None

        merged = []
        if raw_order is not None:
            # Fixed order already decided — just walk it to compute ETAs.
            for job, stop, cross_dock in raw_order:
                dest = None
                if stop is not None:
                    dest = (stop.latitude, stop.longitude) if stop.latitude else (stop.address or None)
                elif cross_dock is not None:
                    loc = self.env["prema.dispatch.location"].browse(hub["location_id"])
                    dest = (loc.pin_lat, loc.pin_lng) if loc.pin_lat else (loc.address or None)
                drive_min = 0
                if current_loc and dest:
                    legs = route_svc.get_sequential_travel([current_loc, dest])
                    drive_min = legs[0].get("drive_minutes") if legs else 0
                clock = clock + _timedelta(minutes=drive_min or 0)
                merged.append({"job": job, "stop": stop, "eta": clock, "cross_dock": cross_dock})
                svc_min = stop.service_time_minutes if stop is not None else 10
                clock = clock + _timedelta(minutes=svc_min or 15)
                if dest:
                    current_loc = dest
        else:
            while any(c["idx"] < len(c["stops"]) for c in chains):
                candidates = [(c, c["stops"][c["idx"]]) for c in chains if c["idx"] < len(c["stops"])]
                drive_min = 0
                if current_loc and len(candidates) > 1:
                    # get_sequential_travel returns CHAINED leg distances (A->B, B->C, ...),
                    # not independent point-to-point distances from a shared origin — so each
                    # candidate must be compared against current_loc in its own 2-point call.
                    distances, drive_mins = [], []
                    for _, s in candidates:
                        dest = (s.latitude, s.longitude) if s.latitude else (s.address or "")
                        legs = route_svc.get_sequential_travel([current_loc, dest])
                        distances.append(legs[0]["distance_km"] if legs else 0.0)
                        drive_mins.append(legs[0].get("drive_minutes") if legs else 0)
                    best_i = distances.index(min(distances))
                    drive_min = drive_mins[best_i] or (distances[best_i] / 0.8)
                else:
                    best_i = 0
                chosen_chain, chosen_stop = candidates[best_i]
                clock = clock + _timedelta(minutes=drive_min)
                merged.append({"job": chosen_chain["job"], "stop": chosen_stop, "eta": clock, "cross_dock": None})
                clock = clock + _timedelta(minutes=chosen_stop.service_time_minutes or 15)
                chosen_chain["idx"] += 1
                current_loc = (chosen_stop.latitude, chosen_stop.longitude) if chosen_stop.latitude else (chosen_stop.address or current_loc)

        suggested_order = []
        for m in merged:
            if m["stop"] is not None:
                suggested_order.append({
                    "stop_id": m["stop"].id,
                    "job_id": m["job"].id,
                    "job_name": m["job"].name,
                    "stop_type": m["stop"].stop_type,
                    "address": m["stop"].address,
                    "pallets_in": m["stop"].pallets_in,
                    "pallets_out": m["stop"].pallets_out,
                    "eta": m["eta"],
                    "cross_dock_type": False,
                    "location_id": False,
                    "pallets": 0,
                    "origin_stop_name": "",
                    "origin_stop_id": False,
                })
            else:
                cd = m["cross_dock"]
                loc = self.env["prema.dispatch.location"].browse(hub["location_id"])
                is_drop = cd["type"] == "drop"
                suggested_order.append({
                    "stop_id": False,
                    "job_id": m["job"].id,
                    "job_name": m["job"].name,
                    "stop_type": "cross_dock_drop" if is_drop else "cross_dock_pickup",
                    "address": loc.address,
                    "pallets_in": 0 if is_drop else cd["pallets"],
                    "pallets_out": cd["pallets"] if is_drop else 0,
                    "eta": m["eta"],
                    "cross_dock_type": cd["type"],
                    "location_id": loc.id,
                    "pallets": cd["pallets"],
                    "origin_stop_name": cd["origin_stop"].name or cd["origin_stop"].address or "",
                    "origin_stop_id": cd["origin_stop"].id,
                })

        return {
            "vehicle_id": vehicle_id,
            "date": date_str,
            "suggested_order": suggested_order,
        }

    def apply_consolidated_route(self, vehicle_id, date_str):
        """Compute suggest_consolidated_route() and write it immediately —
        used by Auto Plan, which must hand the dispatcher an actually
        driveable route (including cross-dock legs) in one pass rather than
        always requiring the separate manual "Consolidate" wizard step.
        Real stops are resequenced/re-timed; cross-dock legs are created.
        """
        result = self.suggest_consolidated_route(vehicle_id, date_str)
        if result.get("error"):
            return result

        Stop = self.env["prema.dispatch.stop"]
        cross_dock_legs = 0
        for i, entry in enumerate(result["suggested_order"]):
            seq = (i + 1) * 10
            if entry["stop_id"]:
                stop = Stop.browse(entry["stop_id"])
                # Unified ETA engine (Section C): the consolidation's walk
                # is an ESTIMATE — write the ETA fields, never the schedule.
                # scheduled_time stays the schedule authority (finalizer /
                # dispatcher / driver edit); the optimizer only seeds it
                # when nothing else set it.
                eta_vals = {
                    "sequence": seq,
                    "travel_arrival_at": entry["eta"],
                    "facility_service_start_at": entry["eta"],
                    "planned_departure_at": entry["eta"],
                    "customer_eta_at": entry["eta"],
                }
                if not stop.scheduled_time:
                    eta_vals["scheduled_time"] = entry["eta"]
                stop.write(eta_vals)
            else:
                # Skip if a prior Auto Plan / consolidation pass already
                # created this exact leg (same job, type, location) — avoids
                # piling up duplicate cross-dock stops on repeated runs.
                base_domain = [
                    ("job_id", "=", entry["job_id"]),
                    ("stop_type", "=", entry["stop_type"]),
                    ("saved_location_id", "=", entry["location_id"]),
                    ("status", "not in", ("cancelled",)),
                ]
                origin_stop_id = entry.get("origin_stop_id") or False
                existing = Stop.search(
                    base_domain + [("cross_dock_origin_stop_id", "=", origin_stop_id)],
                    limit=1,
                )
                if not existing and origin_stop_id:
                    existing = Stop.search(
                        base_domain + [("cross_dock_origin_stop_id", "=", False)],
                        limit=1,
                    )
                if existing:
                    # Same rule as real stops: ETA fields, schedule only
                    # when unset (schedule authority preserved).
                    eta_vals = {
                        "sequence": seq,
                        "travel_arrival_at": entry["eta"],
                        "facility_service_start_at": entry["eta"],
                        "planned_departure_at": entry["eta"],
                        "customer_eta_at": entry["eta"],
                        "service_time_minutes": 10,
                        "pod_required": True,
                        "cross_dock_origin_stop_id": origin_stop_id or existing.cross_dock_origin_stop_id.id or False,
                    }
                    if not existing.scheduled_time:
                        eta_vals["scheduled_time"] = entry["eta"]
                    existing.write(eta_vals)
                    continue
                cross_dock_legs += 1
                is_drop = entry["cross_dock_type"] == "drop"
                Stop.create({
                    "job_id": entry["job_id"],
                    "sequence": seq,
                    "stop_type": entry["stop_type"],
                    "address": entry["address"],
                    "saved_location_id": entry["location_id"],
                    "travel_arrival_at": entry["eta"],
                    "facility_service_start_at": entry["eta"],
                    "planned_departure_at": entry["eta"],
                    "customer_eta_at": entry["eta"],
                    "scheduled_time": entry["eta"],
                    "pallets_in": entry["pallets_in"],
                    "pallets_out": entry["pallets_out"],
                    "pod_required": True,
                    "service_time_minutes": 10,
                    "cross_dock_origin_stop_id": origin_stop_id,
                    "dispatcher_notes": (
                        f"Auto Plan: temporarily hold freight from {entry['origin_stop_name']} here"
                        if is_drop else
                        f"Auto Plan: reload freight held for {entry['origin_stop_name']}"
                    ),
                })

        return {
            "applied": len(result["suggested_order"]),
            "cross_dock_legs": cross_dock_legs,
        }

    def get_best_dispatch_options(self, job_id):
        """
        Suggest best options for dispatching a job:
        - same-day best truck
        - cheapest day (if flexible)
        - consolidation opportunity
        - split shipment opportunity

        Returns list of option dicts.
        """
        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            return []

        options = []
        scored = self.rank_trucks_for_job(job_id)

        if scored:
            best = scored[0]
            options.append({
                "type": "best_truck",
                "title": f"Best Available: {best['name']}",
                "description": (
                    f"Closest truck — {best['distance_to_pickup_km']:.1f} km to pickup. "
                    f"{best['existing_jobs']} existing job(s) today."
                ),
                "truck_id": best["truck_id"],
                "truck_name": best["name"],
                "savings": None,
            })

        # Look for trucks already going near delivery area
        delivery_cities = job.delivery_cities or ""
        if delivery_cities and scored:
            same_corridor = self._find_same_corridor_truck(job, scored)
            if same_corridor:
                options.append({
                    "type": "consolidation",
                    "title": f"Consolidate with {same_corridor['name']}",
                    "description": (
                        f"Already going near {delivery_cities}. "
                        f"Add to existing route for lower cost."
                    ),
                    "truck_id": same_corridor["truck_id"],
                    "truck_name": same_corridor["name"],
                    "savings": "Estimated 20-40% cost reduction",
                })

        # Cheapest-day option (if delivery_flexibility = flexible/economy)
        if job.delivery_flexibility in ("flexible", "economy"):
            options.append({
                "type": "cheapest_day",
                "title": "Hold for Cheaper Day",
                "description": (
                    "Delivery flexibility is set to Flexible/Economy. "
                    "System can consolidate with an existing route within 3 days "
                    "for lower cost."
                ),
                "truck_id": None,
                "truck_name": None,
                "savings": "Estimated 25-50% cost reduction",
            })

        return options

    def _find_same_corridor_truck(self, job, scored_trucks):
        """Find a truck already going to the same delivery area."""
        if not job.delivery_cities:
            return None

        delivery_keywords = {c.strip().lower()[:5] for c in job.delivery_cities.split(",")}

        for truck_info in scored_trucks:
            truck_jobs = self.env["prema.dispatch.job"].search([
                ("vehicle_id", "=", truck_info["truck_id"]),
                ("stage_id.stage_type", "in", ("booking", "dispatched")),
                ("stage_id.is_completed", "=", False),
            ])
            for tj in truck_jobs:
                if not tj.delivery_cities:
                    continue
                tj_keywords = {c.strip().lower()[:5] for c in tj.delivery_cities.split(",")}
                if delivery_keywords & tj_keywords:
                    return truck_info
        return None

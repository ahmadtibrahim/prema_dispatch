"""
Mid-day / opportunistic load finder.

Given a new pickup + delivery and load requirements, finds which truck is
best positioned RIGHT NOW to take the job — based on GPS location, remaining
schedule, and available capacity.
"""
import math
import logging
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

_AVG_SPEED_KMH = 75.0  # conservative average including stops / traffic


def _haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _eta_minutes(dist_km):
    return int(dist_km / _AVG_SPEED_KMH * 60)


class AdhocLoadService:
    def __init__(self, env):
        self.env = env

    def geocode(self, address):
        """Geocode an address using the existing rate estimator RPC. Returns (lat, lng) or None.

        geocode_address_rpc() returns a LIST of suggestions (it's built for
        an autocomplete dropdown) — take the first match. This was silently
        broken before (called .get() on the list itself), so pickup/delivery
        coordinates were always None: no distance/ETA scoring, no on-route
        detection ever actually ran.
        """
        if not address:
            return None
        try:
            estimator = self.env["premafirm.rate.estimator"]
            results = estimator.geocode_address_rpc(address)
            if results:
                first = results[0]
                if first.get("lat") and first.get("lng"):
                    return (float(first["lat"]), float(first["lng"]))
        except Exception as exc:
            _logger.warning("Geocode failed for %r: %s", address, exc)
        return None

    def find_suitable_trucks(
        self,
        pickup_address,
        delivery_address,
        pallets=0,
        requires_reefer=False,
        requires_liftgate=False,
        pickup_by=None,
    ):
        """
        Find and rank all trucks for a mid-day ad-hoc load.

        Returns dict:
          suitable   – list of truck dicts, best first
          available_later – list of busy trucks and when they free up
          pickup_coords  – geocoded pickup (lat, lng) or None
          delivery_coords – geocoded delivery (lat, lng) or None
          recommendation – plain-language string
        """
        now = datetime.utcnow()
        today = now.date()

        # Geocode new pickup
        pickup_coords = self.geocode(pickup_address) if pickup_address else None
        delivery_coords = self.geocode(delivery_address) if delivery_address else None

        # Load all trucks with their day schedule
        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        avail_svc = DispatchAvailabilityService(self.env)
        trucks = avail_svc.get_truck_day_schedule(today)

        suitable = []
        available_later = []

        for t in trucks:
            # Equipment filter (hard block)
            if requires_reefer and not t["has_reefer"]:
                continue
            if requires_liftgate and not t["has_liftgate"]:
                continue

            truck_lat = t["lat"] or 0
            truck_lng = t["lng"] or 0
            has_gps = bool(truck_lat and truck_lng)

            # Distance from truck's current GPS to new pickup
            dist_to_pickup_km = None
            eta_to_pickup_min = None
            if has_gps and pickup_coords:
                dist_to_pickup_km = _haversine_km(
                    truck_lat, truck_lng, pickup_coords[0], pickup_coords[1]
                )
                eta_to_pickup_min = _eta_minutes(dist_to_pickup_km)

            # Detour analysis: is new pickup "on the way" for trucks already moving?
            on_route = False
            detour_km = None
            if has_gps and pickup_coords and t.get("jobs"):
                # Check if new pickup is between current position and next stop
                next_stops = self._get_remaining_stops(t["truck_id"])
                if next_stops:
                    next_stop = next_stops[0]
                    if next_stop.get("lat") and next_stop.get("lng"):
                        # Is pickup within 20km of the line from truck → next stop?
                        on_route = self._is_on_route(
                            truck_lat, truck_lng,
                            pickup_coords[0], pickup_coords[1],
                            next_stop["lat"], next_stop["lng"],
                            threshold_km=20,
                        )
                        if on_route:
                            # Detour = (truck→pickup→next_stop) - (truck→next_stop)
                            direct_km = _haversine_km(
                                truck_lat, truck_lng,
                                next_stop["lat"], next_stop["lng"],
                            )
                            via_km = (
                                dist_to_pickup_km +
                                _haversine_km(
                                    pickup_coords[0], pickup_coords[1],
                                    next_stop["lat"], next_stop["lng"],
                                )
                            )
                            detour_km = round(via_km - direct_km, 1)

            # Capacity check
            cap = t["pallet_capacity"] or 0
            avail_cap = t["available_capacity"]
            if pallets and cap and pallets > avail_cap:
                # Truck doesn't have room right now
                if t["status"] == "busy":
                    available_later.append({
                        "truck_id": t["truck_id"],
                        "name": t["name"],
                        "driver_name": t["driver_name"],
                        "available_from": t["available_from"],
                        "busy_until": t["busy_until"],
                        "has_reefer": t["has_reefer"],
                        "has_liftgate": t["has_liftgate"],
                        "reason": f"Fully committed. Free ~{t['available_from']}",
                    })
                continue

            # Score: lower = better
            # Prefer: on route, close to pickup, available truck
            score = 9999
            if dist_to_pickup_km is not None:
                score = dist_to_pickup_km
                if on_route:
                    score = max(0, score - 30)  # bonus for on-route trucks
                if t["status"] == "available":
                    score -= 5  # small bonus for idle trucks
                if t.get("gps_age_minutes") and t["gps_age_minutes"] > 30:
                    score += 15  # penalty for stale GPS

            # Plain-language fit description
            fit_parts = []
            if on_route and detour_km is not None:
                fit_parts.append(
                    f"already heading that direction — +{detour_km} km detour, "
                    f"+{_eta_minutes(detour_km)} min"
                )
            elif dist_to_pickup_km is not None:
                fit_parts.append(
                    f"{dist_to_pickup_km:.0f} km to pickup, "
                    f"~{eta_to_pickup_min} min ETA"
                )
            else:
                fit_parts.append("GPS location unknown")

            if t["status"] == "available":
                fit_parts.append("no other jobs today")
            elif t["status"] == "partial":
                fit_parts.append(f"{avail_cap} pallet capacity remaining")

            suitable.append({
                "truck_id": t["truck_id"],
                "name": t["name"],
                "driver_name": t["driver_name"],
                "score": round(score, 1),
                "status": t["status"],
                "on_route": on_route,
                "dist_to_pickup_km": dist_to_pickup_km,
                "eta_to_pickup_min": eta_to_pickup_min,
                "detour_km": detour_km,
                "available_capacity": avail_cap,
                "pallet_capacity": cap,
                "has_reefer": t["has_reefer"],
                "has_liftgate": t["has_liftgate"],
                "gps_age_minutes": t.get("gps_age_minutes"),
                "fit_description": "; ".join(fit_parts),
                "current_jobs": len(t.get("jobs", [])),
                "available_from": t["available_from"],
            })

        suitable.sort(key=lambda x: x["score"])

        # Refine the shortlist with real driving distance (Distance Matrix API).
        # Haversine straight-line badly underestimates road distance in this
        # region (Lake Ontario cuts straight lines that don't follow roads),
        # so re-rank just the top candidates with real road distance/time —
        # capped to control API cost.
        if pickup_coords:
            self._refine_top_candidates_with_distance_matrix(suitable, trucks, pickup_coords)
            suitable.sort(key=lambda x: x["score"])

        # Also collect busy trucks (not already added above) for "available later"
        for t in trucks:
            if requires_reefer and not t["has_reefer"]:
                continue
            if requires_liftgate and not t["has_liftgate"]:
                continue
            if t["status"] == "busy" and not any(
                al["truck_id"] == t["truck_id"] for al in available_later
            ) and not any(
                s["truck_id"] == t["truck_id"] for s in suitable
            ):
                available_later.append({
                    "truck_id": t["truck_id"],
                    "name": t["name"],
                    "driver_name": t["driver_name"],
                    "available_from": t["available_from"],
                    "busy_until": t["busy_until"],
                    "has_reefer": t["has_reefer"],
                    "has_liftgate": t["has_liftgate"],
                    "reason": f"Fully committed — free ~{t['available_from']}",
                })

        # Sort available_later by available_from time
        available_later.sort(key=lambda x: x["available_from"] or "99:99")

        recommendation = self._build_recommendation(
            suitable, available_later, pickup_address, delivery_address,
            pallets, requires_reefer, pickup_by,
        )

        return {
            "suitable": suitable,
            "available_later": available_later,
            "pickup_coords": pickup_coords,
            "delivery_coords": delivery_coords,
            "recommendation": recommendation,
        }

    def _refine_top_candidates_with_distance_matrix(self, suitable, trucks, pickup_coords, top_n=5):
        """Re-rank the top N candidates using real driving distance/time
        (Distance Matrix API) instead of the haversine straight-line
        estimate. Capped to top_n to control API cost — this is a refinement
        of an already-reasonable shortlist, not the initial screening.
        """
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        if not api_key:
            return
        trucks_by_id = {t["truck_id"]: t for t in trucks}
        top = [c for c in suitable[:top_n] if c.get("dist_to_pickup_km") is not None]
        if not top:
            return
        origins = []
        for c in top:
            t = trucks_by_id.get(c["truck_id"])
            if not t:
                return
            origins.append(f"{t['lat']},{t['lng']}")
        try:
            import requests
            r = requests.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={
                    "origins": "|".join(origins),
                    "destinations": f"{pickup_coords[0]},{pickup_coords[1]}",
                    "key": api_key,
                },
                timeout=6,
            )
            data = r.json()
            if data.get("status") != "OK":
                _logger.warning("Distance Matrix refinement failed: %s", data.get("status"))
                return
            for c, row in zip(top, data.get("rows", [])):
                elem = (row.get("elements") or [{}])[0]
                if elem.get("status") != "OK":
                    continue
                real_km = elem["distance"]["value"] / 1000.0
                real_min = round(elem["duration"]["value"] / 60)
                c["dist_to_pickup_km"] = round(real_km, 1)
                c["eta_to_pickup_min"] = real_min
                bonus = 0
                if c["on_route"]:
                    bonus -= 30
                if c["status"] == "available":
                    bonus -= 5
                if c.get("gps_age_minutes") and c["gps_age_minutes"] > 30:
                    bonus += 15
                c["score"] = round(max(0, real_km + bonus), 1)
                if not c["on_route"]:
                    extra = (
                        "no other jobs today" if c["status"] == "available"
                        else f"{c['available_capacity']} pallet capacity remaining" if c["status"] == "partial"
                        else ""
                    )
                    c["fit_description"] = f"{real_km:.0f} km to pickup (road distance), ~{real_min} min ETA" + (
                        f"; {extra}" if extra else ""
                    )
        except Exception:
            _logger.exception("Distance Matrix refinement request failed")

    def _get_remaining_stops(self, truck_id):
        """Return remaining pending/en_route stops for a truck's active jobs today."""
        from datetime import date
        today = date.today()
        jobs = self.env["prema.dispatch.job"].search([
            ("vehicle_id", "=", truck_id),
            ("stage_id.is_completed", "=", False),
            ("stage_id.is_cancelled", "=", False),
        ])
        stops = []
        for job in jobs:
            for s in job.stop_ids.filtered(
                lambda st: st.status in ("pending", "en_route")
            ).sorted("sequence"):
                stops.append({
                    "stop_id": s.id,
                    "address": s.address,
                    "lat": s.latitude,
                    "lng": s.longitude,
                    "estimated_arrival": s.estimated_arrival,
                })
        return stops

    @staticmethod
    def _is_on_route(from_lat, from_lng, pickup_lat, pickup_lng, to_lat, to_lng, threshold_km=20):
        """
        Check if pickup_point is within threshold_km of the line from → to.
        Uses cross-track distance approximation.
        """
        # Cross-track distance from point P to great-circle line A→B
        d_ab = _haversine_km(from_lat, from_lng, to_lat, to_lng)
        d_ap = _haversine_km(from_lat, from_lng, pickup_lat, pickup_lng)
        if d_ab < 0.1:
            return d_ap < threshold_km
        # Angular distance
        R = 6371.0
        delta13 = d_ap / R
        theta13 = math.atan2(
            math.sin(math.radians(pickup_lng - from_lng)) * math.cos(math.radians(pickup_lat)),
            math.cos(math.radians(from_lat)) * math.sin(math.radians(pickup_lat))
            - math.sin(math.radians(from_lat)) * math.cos(math.radians(pickup_lat))
            * math.cos(math.radians(pickup_lng - from_lng)),
        )
        theta12 = math.atan2(
            math.sin(math.radians(to_lng - from_lng)) * math.cos(math.radians(to_lat)),
            math.cos(math.radians(from_lat)) * math.sin(math.radians(to_lat))
            - math.sin(math.radians(from_lat)) * math.cos(math.radians(to_lat))
            * math.cos(math.radians(to_lng - from_lng)),
        )
        dxt = abs(math.asin(math.sin(delta13) * math.sin(theta13 - theta12)) * R)
        return dxt < threshold_km

    @staticmethod
    def _build_recommendation(suitable, available_later, pickup_addr, delivery_addr,
                               pallets, requires_reefer, pickup_by):
        """Build a single plain-language recommendation string."""
        equip = "Reefer" if requires_reefer else "Dry"
        load_str = f"{pallets} pallet(s)" if pallets else "load"
        route_str = f"{pickup_addr or '?'} → {delivery_addr or '?'}"

        if not suitable and not available_later:
            return (
                f"No {equip} trucks found for {load_str}. "
                "Check if any trucks have GPS location enabled."
            )

        if not suitable:
            nxt = available_later[0]
            return (
                f"All {equip} trucks are fully committed. "
                f"{nxt['name']} ({nxt['driver_name']}) is the next available at ~{nxt['available_from']}. "
                f"Cannot complete {route_str} until then."
            )

        best = suitable[0]
        if best["on_route"] and best["detour_km"] is not None:
            return (
                f"RECOMMENDED: {best['name']} ({best['driver_name']}) is already heading toward "
                f"the pickup area — only +{best['detour_km']} km detour, "
                f"+{_eta_minutes(best['detour_km'])} min impact. "
                f"Capacity: {best['available_capacity']} pallets available. "
                f"Route: {route_str}."
            )

        if best["dist_to_pickup_km"] is not None:
            return (
                f"RECOMMENDED: {best['name']} ({best['driver_name']}) — "
                f"{best['dist_to_pickup_km']:.0f} km to pickup, ~{best['eta_to_pickup_min']} min ETA. "
                f"Capacity: {best['available_capacity']} pallets available. "
                f"Route: {route_str}."
            )

        return (
            f"RECOMMENDED: {best['name']} ({best['driver_name']}) — GPS location not available. "
            f"Verify availability before assigning. Route: {route_str}."
        )

import logging
from datetime import timedelta

import requests

_logger = logging.getLogger(__name__)

GMAPS_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"


def _fmt_location(loc):
    """Format address string or (lat, lng) tuple for Google Maps API."""
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        return f"{loc[0]},{loc[1]}"
    return str(loc)


class DispatchRouteService:
    """
    Wraps Google Maps Directions API for sequential stop routing.
    Uses the Directions API (not Distance Matrix) so sequential legs
    are returned in one efficient call.
    """

    def __init__(self, env):
        self.env = env
        self.api_key = env["ir.config_parameter"].sudo().get_param(
            "google_maps_api_key", ""
        )

    # ── Core API call ─────────────────────────────────────────────

    def get_sequential_travel(self, locations):
        """
        Call Google Maps Directions API for an ordered list of locations.

        Args:
            locations: list of address strings or (lat, lng) tuples.
                       Must have at least 2 elements.

        Returns:
            list of dicts, length = len(locations) - 1:
              [{drive_minutes: int, distance_km: float}, ...]
            One entry per leg (gap between consecutive stops).
        """
        if len(locations) < 2:
            return []
        if not self.api_key:
            _logger.warning("Google Maps API key not configured.")
            return [{"drive_minutes": 0, "distance_km": 0.0}] * (len(locations) - 1)

        origin = _fmt_location(locations[0])
        destination = _fmt_location(locations[-1])
        midpoints = [_fmt_location(loc) for loc in locations[1:-1]]

        params = {
            "origin": origin,
            "destination": destination,
            "key": self.api_key,
            "mode": "driving",
            "units": "metric",
        }
        if midpoints:
            params["waypoints"] = "optimize:false|" + "|".join(midpoints)

        try:
            resp = requests.get(GMAPS_DIRECTIONS_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status")
            if status != "OK":
                _logger.warning("Directions API returned status=%s", status)
                return [{"drive_minutes": 0, "distance_km": 0.0}] * (len(locations) - 1)

            routes = data.get("routes", [])
            if not routes:
                return [{"drive_minutes": 0, "distance_km": 0.0}] * (len(locations) - 1)

            legs = routes[0].get("legs", [])
            results = []
            for leg in legs:
                dur_sec = leg.get("duration", {}).get("value", 0)
                dist_m = leg.get("distance", {}).get("value", 0)
                results.append({
                    "drive_minutes": round(dur_sec / 60),
                    "distance_km": round(dist_m / 1000, 1),
                })

            # Pad if fewer legs than expected (shouldn't happen, but be safe)
            while len(results) < len(locations) - 1:
                results.append({"drive_minutes": 0, "distance_km": 0.0})

            return results

        except requests.RequestException as exc:
            _logger.exception("Directions API network error: %s", exc)
        except Exception as exc:
            _logger.exception("Directions API unexpected error: %s", exc)

        return [{"drive_minutes": 0, "distance_km": 0.0}] * (len(locations) - 1)

    # ── Job-level estimation ──────────────────────────────────────

    def estimate_job_route(self, job):
        """
        Compute drive times and ETAs for all stops on a job.
        Writes estimated_arrival, estimated_departure, drive_time_from_prev_minutes
        to each stop, and estimated_duration_minutes / estimated_distance_km to job.

        Returns dict: {total_minutes, total_km, stops_updated}
        """
        stops = list(job.stop_ids.sorted("sequence"))
        if not stops:
            return {"total_minutes": 0, "total_km": 0.0, "stops_updated": 0}

        # Build location list — prefer lat/lng, fall back to address
        locations = []
        for s in stops:
            if s.latitude and s.longitude:
                locations.append((s.latitude, s.longitude))
            elif s.address:
                locations.append(s.address)
            else:
                _logger.warning(
                    "Stop %s on job %s has no address or GPS — cannot estimate route.",
                    s.id, job.name,
                )
                return {"total_minutes": 0, "total_km": 0.0, "stops_updated": 0}

        travel = self.get_sequential_travel(locations)

        # Determine route start time
        start_time = (
            job.pickup_exact_time
            or job.pickup_earliest
            or job.scheduled_pickup
        )

        total_minutes = 0
        total_km = 0.0
        current_time = start_time
        stops_updated = 0

        for i, stop in enumerate(stops):
            leg = travel[i - 1] if i > 0 else {"drive_minutes": 0, "distance_km": 0.0}
            drive_min = leg["drive_minutes"]
            dist_km = leg["distance_km"]
            total_minutes += drive_min
            total_km += dist_km

            svc_min = stop.service_time_minutes
            if not svc_min:
                svc_min = 20 if stop.stop_type == "pickup" else 15

            vals = {"drive_time_from_prev_minutes": drive_min}

            if current_time:
                if i > 0 and drive_min:
                    arrival = current_time + timedelta(minutes=drive_min)
                else:
                    arrival = current_time
                departure = arrival + timedelta(minutes=svc_min)
                vals["estimated_arrival"] = arrival
                vals["estimated_departure"] = departure
                current_time = departure

            stop.write(vals)
            stops_updated += 1

        job.write({
            "estimated_duration_minutes": total_minutes,
            "estimated_distance_km": total_km,
        })

        return {
            "total_minutes": total_minutes,
            "total_km": total_km,
            "stops_updated": stops_updated,
        }

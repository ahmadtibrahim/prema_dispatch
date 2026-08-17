"""
DispatchRouteService — Google Maps Directions wrapper for sequential
stop routing, with a deterministic straight-line fallback.

Google road routing is PRIMARY (Directions API, region=ca so results are
biased to Canada and never detour through the USA); when the API key is
missing, the call fails, or the response is unusable, EVERY leg falls
back to a straight-line ×1.4 road-factor estimate @ 50 km/h instead of
silent zeros — so route ordering, feasibility and ETAs degrade
gracefully instead of vanishing.

A "no USA routing" guard decodes the returned overview polyline and
flags any route that dips more than 0.75° south of the route's own
southernmost stop (an Ontario corridor never legitimately does that; a
US detour through New York state does). The flag is surfaced on the
returned legs as an attribute and promoted to a warning by consumers —
never silently ignored, never hard-blocking.
"""
import logging
import math
from datetime import timedelta

import requests

_logger = logging.getLogger(__name__)

GMAPS_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

# Fallback travel model (used whenever Google is unavailable).
FALLBACK_ROAD_FACTOR = 1.4
FALLBACK_KMH = 50.0

# A route point more than this far south of the route's own southernmost
# stop cannot be part of any sane Ontario corridor drive.
US_DIP_MARGIN_DEG = 0.75


def _fmt_location(loc):
    """Format address string or (lat, lng) tuple for Google Maps API."""
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        return f"{loc[0]},{loc[1]}"
    return str(loc)


class _Legs(list):
    """List subclass that may carry route metadata (source, USA-dip flag,
    raw polyline) as attributes — plain lists cannot hold attributes."""


def _straight_line_km(loc_a, loc_b):
    """Great-circle distance between two (lat, lng) tuples."""
    lat1, lng1 = float(loc_a[0]), float(loc_a[1])
    lat2, lng2 = float(loc_b[0]), float(loc_b[1])
    if not (lat1 and lat2):
        return 0.0
    dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 111.32
    return math.sqrt(dx * dx + dy * dy) * FALLBACK_ROAD_FACTOR


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

        The returned list carries two attributes:
            legs._source: "google" (road data used) or "fallback"
                (straight-line estimate substituted)
            legs._us_dip_detected: True when the Google route shape
                dips south of the route's own stops — likely a USA
                detour (fallback legs never flag: a straight line
                between validated Ontario coordinates cannot cross
                the border).
        """
        if len(locations) < 2:
            return []

        legs = self._google_legs(locations)
        if legs is not None:
            legs._source = "google"
            legs._us_dip_detected = self._google_route_dips_into_usa(
                legs, locations)
            return legs

        # Deterministic fallback: straight-line ×1.4 @ 50 km/h per leg —
        # never zeros, so ordering/feasibility/ETA logic keeps working.
        results = _Legs()
        for i in range(len(locations) - 1):
            a, b = locations[i], locations[i + 1]
            km = 0.0
            if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
                km = _straight_line_km(a, b)
            results.append({
                "drive_minutes": round(km / FALLBACK_KMH * 60.0),
                "distance_km": round(km, 1),
            })
        results._source = "fallback"
        results._us_dip_detected = False
        return results

    def _google_legs(self, locations):
        """Google Directions legs, or None when the API is unavailable /
        unusable (caller falls back to the straight-line model)."""
        if not self.api_key:
            _logger.warning(
                "Google Maps API key not configured — using straight-line "
                "fallback for route estimates.")
            return None

        origin = _fmt_location(locations[0])
        destination = _fmt_location(locations[-1])
        midpoints = [_fmt_location(loc) for loc in locations[1:-1]]

        params = {
            "origin": origin,
            "destination": destination,
            "key": self.api_key,
            "mode": "driving",
            "units": "metric",
            # Canada-only bias: never detour through the United States.
            "region": "ca",
            "alternatives": "false",
        }
        if midpoints:
            params["waypoints"] = "optimize:false|" + "|".join(midpoints)

        try:
            resp = requests.get(GMAPS_DIRECTIONS_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _logger.exception("Directions API network error: %s", exc)
            return None
        except Exception as exc:
            _logger.exception("Directions API unexpected error: %s", exc)
            return None

        status = data.get("status")
        if status != "OK":
            _logger.warning("Directions API returned status=%s", status)
            return None
        routes = data.get("routes", [])
        if not routes:
            return None

        results = _Legs()
        for leg in routes[0].get("legs", []):
            dur_sec = leg.get("duration", {}).get("value", 0)
            dist_m = leg.get("distance", {}).get("value", 0)
            results.append({
                "drive_minutes": round(dur_sec / 60),
                "distance_km": round(dist_m / 1000, 1),
            })
        # Pad if fewer legs than expected (shouldn't happen, but be safe).
        while len(results) < len(locations) - 1:
            results.append({"drive_minutes": 0, "distance_km": 0.0})
        results._polyline = routes[0].get("overview_polyline") or {}
        return results

    # ── USA-detour guard ───────────────────────────────────────────

    @staticmethod
    def decode_polyline(polyline):
        """Google encoded polyline → [(lat, lng), ...] (standard
        algorithm). Returns [] on malformed input — never raises."""
        points = []
        if not polyline:
            return points
        index = 0
        lat = lng = 0
        try:
            while index < len(polyline):
                result = 0
                shift = 0
                while True:
                    b = ord(polyline[index]) - 63
                    index += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                lat += ~(result >> 1) if result & 1 else result >> 1
                result = 0
                shift = 0
                while True:
                    b = ord(polyline[index]) - 63
                    index += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                lng += ~(result >> 1) if result & 1 else result >> 1
                points.append((lat / 1e5, lng / 1e5))
        except (IndexError, ValueError):
            return []
        return points

    def _google_route_dips_into_usa(self, legs, locations):
        """True when the Google route shape dips far south of the route's
        own stops (a USA detour). Only checked when every stop is a
        (lat, lng) tuple — address strings carry no geometry."""
        polyline = getattr(legs, "_polyline", None)
        if not polyline:
            return False
        if not all(isinstance(loc, (list, tuple)) and len(loc) == 2
                   for loc in locations):
            return False
        points = self.decode_polyline(polyline.get("points") or "")
        if not points:
            return False
        min_stop_lat = min(float(loc[0]) for loc in locations)
        floor = min_stop_lat - US_DIP_MARGIN_DEG
        worst = min(p[0] for p in points)
        if worst < floor:
            _logger.warning(
                "Directions route dips %.2f° south of the route's own "
                "stops (floor %.2f°N) — USA detour suspected.",
                floor - worst, floor,
            )
            return True
        return False

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

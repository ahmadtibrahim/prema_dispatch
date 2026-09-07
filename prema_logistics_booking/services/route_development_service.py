"""Route Development advice (master §13) — the estimator's view of the
scheduled corridor week plus the honest development gaps.

Two read-only surfaces:
  1. ``route_dev_report(vehicle_id, week_start, fsa_codes)`` — every
     scheduled corridor departure that week for (a) the chosen truck and
     (b) the corridors serving the request's region pair, each with live
     free capacity (CapacityEngine peaks), plus region city context so the
     AI can match CRM leads by city.
  2. Development gaps — configured corridor pairs (existing topology) that
     have NO scheduled departure in the week for the request pair: those
     are scheduling opportunities the dispatcher must open; the AI never
     invents them.

Authority rules:
  - Departure rows come from the corridor schedule only (read).
  - Free positions come from CapacityEngine.compute_departure_peak — the
    same count the booking pipeline uses.
  - City/region data comes from logistics.region / logistics.city markers.
  - Nothing here creates departures, allocates trucks or books loads.
"""

import datetime

import logging

_logger = logging.getLogger(__name__)

WEEK_SPAN_DAYS = 7
MAX_DEPARTURE_ROWS = 60


class RouteDevelopmentService:
    def __init__(self, env):
        self.env = env(su=True)

    # ── Public surface ──────────────────────────────────────────────

    def route_dev_report(self, vehicle_id=0, week_start=None,
                         fsa_codes=None):
        """Weekly corridor development view for the estimator route-dev
        panel. Never raises — a missing schedule is a fact, not an error."""
        vehicle = self.env["fleet.vehicle"].browse(int(vehicle_id or 0))
        if not vehicle.exists():
            vehicle = False
        else:
            vehicle = vehicle.sudo()
        start = self._week_start(week_start)
        end = start + datetime.timedelta(days=WEEK_SPAN_DAYS)

        regions = self._regions_from_fsa(fsa_codes or [])
        region_ids = {r.id for r in regions if r}

        truck_rows = self._truck_week(vehicle, start, end)
        pair_rows, pairs = self._pair_week(region_ids, regions, start, end)
        city_context = self._city_context(regions)
        gaps = self._development_gaps(region_ids, regions, start, end)

        return {
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_name": (vehicle.name or vehicle.license_plate or "")
            if vehicle else "",
            "truck_rows": truck_rows,
            "pair_rows": pair_rows,
            "pairs": pairs,
            "gaps": gaps,
            "city_context": city_context,
            "authority": "logistics.corridor.departure (read-only)",
        }

    # ── (a) The chosen truck's scheduled week ───────────────────────

    def _truck_week(self, vehicle, start, end):
        Departure = self.env["logistics.corridor.departure"]
        if "logistics.corridor.departure" not in self.env.registry \
                or not vehicle:
            return []
        rows = []
        for dep in Departure.search([
            ("vehicle_id", "=", vehicle.id),
            ("departure_date", ">=", start),
            ("departure_date", "<", end),
        ], order="departure_date asc, departure_time asc, id asc",
                limit=MAX_DEPARTURE_ROWS):
            rows.append(self._departure_row(dep))
        return rows

    # ── (b) Corridors serving the request's region pair ─────────────

    def _pair_week(self, region_ids, regions, start, end):
        """Departures on corridors whose topology covers the request pair.
        Direction labels are drawn from the request's own regions (the
        corridor itself may carry several segments)."""
        Departure = self.env["logistics.corridor.departure"]
        if "logistics.corridor.departure" not in self.env.registry:
            return [], []
        rows = []
        pairs = set()
        dep_list = Departure.search([
            ("departure_date", ">=", start),
            ("departure_date", "<", end),
        ], order="departure_date asc, departure_time asc, id asc",
                limit=MAX_DEPARTURE_ROWS)
        for dep in dep_list:
            if not dep.corridor_id or not dep.vehicle_id:
                continue
            served = []
            for origin in regions:
                for dest in regions:
                    if origin.id == dest.id:
                        continue
                    if dep.corridor_id.resolve_region_segment(origin, dest):
                        served.append((origin, dest))
            if not served:
                continue
            row = self._departure_row(dep)
            for origin, dest in served:
                key = "%s→%s" % (origin.code, dest.code)
                pairs.add((key, origin.main_city or origin.name,
                           dest.main_city or dest.name))
            rows.append(row)
        return rows, sorted(pairs)

    def _departure_row(self, dep):
        vehicle = dep.vehicle_id
        peak = {}
        free = False
        if vehicle:
            try:
                from .capacity_engine import CapacityEngine
                peak = CapacityEngine(self.env).compute_departure_peak(dep)
                free = max(0, int(vehicle.straight_pallet_capacity or 12)
                           - peak.get("peak_pallets", 0))
            except Exception:
                peak, free = {}, False
        return {
            "departure_id": dep.id,
            "corridor": dep.corridor_id.name if dep.corridor_id else "",
            "corridor_id": dep.corridor_id.id if dep.corridor_id else False,
            "date": self._iso(dep.departure_date),
            "time": dep.departure_time or "",
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_name": (vehicle.name or vehicle.license_plate or "")
            if vehicle else "",
            "peak_pallets": peak.get("peak_pallets", 0),
            "free_pallets": free,
            "state": dep.state if "state" in dep._fields else "",
        }

    # ── Development gaps ────────────────────────────────────────────

    def _development_gaps(self, region_ids, regions, start, end):
        """Configured corridor segments with no departure in the week.
        These need a dispatcher's scheduling action — the AI can flag them
        but never schedule them."""
        Departure = self.env["logistics.corridor.departure"]
        gaps = []
        if "logistics.region" not in self.env.registry:
            return gaps
        all_regions = self.env["logistics.region"].search(
            [("active", "=", True)])
        for origin in regions:
            if not origin:
                continue
            dep_dates = {}
            if "logistics.corridor.departure" in self.env.registry:
                for dep in Departure.search([
                    ("departure_date", ">=", start),
                    ("departure_date", "<", end),
                ]):
                    dep_dates.setdefault(dep.corridor_id.id, dep.departure_date)
            for corridor in self.env["logistics.corridor"].search(
                    [("active", "=", True)]):
                if corridor.id in dep_dates:
                    continue
                for dest in all_regions:
                    if origin.id == dest.id:
                        continue
                    segment = corridor.resolve_region_segment(origin, dest)
                    if not segment:
                        continue
                    gaps.append({
                        "from_region": origin.code,
                        "from_city": origin.main_city or origin.name,
                        "to_region": dest.code,
                        "to_city": dest.main_city or dest.name,
                        "corridor": corridor.name,
                        "corridor_id": corridor.id,
                        "note": ("Configured corridor %s has no scheduled "
                                 "departure in this week — a dispatcher "
                                 "must schedule it before it can serve."
                                 % corridor.name),
                    })
                    break  # one gap row per corridor per origin
        # de-duplicate corridor-level repeats per pair
        seen = set()
        unique = []
        for g in gaps:
            key = (g["corridor_id"], g["from_region"], g["to_region"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(g)
        return unique[:24]

    # ── City context for AI-side CRM-lead matching ──────────────────

    def _city_context(self, regions):
        out = []
        for region in regions:
            if not region:
                continue
            cities = []
            if "logistics.city" in self.env.registry:
                cities = [c.name for c in self.env["logistics.city"].search(
                    [("region_id", "=", region.id),
                     ("active", "in", (True, False))],
                    order="name asc", limit=15)]
            out.append({
                "region_code": region.code,
                "region_name": region.name,
                "main_city": region.main_city or "",
                "marker_lat": region.marker_latitude,
                "marker_lng": region.marker_longitude,
                "cities": cities,
            })
        return out

    # ── Small helpers ───────────────────────────────────────────────

    def _regions_from_fsa(self, fsa_codes):
        regions = []
        seen = set()
        for code in fsa_codes or []:
            code = str(code or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(code)
            if not fsa or not fsa.region_id:
                continue
            try:
                from .region_resolver import RegionResolver
                region = RegionResolver(self.env).canonical_region(fsa.region_id)
            except Exception:
                region = fsa.region_id
            if region and region.id not in {r.id for r in regions}:
                regions.append(region)
        return regions

    def _week_start(self, value):
        """ISO week start (Monday) for the given date or today."""
        today = datetime.date.today()
        if value:
            try:
                if hasattr(value, "date") and hasattr(value, "hour"):
                    today = value.date()
                elif hasattr(value, "isoformat"):
                    today = value
                else:
                    today = datetime.date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                today = datetime.date.today()
        return today - datetime.timedelta(days=today.weekday())

    @staticmethod
    def _iso(value):
        if not value:
            return ""
        try:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return datetime.date.fromisoformat(str(value)[:10]).isoformat()
        except (TypeError, ValueError):
            return ""

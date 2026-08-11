"""Canonical geographic region resolver for Prema Logistics.

Two responsibilities, one authority:

1. POINT-IN-POLYGON matching — match a latitude/longitude coordinate to a
   Premafirm Service Region via Shapely. All Odoo components MUST use this
   service instead of independently implementing polygon logic.

2. DETERMINISTIC REGION BRIDGE — normalize between the two disconnected
   region systems in production:
     OLD regions (IDs 1-10, 16-20; codes R1..R15) — logistics.lane,
       service offerings, lane schedules, rate plans, corridor
       start_hub/end_hub FKs
     NEW official LTL regions (IDs 142-159; codes R-NIA..R-KAW) —
       logistics.corridor.stop, logistics.direct.delivery.rule,
       is_official_ltl_region = true
   corridor_lane_rel is empty, so the bridge below is the read-only glue
   that lets corridor routing reach lane-based pricing.

Input:   latitude, longitude, optional country/province filters
         — or any region-ish reference (old/new region, FSA/postal, city,
           saved location, hub, corridor stop)
Output:  matched region, method, candidates, ambiguity status, reason
         — or the canonical official-LTL region record

Uses Shapely 2.1.2 (already installed in venv-18).
"""

import hashlib
import json
import logging
import re
from collections import namedtuple

from shapely.geometry import Point, shape
from shapely.validation import explain_validity

_logger = logging.getLogger(__name__)

# ── Result type ──────────────────────────────────────────────────────────
RegionMatch = namedtuple("RegionMatch", [
    "matched_region",        # logistics.region record or None
    "matched_region_code",   # str or None
    "outcome",               # 'SCHEDULED_MATCH' | 'MANUAL_QUOTE' | 'NETWORK_DISABLED' | 'AMBIGUOUS'
    "match_method",          # 'polygon' | 'fsa_fallback' | 'manual' | 'none'
    "candidate_regions",     # list of region records that contained the point
    "ambiguity",             # bool — True if multiple regions matched
    "ambiguity_detail",      # str — explanation when ambiguous
    "reason",                # str — human-readable resolution summary
    "match_timestamp",       # str — ISO timestamp
])

# Sentinel for "no match"
NO_MATCH = RegionMatch(
    matched_region=None,
    matched_region_code=None,
    outcome="MANUAL_QUOTE",
    match_method="none",
    candidate_regions=[],
    ambiguity=False,
    ambiguity_detail="",
    reason="",
    match_timestamp="",
)


# ═══════════════════════════════════════════════════════════════════════════
# Region bridge — old-lane regions (1-20) ↔ official LTL regions (142-159)
#
# Two disconnected region systems coexist in production:
#   OLD (IDs 1-10, 16-20; codes R1..R15) — logistics.lane, service
#       offerings, lane schedules, rate plans, corridor start_hub/end_hub
#       FKs, logistics.fsa
#   NEW (IDs 142-159; codes R-NIA..R-KAW) — logistics.corridor.stop,
#       logistics.direct.delivery.rule, is_official_ltl_region = true
#
# corridor_lane_rel is EMPTY (0 rows), so the pricing engine cannot bridge
# corridor routing to lane-based pricing. These constants ARE the bridge —
# deterministic, read-only. The mapping is based on geographic overlap /
# name match; regions without an approved polygon (old 2, 4, 9, 16, 19)
# map by geography/name.
#
# For many-to-many relationships (one old region spans several new ones),
# OLD_TO_NEW_PRIMARY holds the resolver's FIRST/primary choice; the full
# sets are documented in OLD_TO_NEW_FULL.
# ═══════════════════════════════════════════════════════════════════════════

OLD_TO_NEW_PRIMARY = {
    1: 144,   # GTA Central            → R-GTA  (Greater Toronto Area)
    2: 148,   # Southwest Ontario      → R-WAT  (Waterloo and Wellington) + regions west
    3: 143,   # Golden Horseshoe South → R-HAM  (Hamilton, Halton and Brant)
    4: 147,   # Central Ont / Grey-Br  → R-HDW  (Headwaters)
    5: 149,   # East-Central Ontario   → R-NOR  (Northumberland)
    6: 150,   # Eastern Ontario        → R-SEO  (Southeastern Ontario)
    7: 157,   # Ottawa Valley          → R-OTT  (Ottawa Region)
    8: 151,   # Greater Montreal       → R-MON  (Montérégie)
    9: 153,   # Central Quebec         → R-CDQ  (Centre-du-Québec)
    10: 154,  # Quebec City Region     → R-QUE  (Québec city and area)
    16: 150,  # Eastern Ontario East   → R-SEO  (Southeastern Ontario)
    17: 157,  # Ottawa Valley (R12)    → R-OTT  (Ottawa Region)
    18: 151,  # Greater Montreal (R13) → R-MON  (Montérégie)
    19: 153,  # Central Quebec (R14)   → R-CDQ  (Centre-du-Québec)
    20: 154,  # Quebec City (R15)      → R-QUE  (Québec city and area)
}

# Full old → new sets (documented; a single old region may cover several
# official LTL regions).
OLD_TO_NEW_FULL = {
    1: [144, 145, 146],   # R-GTA, R-YRK, R-DUR
    2: [148],             # R-WAT + regions west (not yet created in prod)
    3: [143, 142],        # R-HAM, R-NIA
    4: [147],             # R-HDW
    5: [149, 159],        # R-NOR, R-KAW
    6: [150],             # R-SEO
    7: [157, 158],        # R-OTT, R-HAL
    8: [151, 152],        # R-MON, R-LAV
    9: [153],             # R-CDQ
    10: [154, 155],       # R-QUE, R-CHA
    16: [150],            # R-SEO
    17: [157, 158],       # R-OTT, R-HAL
    18: [151, 152],       # R-MON, R-LAV
    19: [153],            # R-CDQ
    20: [154, 155],       # R-QUE, R-CHA
}

# Inverse: canonical region → primary old region (function, not a set).
# R-SEO (150) is shared by old R6 and R11 → primary old is 6 (R11 maps in
# as a secondary via NEW_TO_OLD_FULL). R-BSL (156) has no old equivalent.
NEW_TO_OLD_PRIMARY = {
    142: 3, 143: 3,                 # R-NIA, R-HAM → Golden Horseshoe South
    144: 1, 145: 1, 146: 1,         # R-GTA, R-YRK, R-DUR → GTA Central
    147: 4,                         # R-HDW → Central Ontario / Grey-Bruce
    148: 2,                         # R-WAT → Southwest Ontario
    149: 5, 159: 5,                 # R-NOR, R-KAW → East-Central Ontario
    150: 6,                         # R-SEO → Eastern Ontario (R11 secondary)
    151: 8, 152: 8,                 # R-MON, R-LAV → Greater Montreal
    153: 9,                         # R-CDQ → Central Quebec
    154: 10, 155: 10,               # R-QUE, R-CHA → Quebec City Region
    157: 7, 158: 7,                 # R-OTT, R-HAL → Ottawa Valley
}

# Inverse full sets: canonical region → every old region it covers.
NEW_TO_OLD_FULL = {
    142: [3], 143: [3],
    144: [1], 145: [1], 146: [1],
    147: [4],
    148: [2],
    149: [5], 159: [5],
    150: [6, 16],
    151: [8, 18], 152: [8, 18],
    153: [9, 19],
    154: [10, 20], 155: [10, 20],
    156: [],  # R-BSL (Bas-Saint-Laurent) — no old-region equivalent
    157: [7, 17], 158: [7, 17],
}


class RegionResolver:
    """Deterministic bridge between old-lane regions (1-20) and
    new-official LTL regions (142-159).

    Also resolves a coordinate to a Premafirm Service Region via Shapely
    point-in-polygon. Both capabilities share one service so every Odoo
    component normalizes region IDs through the same authority.

    Usage::

        resolver = RegionResolver(env)
        result = resolver.resolve(lat=43.2, lng=-79.8)
        if result.matched_region:
            print(result.matched_region.code)

        canonical = resolver.canonical_region("L5M")        # → R-GTA record
        old_id = resolver.map_new_to_old(canonical.id)      # → 1
        lanes = resolver.matching_lanes(canonical, dest)    # old-region lanes
    """

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env
        self._geometry_cache = {}  # keyed by (region_id, checksum)
        self._region_cache = {}    # region id → logistics.region record (request-scoped)

    # ── Public API ───────────────────────────────────────────────────

    def resolve(self, latitude, longitude, country=None, state=None):
        """Resolve a coordinate to a service region.

        Args:
            latitude:  float, WGS84 latitude
            longitude: float, WGS84 longitude
            country:   optional res.country record or ID to restrict search
            state:     optional res.country.state record or ID to restrict search

        Returns:
            RegionMatch namedtuple
        """
        from datetime import datetime

        timestamp = datetime.utcnow().isoformat() + "Z"

        # 1. Validate inputs
        valid, reason = self._validate_coordinates(latitude, longitude)
        if not valid:
            return RegionMatch(
                matched_region=None,
                matched_region_code=None,
                outcome="MANUAL_QUOTE",
                match_method="none",
                candidate_regions=[],
                ambiguity=False,
                ambiguity_detail="",
                reason=reason,
                match_timestamp=timestamp,
            )

        point = Point(longitude, latitude)  # GeoJSON order: (lng, lat)

        # 2. Check network enablement — must precede polygon search
        Country = self.env["res.country"]
        State = self.env["res.country.state"]

        if country:
            country_rec = Country.browse(int(country)) if isinstance(country, (int, str)) else country
            if country_rec.exists() and not country_rec.logistics_network_enabled:
                return RegionMatch(
                    matched_region=None, matched_region_code=None,
                    outcome="NETWORK_DISABLED", match_method="none",
                    candidate_regions=[], ambiguity=False, ambiguity_detail="",
                    reason=f"Country '{country_rec.name}' is disabled for Prema Logistics.",
                    match_timestamp=timestamp,
                )
        else:
            # Check if ALL countries are disabled (unlikely — but check Canada specifically)
            canada = Country.search([("code", "=", "CA")], limit=1)
            if canada and not canada.logistics_network_enabled:
                return RegionMatch(
                    matched_region=None, matched_region_code=None,
                    outcome="NETWORK_DISABLED", match_method="none",
                    candidate_regions=[], ambiguity=False, ambiguity_detail="",
                    reason="Canada is disabled for Prema Logistics.",
                    match_timestamp=timestamp,
                )

        if state:
            state_rec = State.browse(int(state)) if isinstance(state, (int, str)) else state
            if state_rec.exists() and not state_rec.logistics_network_enabled:
                return RegionMatch(
                    matched_region=None, matched_region_code=None,
                    outcome="NETWORK_DISABLED", match_method="none",
                    candidate_regions=[], ambiguity=False, ambiguity_detail="",
                    reason=f"Province/state '{state_rec.name}' is disabled for Prema Logistics.",
                    match_timestamp=timestamp,
                )

        # 3. Build region search domain with network filters
        region_domain = [
            ("active", "=", True),
            ("is_official_ltl_region", "=", True),
            ("boundary_status", "=", "approved"),
            ("polygon_geojson", "!=", False),
            ("polygon_geojson", "!=", ""),
            ("polygon_geojson", "!=", None),
        ]

        if country:
            region_domain.append(("country_id", "=", int(country)))
        else:
            region_domain.append(("country_id", "!=", False))
            region_domain.append(("country_id.logistics_network_enabled", "=", True))

        if state:
            region_domain.append(("state_id", "=", int(state)))
        else:
            region_domain.append(("state_id", "!=", False))
            region_domain.append(("state_id.logistics_network_enabled", "=", True))

        # 4. Load candidate regions
        Region = self.env["logistics.region"]
        candidates = Region.search(region_domain, order="match_priority DESC, boundary_area_km2 ASC")

        if not candidates:
            # If no state was explicitly passed, check if the point falls in a
            # disabled-province region → NETWORK_DISABLED
            if not state:
                disabled_result = self._check_disabled_state(point, timestamp)
                if disabled_result:
                    return disabled_result
            return RegionMatch(
                matched_region=None,
                matched_region_code=None,
                outcome="MANUAL_QUOTE",
                match_method="none",
                candidate_regions=[],
                ambiguity=False,
                ambiguity_detail="",
                reason="No active, approved region with a polygon found for the given constraints.",
                match_timestamp=timestamp,
            )

        # 4. Point-in-polygon test
        containing = []
        for region in candidates:
            polygon = self._get_geometry(region)
            if polygon is None:
                continue
            try:
                # Use covers() for boundary-inclusive matching
                if polygon.covers(point):
                    containing.append(region)
            except Exception:
                _logger.warning(
                    "Shapely error testing point against region %s (%s)",
                    region.code, region.id, exc_info=True,
                )
                continue

        # 6. No match
        if not containing:
            # Check for disabled-province fallthrough
            if not state:
                disabled_result = self._check_disabled_state(point, timestamp)
                if disabled_result:
                    return disabled_result
            return RegionMatch(
                matched_region=None,
                matched_region_code=None,
                outcome="MANUAL_QUOTE",
                match_method="none",
                candidate_regions=[],
                ambiguity=False,
                ambiguity_detail="",
                reason=f"Coordinate ({latitude}, {longitude}) is outside all "
                       f"configured service region polygons.",
                match_timestamp=timestamp,
            )

        # 6. Single match
        if len(containing) == 1:
            region = containing[0]
            return RegionMatch(
                matched_region=region,
                matched_region_code=region.code,
                outcome="SCHEDULED_MATCH",
                match_method="polygon",
                candidate_regions=containing,
                ambiguity=False,
                ambiguity_detail="",
                reason=f"Coordinate falls inside {region.code} ({region.name}).",
                match_timestamp=timestamp,
            )

        # 7. Multiple matches — resolve by priority + area
        containing_sorted = sorted(
            containing,
            key=lambda r: (-(r.match_priority or 10), (r.boundary_area_km2 or 999999)),
        )
        winner = containing_sorted[0]
        others = containing_sorted[1:]

        # Check if winner is unambiguous (highest priority + smallest area)
        # If the next region has same priority and similar area, it's ambiguous
        next_best = others[0] if others else None
        if next_best and (next_best.match_priority or 10) == (winner.match_priority or 10):
            ambiguity_detail = (
                f"Multiple regions contain this point: "
                + ", ".join(f"{r.code} ({r.name})" for r in containing_sorted)
                + ". Winner selected by priority and area but manual review recommended."
            )
            return RegionMatch(
                matched_region=winner,
                matched_region_code=winner.code,
                outcome="AMBIGUOUS",
                match_method="polygon",
                candidate_regions=containing,
                ambiguity=True,
                ambiguity_detail=ambiguity_detail,
                reason=f"Resolved to {winner.code} by priority {winner.match_priority} "
                       f"and area {winner.boundary_area_km2} km², but overlap exists. "
                       f"Manual review recommended.",
                match_timestamp=timestamp,
            )

        return RegionMatch(
            matched_region=winner,
            matched_region_code=winner.code,
            outcome="SCHEDULED_MATCH",
            match_method="polygon",
            candidate_regions=containing,
            ambiguity=False,
            ambiguity_detail="",
            reason=f"Resolved to {winner.code} by priority {winner.match_priority} "
                   f"over {', '.join(r.code for r in others)}.",
            match_timestamp=timestamp,
        )

    # ── Disabled-state detection ────────────────────────────────────

    def _check_disabled_state(self, point, timestamp):
        """After a polygon miss, check if the point falls in a region whose
        state is disabled. Returns NETWORK_DISABLED RegionMatch or None."""
        Region = self.env["logistics.region"]
        # Search with country filter only (no state network filter)
        disabled_candidates = Region.search([
            ("active", "=", True),
            ("is_official_ltl_region", "=", True),
            ("boundary_status", "=", "approved"),
            ("polygon_geojson", "!=", False),
            ("polygon_geojson", "!=", ""),
            ("polygon_geojson", "!=", None),
            ("country_id", "!=", False),
            ("country_id.logistics_network_enabled", "=", True),
        ])
        for region in disabled_candidates:
            if not region.state_id or not region.state_id.logistics_network_enabled:
                polygon = self._get_geometry(region)
                if polygon and polygon.covers(point):
                    return RegionMatch(
                        matched_region=None, matched_region_code=None,
                        outcome="NETWORK_DISABLED", match_method="none",
                        candidate_regions=[], ambiguity=False, ambiguity_detail="",
                        reason=f"Province/state '{region.state_id.name}' is disabled "
                               f"for Prema Logistics.",
                        match_timestamp=timestamp,
                    )
        return None

    # ── Coordinate validation ────────────────────────────────────────

    @staticmethod
    def _validate_coordinates(latitude, longitude):
        """Validate lat/lng values are within Earth bounds."""
        if latitude is None or longitude is None:
            return False, "Latitude and longitude are required."
        try:
            lat = float(latitude)
            lng = float(longitude)
        except (ValueError, TypeError):
            return False, f"Invalid coordinate values: ({latitude}, {longitude})."
        if not (-90.0 <= lat <= 90.0):
            return False, f"Latitude out of range: {lat} (must be -90 to 90)."
        if not (-180.0 <= lng <= 180.0):
            return False, f"Longitude out of range: {lng} (must be -180 to 180)."
        return True, "ok"

    # ── Geometry cache ───────────────────────────────────────────────

    def _get_geometry(self, region):
        """Return a Shapely geometry for the region's polygon, with caching."""
        region.ensure_one()
        cache_key = (region.id, region.boundary_checksum or "")
        if cache_key in self._geometry_cache:
            return self._geometry_cache[cache_key]

        geojson_str = region.polygon_geojson
        if not geojson_str:
            return None

        try:
            geojson = json.loads(geojson_str)
            geom = shape(geojson)
            self._geometry_cache[cache_key] = geom
            return geom
        except Exception:
            _logger.warning(
                "Failed to parse GeoJSON for region %s (%s)",
                region.code, region.id, exc_info=True,
            )
            return None

    def invalidate_cache(self, region):
        """Remove a region from the geometry cache (call after polygon update)."""
        region.ensure_one()
        for key_prefix in [(region.id, region.boundary_checksum or "")]:
            self._geometry_cache.pop(key_prefix, None)
        # Also clear any old checksum entries for this region ID
        stale = [k for k in self._geometry_cache if k[0] == region.id]
        for k in stale:
            del self._geometry_cache[k]

    # ── Geometry validation ──────────────────────────────────────────

    @staticmethod
    def validate_geometry(polygon_geojson):
        """Validate a GeoJSON polygon string.

        Returns:
            (is_valid: bool, message: str, repaired_geojson: str or None)
        """
        if not polygon_geojson or not polygon_geojson.strip():
            return False, "Polygon GeoJSON is empty.", None

        # 1. Valid JSON
        try:
            geojson = json.loads(polygon_geojson)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}", None

        # 2. Must be a dict with type
        if not isinstance(geojson, dict):
            return False, "GeoJSON must be a JSON object, not an array or scalar.", None

        geom_type = geojson.get("type", "")

        # 3. Must be Feature or Geometry
        if geom_type == "Feature":
            geom = geojson.get("geometry", {})
            if not geom:
                return False, "GeoJSON Feature has no 'geometry' property.", None
            geom_type = geom.get("type", "")
        elif geom_type in ("FeatureCollection", "GeometryCollection"):
            return False, f"GeoJSON type '{geom_type}' is not supported. Use Polygon or MultiPolygon.", None
        else:
            geom = geojson

        # 4. Only Polygon or MultiPolygon
        if geom_type not in ("Polygon", "MultiPolygon"):
            return False, f"Unsupported geometry type: '{geom_type}'. Only Polygon and MultiPolygon are allowed.", None

        # 5. Parse with Shapely
        try:
            shapely_geom = shape(geom)
        except Exception as e:
            return False, f"Shapely could not parse geometry: {e}", None

        # 6. Not empty
        if shapely_geom.is_empty:
            return False, "Geometry is empty.", None

        # 7. Valid according to Shapely
        if not shapely_geom.is_valid:
            validity_msg = explain_validity(shapely_geom)
            # Try to repair
            repaired = shapely_geom.buffer(0)
            if repaired.is_valid and not repaired.is_empty:
                from shapely.geometry import mapping
                repaired_geojson = json.dumps(mapping(repaired))
                return (
                    False,
                    f"Geometry is invalid: {validity_msg}. A repaired version is available.",
                    repaired_geojson,
                )
            return False, f"Geometry is invalid: {validity_msg}. Auto-repair failed.", None

        # 8. Check for impossible coordinates
        if geom_type == "Polygon":
            coords = geom.get("coordinates", [[]])[0]
        else:
            coords = []
            for polygon in geom.get("coordinates", []):
                coords.extend(polygon[0] if polygon else [])

        for coord in coords:
            if len(coord) >= 2:
                lng, lat = coord[0], coord[1]
                if not (-180 <= lng <= 180):
                    return False, f"Longitude {lng} out of range (-180 to 180).", None
                if not (-90 <= lat <= 90):
                    return False, f"Latitude {lat} out of range (-90 to 90).", None

        # 9. Area is reasonable (> 0, < 1/4 of Earth's land area)
        # Convert to km² using simple approximation at mid-latitudes
        area_deg = shapely_geom.area  # in square degrees
        # 1 deg lat ≈ 111.32 km, 1 deg lng ≈ 111.32 * cos(lat) km
        # For rough validation: area_deg × (111.32²) ≈ km²
        area_km2 = area_deg * (111.32 ** 2)
        if area_km2 <= 0:
            return False, "Polygon area is zero or negative.", None
        if area_km2 > 10_000_000:
            return False, f"Polygon area ({area_km2:,.0f} km²) is unreasonably large.", None

        return True, f"Valid. Approximate area: {area_km2:,.1f} km².", None

    @staticmethod
    def compute_area_km2(polygon_geojson):
        """Compute approximate area in km² from a GeoJSON polygon."""
        if not polygon_geojson:
            return 0.0
        try:
            geojson = json.loads(polygon_geojson)
            if geojson.get("type") == "Feature":
                geojson = geojson.get("geometry", {})
            geom = shape(geojson)
            if geom.is_empty:
                return 0.0
            area_deg = geom.area
            return area_deg * (111.32 ** 2)
        except Exception:
            return 0.0

    @staticmethod
    def compute_checksum(polygon_geojson):
        """Compute SHA-256 checksum of normalized polygon JSON."""
        if not polygon_geojson:
            return ""
        try:
            geojson = json.loads(polygon_geojson)
            # Normalize: sort keys for stable checksum
            normalized = json.dumps(geojson, sort_keys=True)
            return hashlib.sha256(normalized.encode()).hexdigest()
        except Exception:
            return ""

    # ── Overlap detection ────────────────────────────────────────────

    def detect_overlaps(self, regions=None):
        """Detect polygon overlaps between regions in the same province.

        Args:
            regions: optional recordset of regions to check (defaults to
                     all active, approved, official regions with polygons).

        Returns:
            list of dicts: {region_a, region_b, overlap_area_km2,
                            overlap_pct, severity}
        """
        Region = self.env["logistics.region"]
        if regions is None:
            regions = Region.search([
                ("active", "=", True),
                ("is_official_ltl_region", "=", True),
                ("boundary_status", "=", "approved"),
                ("polygon_geojson", "!=", False),
                ("polygon_geojson", "!=", ""),
                ("polygon_geojson", "!=", None),
            ])

        overlaps = []
        region_list = list(regions)
        for i in range(len(region_list)):
            for j in range(i + 1, len(region_list)):
                ra = region_list[i]
                rb = region_list[j]

                # Only compare within same province
                if ra.state_id != rb.state_id:
                    continue

                geom_a = self._get_geometry(ra)
                geom_b = self._get_geometry(rb)
                if geom_a is None or geom_b is None:
                    continue

                try:
                    intersection = geom_a.intersection(geom_b)
                    if intersection.is_empty:
                        continue

                    inter_area_deg = intersection.area
                    inter_area_km2 = inter_area_deg * (111.32 ** 2)

                    area_a = geom_a.area * (111.32 ** 2)
                    area_b = geom_b.area * (111.32 ** 2)

                    pct_a = (inter_area_km2 / area_a * 100) if area_a > 0 else 0
                    pct_b = (inter_area_km2 / area_b * 100) if area_b > 0 else 0

                    max_pct = max(pct_a, pct_b)

                    if max_pct < 0.01:
                        severity = "negligible"
                    elif max_pct < 1.0:
                        severity = "minor"
                    elif max_pct < 10.0:
                        severity = "moderate"
                    else:
                        severity = "major"

                    overlaps.append({
                        "region_a": ra,
                        "region_b": rb,
                        "overlap_area_km2": round(inter_area_km2, 3),
                        "overlap_pct_a": round(pct_a, 2),
                        "overlap_pct_b": round(pct_b, 2),
                        "severity": severity,
                    })
                except Exception:
                    _logger.warning(
                        "Overlap detection failed for %s / %s",
                        ra.code, rb.code, exc_info=True,
                    )
                    continue

        return overlaps

    # ═══════════════════════════════════════════════════════════════════
    # Region bridge — canonical region resolution (old ↔ new)
    #
    # THE single entry point for normalizing region references between
    # the old lane region system (IDs 1-20) and the official LTL region
    # system (IDs 142-159). Read-only: never modifies region data.
    # ═══════════════════════════════════════════════════════════════════

    def canonical_region(self, location):
        """Resolve ANY region-ish reference to the canonical official-LTL
        ``logistics.region`` record (IDs 142-159).

        Accepts:
          - ``logistics.region`` record or ID (old 1-20 or new 142-159)
          - ``logistics.saved.location`` record (override/detected region,
            then postal, then city)
          - ``prema.dispatch.location``, ``logistics.hub``,
            ``logistics.fsa``, ``logistics.corridor.stop``,
            ``logistics.booking.stop`` records
          - FSA or full postal code string ("L5M", "L5M 2C3")
          - region code string ("R1" .. "R15", "R-GTA")
          - city name string ("Mississauga", "Montréal")

        Returns an EMPTY recordset when nothing resolvable is found —
        callers must never assume a match.
        """
        region = self._location_to_region(location)
        if not region:
            return self.env["logistics.region"]
        canonical_id = self._bridge_region_id(region.id)
        if not canonical_id:
            return self.env["logistics.region"]
        return self._region_by_id(canonical_id)

    def map_old_to_new(self, old_region_id):
        """Return the PRIMARY new region ID (142-159) for an old region
        ID (1-20). Canonical IDs pass through unchanged. Returns False for
        unknown/unmapped IDs."""
        rid = self._as_region_id(old_region_id)
        if rid in OLD_TO_NEW_PRIMARY:
            return OLD_TO_NEW_PRIMARY[rid]
        if rid in NEW_TO_OLD_PRIMARY:
            return rid
        return False

    def map_new_to_old(self, new_region_id):
        """Return the PRIMARY old region ID (1-20) for a new/canonical
        region ID (142-159). Old IDs pass through unchanged. Returns False
        for unknown/unmapped IDs (e.g. R-BSL / 156)."""
        rid = self._as_region_id(new_region_id)
        if rid in NEW_TO_OLD_PRIMARY:
            return NEW_TO_OLD_PRIMARY[rid]
        if rid in OLD_TO_NEW_PRIMARY:
            return rid
        return False

    def resolve_lane_for_corridor_stop(self, corridor_stop):
        """Find the appropriate old-region lane for a corridor stop.

        The stop's ``region_id`` is a NEW region (142-159) while lanes are
        keyed by OLD regions (1-20). Resolution order:
          1. Exact lane on the corridor's deprecated start_hub/end_hub
             old-region FKs (either direction)
          2. Any active lane touching the stop's mapped old region,
             preferring a lane whose other endpoint is the corridor's
             opposite old hub region
          3. Any active lane touching the stop's old region

        Returns an EMPTY recordset when nothing matches.
        """
        Lane = self.env["logistics.lane"].sudo()
        stop = corridor_stop
        if not (hasattr(stop, "_name") and stop._name == "logistics.corridor.stop"):
            stop = self.env["logistics.corridor.stop"].sudo().browse(int(stop or 0))
        if not stop or not stop.exists():
            return Lane

        corridor = stop.corridor_id
        stop_old = self.map_new_to_old(stop.region_id.id) if stop.region_id else False
        if not stop_old:
            return Lane

        # 1. Exact corridor hub-pair lanes (legacy old-region FKs)
        if corridor and corridor.start_hub_id and corridor.end_hub_id:
            pair = (corridor.start_hub_id.id, corridor.end_hub_id.id)
            for a, b in (pair, (pair[1], pair[0])):
                lane = Lane.search([
                    ("origin_region_id", "=", a),
                    ("destination_region_id", "=", b),
                    ("active", "=", True),
                ], limit=1)
                if lane:
                    return lane

        # 2+3. Lanes touching this stop's old region
        lanes = Lane.search([
            "|",
            ("origin_region_id", "=", stop_old),
            ("destination_region_id", "=", stop_old),
            ("active", "=", True),
        ])
        if not lanes:
            return Lane
        other_old = False
        if corridor:
            if corridor.start_hub_id and corridor.start_hub_id.id != stop_old:
                other_old = corridor.start_hub_id.id
            elif corridor.end_hub_id and corridor.end_hub_id.id != stop_old:
                other_old = corridor.end_hub_id.id
        if other_old:
            preferred = lanes.filtered(
                lambda l: (l.destination_region_id.id if l.origin_region_id.id == stop_old
                           else l.origin_region_id.id) == other_old
            )
            if preferred:
                return preferred[:1]
        return lanes[:1]

    def matching_lanes(self, origin_region, dest_region):
        """Find all ACTIVE ``logistics.lane`` records (keyed by OLD region
        IDs) serving the movement between two canonical regions.

        Both endpoints are run through :meth:`canonical_region`, then
        expanded to their full old-region ID sets (e.g. R-SEO ← R6 + R11)
        so lanes on any historical region pair are found.
        """
        origin = self.canonical_region(origin_region)
        dest = self.canonical_region(dest_region)
        if not origin or not dest:
            return self.env["logistics.lane"]
        old_origins = NEW_TO_OLD_FULL.get(origin.id) or [self.map_new_to_old(origin.id)]
        old_dests = NEW_TO_OLD_FULL.get(dest.id) or [self.map_new_to_old(dest.id)]
        old_origins = [oid for oid in old_origins if oid]
        old_dests = [oid for oid in old_dests if oid]
        if not old_origins or not old_dests:
            return self.env["logistics.lane"]
        return self.env["logistics.lane"].sudo().search([
            ("origin_region_id", "in", old_origins),
            ("destination_region_id", "in", old_dests),
            ("active", "=", True),
        ], order="road_km")

    # ── Bridge internals ──────────────────────────────────────────────

    def _location_to_region(self, location):
        """Reduce any location-ish input to a logistics.region record
        (old OR new — not yet canonicalized)."""
        if location is None or location is False or location == 0:
            return self.env["logistics.region"]
        if hasattr(location, "_name"):
            model = location._name
            if model == "logistics.region":
                return location if location.exists() else self.env["logistics.region"]
            if model == "logistics.saved.location":
                if location.override_region_id:
                    return location.override_region_id
                if location.detected_region_id:
                    return location.detected_region_id
                if location.postal_code:
                    return self._postal_to_region(location.postal_code)
                if location.city:
                    return self._city_to_region(location.city)
                return self.env["logistics.region"]
            if model == "logistics.hub":
                return location.canonical_region_id or self.env["logistics.region"]
            if model == "logistics.fsa":
                return location.region_id or self.env["logistics.region"]
            if model == "logistics.corridor.stop":
                return location.region_id or self.env["logistics.region"]
            if model in ("logistics.booking.stop", "logistics.pricing.session.stop"):
                postal = location.postal_zip or location.postal_code or ""
                if postal:
                    return self._postal_to_region(postal)
                if location.city:
                    return self._city_to_region(location.city)
                return self.env["logistics.region"]
            if model == "prema.dispatch.location":
                if location.postal_code:
                    return self._postal_to_region(location.postal_code)
                if location.city:
                    return self._city_to_region(location.city)
                return self.env["logistics.region"]
            # Generic record fallback: any model carrying postal/city fields
            try:
                if location._fields.get("postal_code") and location.postal_code:
                    return self._postal_to_region(location.postal_code)
                if location._fields.get("city") and location.city:
                    return self._city_to_region(location.city)
            except Exception:
                pass
            return self.env["logistics.region"]
        if isinstance(location, (int, str)):
            text = str(location).strip()
            if text.isdigit():
                rid = int(text)
                return self._region_by_id(rid) if rid else self.env["logistics.region"]
            return self._string_to_region(text)
        return self.env["logistics.region"]

    def _string_to_region(self, text):
        """Resolve a string: region code → FSA/postal → city name."""
        original = str(text).strip()
        upper = original.upper()
        if not upper:
            return self.env["logistics.region"]
        # 1. Region code — new-style (R-GTA) or legacy (R1..R15).
        #    Legacy regions are archived (active=False), so the implicit
        #    active filter must be disabled for code lookups.
        if re.match(r"^R-[A-Z]{3}$", upper) or re.match(r"^R\d{1,2}$", upper):
            region = self.env["logistics.region"].sudo().with_context(
                active_test=False,
            ).search([("code", "=ilike", upper)], limit=1)
            if region:
                return region
        # 2. FSA / postal code — bare 3-char FSA ("L5M") or full postal
        #    ("L5M 2C3", "L5M2C3")
        compact = re.sub(r"[\s\-]", "", upper)
        if re.match(r"^[A-Z]\d[A-Z]", compact):
            region = self._postal_to_region(compact)
            if region:
                return region
        # 3. City name
        return self._city_to_region(original)

    def _disambiguate_region(self, full_new_ids, fsa_code, display_city):
        """Pick the best canonical region from a FULL set using FSA context.

        Strategy (in order):
        1. Exact display_city ↔ main_city match
        2. display_city keyword found in canonical region name
           (e.g. "NIAGARA FALLS" → "Niagara Region")
        3. FSA prefix rule for known ambiguous splits
        """
        city = (display_city or "").strip().upper()
        # 1. Exact city match
        for nid in full_new_ids:
            rec = self._region_by_id(nid)
            if rec and (rec.main_city or "").upper() == city:
                return rec
        # 2. Keyword overlap — each word in city must appear in region name
        if city:
            city_words = set(city.split())
            for nid in full_new_ids:
                rec = self._region_by_id(nid)
                if rec:
                    name_upper = (rec.name or "").upper()
                    if any(w in name_upper for w in city_words if len(w) > 2):
                        return rec
        # 3. Known FSA prefix splits
        fsa_prefix = (fsa_code or "")[:2].upper()
        if fsa_prefix in ("L2",):   # Niagara Falls / St. Catharines
            for nid in full_new_ids:
                rec = self._region_by_id(nid)
                if rec and "NIAGARA" in (rec.name or "").upper():
                    return rec
        if fsa_prefix in ("L8", "L9"):  # Hamilton / Burlington
            for nid in full_new_ids:
                rec = self._region_by_id(nid)
                if rec and "HAMILTON" in (rec.name or "").upper():
                    return rec
        return self._region_by_id(full_new_ids[0])

    def _postal_to_region(self, postal):
        """Resolve an FSA or full postal code to its CANONICAL region.

        Goes through logistics.fsa → old region_id → bridge. When an old
        region maps to multiple canonical regions (e.g. old 3 → R-HAM +
        R-NIA), disambiguates using display_city and FSA prefix.
        """
        cleaned = re.sub(r"[\s\-]", "", str(postal or "")).upper()
        fsa_code = cleaned[:3]
        if not re.match(r"^[A-Z]\d[A-Z]$", fsa_code):
            return self.env["logistics.region"]
        fsa = self.env["logistics.fsa"].sudo().search(
            [("fsa", "=", fsa_code)], limit=1)
        if not (fsa and fsa.region_id):
            return self.env["logistics.region"]
        old_id = fsa.region_id.id
        full_new_ids = OLD_TO_NEW_FULL.get(old_id, [old_id])
        if len(full_new_ids) == 1:
            return self._region_by_id(full_new_ids[0])
        return self._disambiguate_region(
            full_new_ids, fsa_code, fsa.display_city or "")

    def _city_to_region(self, city):
        """Resolve a city name to a region via logistics.city, saved
        locations, then region main_city."""
        name = str(city or "").strip()
        if not name:
            return self.env["logistics.region"]
        cities = self.env["logistics.city"].sudo().search(
            [("name", "=ilike", name)], limit=5)
        for city_rec in cities:
            if city_rec.region_id:
                return city_rec.region_id
        locs = self.env["logistics.saved.location"].sudo().search(
            [("city", "=ilike", name)], limit=5)
        for loc in locs:
            if loc.detected_region_id:
                return loc.detected_region_id
        # Fallback: region main_city (new regions), hub_name (old regions),
        # then region name itself.
        Region = self.env["logistics.region"].sudo().with_context(active_test=False)
        region = Region.search([("main_city", "=ilike", name)], limit=1)
        if not region:
            region = Region.search([("hub_name", "=ilike", name)], limit=1)
        if not region:
            region = Region.search([("name", "=ilike", name)], limit=1)
        return region or self.env["logistics.region"]

    def _bridge_region_id(self, region_id):
        """Map any known region ID to its canonical official-LTL ID
        (142-159). Returns False for unknown or unmapped IDs."""
        if not region_id:
            return False
        rid = int(region_id)
        if rid in NEW_TO_OLD_PRIMARY:
            return rid  # already canonical
        if rid in OLD_TO_NEW_PRIMARY:
            return OLD_TO_NEW_PRIMARY[rid]
        rec = self._region_by_id(rid)
        if rec and rec.is_official_ltl_region:
            return rid  # canonical by flag
        return False

    def _region_by_id(self, region_id):
        """Sudo-read a logistics.region record with request-level caching."""
        if not region_id:
            return self.env["logistics.region"]
        rid = int(region_id)
        if rid in self._region_cache:
            return self._region_cache[rid]
        rec = self.env["logistics.region"].sudo().browse(rid)
        rec = rec if rec.exists() else self.env["logistics.region"]
        self._region_cache[rid] = rec
        return rec

    @staticmethod
    def _as_region_id(ref):
        """Reduce a region record / numeric ID / numeric string to an
        integer region ID; returns False otherwise."""
        if ref is None or ref is False:
            return False
        if hasattr(ref, "_name") and ref._name == "logistics.region":
            return int(ref.id) if ref.id and isinstance(ref.id, int) else False
        if isinstance(ref, int):
            return int(ref)
        text = str(ref).strip()
        if text.isdigit():
            return int(text)
        return False

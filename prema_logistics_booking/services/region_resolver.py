"""Canonical geographic region resolver for Prema Logistics.

Single authority for matching a latitude/longitude coordinate to a
Premafirm Service Region via Shapely point-in-polygon.

All Odoo components MUST use this service instead of independently
implementing polygon logic.

Input:   latitude, longitude, optional country/province filters
Output:  matched region, method, candidates, ambiguity status, reason

Uses Shapely 2.1.2 (already installed in venv-18).
"""

import hashlib
import json
import logging
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


class RegionResolver:
    """Resolve a coordinate to a Premafirm Service Region.

    Usage::

        resolver = RegionResolver(env)
        result = resolver.resolve(lat=43.2, lng=-79.8)
        if result.matched_region:
            print(result.matched_region.code)
    """

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env
        self._geometry_cache = {}  # keyed by (region_id, checksum)

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

"""Canonical Google Places service — the ONE Places integration.

Every Google resolution in the booking module (portal Saved Location
server-side verification, the batch-verify action on
logistics.saved.location, address repairs) goes through this service.
New Places API v1 (places.googleapis.com) with the legacy
maps.googleapis.com details / findplacefromtext fallback — the same pair
the original batch verifier used, kept in one place.

A Place ID is NEVER trusted on its own: the caller only marks a location
Google-verified when this service returns a valid coordinate pair
(in-range, and not the 0.0/0.0 placeholder — a single zero component,
e.g. the Equator, is legitimate).
"""

import json
import logging
import urllib.parse
import urllib.request

_logger = logging.getLogger(__name__)

_ICP_KEY = "google_maps_api_key"

_LEGACY_FIELDS = "place_id,formatted_address,geometry,address_components,name"


def valid_coordinate_pair(latitude, longitude):
    """True only for a usable physical coordinate pair.

    Both values must be in range AND the pair must not be the 0.0/0.0
    placeholder; a legitimate coordinate with one exact zero component
    (e.g. the Equator) is accepted.
    """
    if latitude is None or longitude is None:
        return False
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return False
    if lat == 0.0 and lng == 0.0:
        return False
    return True


class GooglePlacesService:
    """Resolve Google Place IDs / address searches to verified physical
    data. Stateless over env; every method returns plain dicts."""

    def __init__(self, env):
        self.env = env

    def _api_key(self):
        key = self.env["ir.config_parameter"].sudo().get_param(_ICP_KEY, "")
        return key.strip() if key else ""

    # ── New Places API v1 ─────────────────────────────────────────────

    def _v1_details(self, place_id, api_key):
        url = "https://places.googleapis.com/v1/places/%s?key=%s" % (
            urllib.parse.quote(place_id), api_key)
        req = urllib.request.Request(url, headers={"X-Goog-FieldMask": (
            "id,formattedAddress,location,addressComponents,displayName")})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            _logger.debug("Places v1 details failed for %s: %s", place_id, exc)
            return None

    def _v1_search(self, query, api_key):
        params = urllib.parse.urlencode({"query": query, "key": api_key})
        url = "https://places.googleapis.com/v1/places:searchText?%s" % params
        req = urllib.request.Request(url, headers={"X-Goog-FieldMask": (
            "places.id,places.formattedAddress")})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            places = data.get("places") or []
            return [p for p in places if p.get("id")]
        except Exception as exc:
            _logger.debug("Places v1 search failed for %r: %s", query, exc)
            return []

    # ── Legacy Places API fallback ────────────────────────────────────

    def _legacy_details(self, place_id, api_key):
        url = ("https://maps.googleapis.com/maps/api/place/details/json"
               "?place_id=%s&fields=%s&key=%s" % (
                   urllib.parse.quote(place_id), _LEGACY_FIELDS, api_key))
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            return data.get("result") or None
        except Exception as exc:
            _logger.debug("Legacy place details failed for %s: %s", place_id, exc)
            return None

    def _legacy_search(self, query, api_key):
        url = ("https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
               "?input=%s&inputtype=textquery&fields=place_id&key=%s" % (
                   urllib.parse.quote(query), api_key))
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            candidates = data.get("candidates") or []
            return [c for c in candidates if c.get("place_id")]
        except Exception as exc:
            _logger.debug("Legacy findplacefromtext failed for %r: %s", query, exc)
            return []

    # ── Component extraction (both API shapes) ────────────────────────

    @staticmethod
    def _long_name(comp):
        return comp.get("longText") or comp.get("long_name") or ""

    @staticmethod
    def _short_name(comp):
        return comp.get("shortText") or comp.get("short_name") or ""

    @staticmethod
    def _comps_by_type(comps):
        out = {}
        for comp in comps or []:
            for t in comp.get("types") or []:
                out.setdefault(t, comp)
        return out

    # ── Public API ────────────────────────────────────────────────────

    def resolve_place(self, place_id):
        """Resolve a Google Place ID to verified physical data.

        Returns None when the place is unknown, the API is unavailable,
        or the returned coordinates are unusable (out of range or the
        0.0/0.0 placeholder). On success returns:

            {place_id, latitude, longitude, formatted_address, street,
             city, province, province_code, postal_code, country_code}

        The canonical Place ID returned by Google replaces the submitted
        one (new Places API may canonicalize case).
        """
        if not place_id or not str(place_id).strip():
            return None
        place_id = str(place_id).strip()
        api_key = self._api_key()
        if not api_key:
            _logger.warning("Google Places: no %s configured", _ICP_KEY)
            return None

        data = self._v1_details(place_id, api_key)
        v1 = bool(data and data.get("formattedAddress"))
        if not v1:
            data = self._legacy_details(place_id, api_key)
            if not data:
                return None

        if v1:
            loc = data.get("location") or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")
            comps = data.get("addressComponents") or []
            canonical_id = data.get("id") or place_id
        else:
            loc = (data.get("geometry") or {}).get("location") or {}
            lat, lng = loc.get("lat"), loc.get("lng")
            comps = data.get("address_components") or []
            canonical_id = data.get("place_id") or place_id

        if not valid_coordinate_pair(lat, lng):
            return None

        by_type = self._comps_by_type(comps)
        street_number = self._long_name(by_type.get("street_number") or {})
        route = self._long_name(by_type.get("route") or {})
        province = self._long_name(by_type.get("administrative_area_level_1") or {})
        return {
            "place_id": canonical_id,
            "latitude": float(lat),
            "longitude": float(lng),
            "formatted_address": (
                data.get("formattedAddress") or data.get("formatted_address") or ""),
            "street": " ".join(p for p in (street_number, route) if p),
            "city": self._long_name(by_type.get("locality") or {}) or self._long_name(
                by_type.get("administrative_area_level_3") or {}) or self._long_name(
                by_type.get("sublocality") or {}) or self._long_name(
                by_type.get("postal_town") or {}),
            "province": province,
            "province_code": self._short_name(
                by_type.get("administrative_area_level_1") or {}),
            "postal_code": self._long_name(by_type.get("postal_code") or {}),
            "country_code": self._short_name(by_type.get("country") or {}),
        }

    def search_address(self, query):
        """Text-search an address and return the first candidate's
        canonical Place ID, or None. Used to attach a Place ID to an
        existing address (repairs) — never marks anything verified on
        its own; callers must resolve_place() the returned ID."""
        if not query or not str(query).strip():
            return None
        query = str(query).strip()
        api_key = self._api_key()
        if not api_key:
            return None
        places = self._v1_search(query, api_key)
        if not places:
            places = self._legacy_search(query, api_key)
        if not places:
            return None
        return places[0].get("id") or places[0].get("place_id") or None

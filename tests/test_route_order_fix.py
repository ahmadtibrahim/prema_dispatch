"""Google routing + route-order primitives (Manual-UAT part 5).

DispatchRouteService tests — run in the prema_dispatch test phase (the
Booking-185 integration tests live in prema_logistics_booking/tests/
test_route_order_fix_booking.py, which needs the booking models loaded).

Covers:
  * Google road routing PRIMARY (region=ca, alternatives=false — no USA
    detours) with a deterministic straight-line ×1.4 @ 50 km/h fallback
    (never silent zeros).
  * USA-dip guard on the decoded polyline: a route shape dipping south
    of the route's own stops is flagged.
  * The adviser's travel uses Google minutes when available.
"""
from odoo.tests import TransactionCase

from odoo.addons.prema_dispatch.services.route_adviser_service import (
    RouteAdviserService,
)
from odoo.addons.prema_dispatch.services.route_service import (
    DispatchRouteService,
    _straight_line_km,
)

BRAMPTON = (43.755959, -79.692568)
BELLEVILLE = (44.183661, -77.394851)
OTTAWA = (45.421500, -75.697200)


def _encode_polyline(points):
    """Google polyline encoder (test side of the decoder)."""
    out = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        lat_e5 = int(round(lat * 1e5))
        lng_e5 = int(round(lng * 1e5))
        for value in (lat_e5 - prev_lat, lng_e5 - prev_lng):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lng = lat_e5, lng_e5
    return "".join(out)


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            raise Exception("HTTP %s" % self.status_code)


class TestRouteOrderFix(TransactionCase):

    def test_01_fallback_straight_line_when_no_api_key(self):
        """No API key → straight-line ×1.4 @ 50 km/h legs, marked as
        fallback — never silent zeros."""
        self.env["ir.config_parameter"].sudo().set_param(
            "google_maps_api_key", "")
        legs = DispatchRouteService(self.env).get_sequential_travel(
            [BRAMPTON, BELLEVILLE])
        self.assertEqual(legs._source, "fallback")
        expected_km = round(_straight_line_km(BRAMPTON, BELLEVILLE), 1)
        self.assertEqual(legs[0]["distance_km"], expected_km)
        self.assertEqual(legs[0]["drive_minutes"],
                         round(expected_km / 50.0 * 60.0))
        self.assertFalse(legs._us_dip_detected)

    def test_02_google_primary_with_region_ca_and_dip_guard(self):
        """API key present → Google legs used, region=ca requested, and a
        polyline dipping south of the stops' own latitude is flagged as a
        USA detour while a clean corridor shape is not."""
        import requests

        self.env["ir.config_parameter"].sudo().set_param(
            "google_maps_api_key", "test-key")
        captured = {}

        def fake_get(url, **kwargs):
            captured["params"] = kwargs.get("params") or {}
            polyline = captured.get("polyline")
            return _FakeResponse(200, {
                "status": "OK",
                "routes": [{
                    "legs": [{
                        "duration": {"value": 1200, "text": "20 mins"},
                        "distance": {"value": 25000, "text": "25.0 km"},
                    }],
                    "overview_polyline": {"points": polyline},
                }],
            })

        self.patch(requests, "get", fake_get)
        svc = DispatchRouteService(self.env)

        # Clean 401-corridor shape: Brampton → Kingston-ish → Ottawa.
        captured["polyline"] = _encode_polyline([
            (43.80, -79.60), (44.20, -77.40), (45.42, -75.70)])
        legs = svc.get_sequential_travel([BRAMPTON, OTTAWA])
        self.assertEqual(legs._source, "google")
        self.assertEqual(legs[0]["drive_minutes"], 20)
        self.assertEqual(captured["params"].get("region"), "ca")
        self.assertEqual(captured["params"].get("alternatives"), "false")
        self.assertFalse(legs._us_dip_detected)

        # Syracuse-style USA dip: well south of the stops' 43.7°N floor.
        captured["polyline"] = _encode_polyline([
            (43.80, -79.60), (42.80, -78.00), (45.42, -75.70)])
        legs = svc.get_sequential_travel([BRAMPTON, OTTAWA])
        self.assertTrue(legs._us_dip_detected)

    def test_03_google_travel_is_primary_for_adviser(self):
        """With Google available, the adviser's travel minutes come from
        road data (20 min mock), not the ~5-hour straight-line estimate."""
        import requests

        self.env["ir.config_parameter"].sudo().set_param(
            "google_maps_api_key", "test-key")

        def fake_get(url, **kwargs):
            return _FakeResponse(200, {
                "status": "OK",
                "routes": [{
                    "legs": [{
                        "duration": {"value": 1200, "text": "20 mins"},
                        "distance": {"value": 25000, "text": "25.0 km"},
                    }],
                    "overview_polyline": {"points": _encode_polyline([
                        (43.80, -79.60), (44.20, -77.40)])},
                }],
            })

        self.patch(requests, "get", fake_get)
        adviser = RouteAdviserService(self.env)
        minutes = adviser._travel_minutes(
            {"latitude": BRAMPTON[0], "longitude": BRAMPTON[1]},
            {"latitude": BELLEVILLE[0], "longitude": BELLEVILLE[1]},
        )
        self.assertEqual(minutes, 20.0)
        # Cache-hit on the identical pair → same answer, no extra call.
        minutes_again = adviser._travel_minutes(
            {"latitude": BRAMPTON[0], "longitude": BRAMPTON[1]},
            {"latitude": BELLEVILLE[0], "longitude": BELLEVILLE[1]},
        )
        self.assertEqual(minutes_again, 20.0)

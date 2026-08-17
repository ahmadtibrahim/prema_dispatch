"""Shared Google API mocks for prema_dispatch automated tests.

Provides deterministic fixture responses so tests never call live Google APIs.
Import and call `install_google_mocks(test_case)` in setUp().

Uses Odoo's `self.patch()` for clean integration with the test framework.
"""
import json


def _mock_directions_response():
    return {
        "status": "OK",
        "routes": [{
            "legs": [{
                "distance": {"value": 25000, "text": "25.0 km"},
                "duration": {"value": 1200, "text": "20 mins"},
                "end_address": "Destination, ON, Canada",
                "start_address": "Origin, ON, Canada",
            }],
            "overview_polyline": {"points": "_fake_polyline_"},
        }],
    }


def _mock_geocode_response(lat=43.7, lng=-79.4):
    return {
        "status": "OK",
        "results": [{
            "formatted_address": "Test Address, ON, Canada",
            "geometry": {
                "location": {"lat": lat, "lng": lng},
                "location_type": "ROOFTOP",
            },
            "types": ["premise"],
        }],
    }


def _mock_validate_address_response():
    return {
        "result": {
            "verdict": {
                "inputGranularity": "PREMISE",
                "validationGranularity": "PREMISE",
                "geocodeGranularity": "PREMISE",
                "addressComplete": True,
            },
            "address": {
                "formattedAddress": "Test Address, ON, Canada",
                "postalAddress": {
                    "regionCode": "CA", "administrativeArea": "ON",
                    "locality": "Test City", "postalCode": "A1A 1A1",
                },
            },
            "geocode": {"location": {"latitude": 43.7, "longitude": -79.7}},
        },
    }


def _mock_request(method, url, **kwargs):
    """Route to the right fixture based on URL. Returns (status_code, json_body)."""
    url_str = url if isinstance(url, str) else str(url)

    if "maps.googleapis.com/maps/api/directions" in url_str:
        return 200, _mock_directions_response()
    elif "maps.googleapis.com/maps/api/geocode" in url_str:
        return 200, _mock_geocode_response()
    elif "addressvalidation.googleapis.com" in url_str:
        return 200, _mock_validate_address_response()
    elif "fcm.googleapis.com" in url_str:
        return 200, {"success": True}
    # For any other URL, let it through to the real handler (will be blocked in test mode)
    return None, None


def install_google_mocks(test_case):
    """Install mocks on a test case using Odoo's self.patch().
    Call in setUp() after super().setUp()."""

    # Patch the requests module inside the Odoo addons that use it
    import requests as real_requests

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data
            self.text = json.dumps(json_data) if json_data else ""
            self.ok = 200 <= status_code < 300

        def json(self):
            return self._json_data

        def raise_for_status(self):
            if not self.ok:
                raise Exception(f"HTTP {self.status_code}")

    def mock_get(url, **kwargs):
        status, data = _mock_request('GET', url, **kwargs)
        if data is not None:
            return MockResponse(status, data)
        # Let unmocked URLs go through to real requests (Odoo test mode blocks them)
        return real_requests.get(url, **kwargs)

    def mock_post(url, **kwargs):
        status, data = _mock_request('POST', url, **kwargs)
        if data is not None:
            return MockResponse(status, data)
        return real_requests.post(url, **kwargs)

    # Patch at the module level where requests is used — Odoo's
    # BaseCase.patch takes (obj, key, val); a string path would be
    # setattr'd onto the requests module itself and explode.
    test_case.patch(real_requests, 'get', mock_get)
    test_case.patch(real_requests, 'post', mock_post)

    # Also disable FCM push notifications
    test_case.env['ir.config_parameter'].sudo().set_param('mail_mobile.fcm_enabled', 'False')

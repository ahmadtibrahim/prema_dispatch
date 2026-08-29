# -*- coding: utf-8 -*-
"""18-section work order §19: Google Maps / Routes API migration.

- driver_app.js and dispatch_board.js drop the retired DirectionsService /
  DirectionsRenderer pair (Google retirement 2026-02-25) for the Routes
  API (computeRoutes) with debounce + stale-token guards in the driver app
  and token guards on the dispatcher board
- saved_locations_portal._GOOGLE_INSTRUCTION is module-level _lt, not
  _(): a lazily-imported controller module would otherwise bake the
  request-language translation into the constant forever

Run: --test-tags /prema_logistics_booking/tests/test_phase19_routes_migration
"""
import os

from odoo.tests import TransactionCase

_ADDONS = "/opt/odoo/custom-addons"


def _js_source(rel):
    with open(os.path.join(_ADDONS, "prema_dispatch", rel), "r",
              encoding="utf-8") as fh:
        return fh.read()


class TestPhase19RoutesMigration(TransactionCase):
    """§19 Routes API migration guards."""

    def test_a_google_instruction_is_lazy(self):
        """_GOOGLE_INSTRUCTION must be a lazy translation — never an eager
        module-level _() that bakes the first request's language."""
        from odoo.addons.prema_logistics_booking.controllers import (
            saved_locations_portal as slp)
        from odoo.tools.translate import _lt
        value = slp._GOOGLE_INSTRUCTION
        self.assertFalse(isinstance(value, str),
                         "module-level constant must not be a baked string")
        # _lt returns lazy_translate instances; translate() yields English
        # under no request context.
        self.assertTrue(
            isinstance(value, type(_lt("x"))),
            f"expected lazy translation, got {type(value).__name__}")
        self.assertIn("Google address suggestions", str(value))

    def test_b_driver_app_uses_routes_api(self):
        """driver_app.js: computeRoutes fetch present, DirectionsService /
        DirectionsRenderer constructors gone, debounce + stale-token guards
        in place."""
        js = _js_source("static/src/js/driver_app.js")
        self.assertIn("routes.googleapis.com/directions/v2:computeRoutes", js)
        self.assertIn("X-Goog-Api-Key", js)
        self.assertIn("X-Goog-FieldMask", js)
        self.assertIn("decodePath", js)
        # Debounce + stale-token machinery.
        self.assertIn("S.routeToken", js)
        self.assertIn("AbortController", js)
        self.assertIn("S.routeTimer=setTimeout", js)
        self.assertIn("token!==S.routeToken", js)
        self.assertNotIn("new google.maps.DirectionsService", js)
        self.assertNotIn("new google.maps.DirectionsRenderer", js)

    def test_c_board_uses_routes_api(self):
        """dispatch_board.js: computeRoutes helper present, deprecated
        constructors gone, token guard on both route draws."""
        js = _js_source("static/src/js/dispatch_board.js")
        self.assertIn("routes.googleapis.com/directions/v2:computeRoutes", js)
        self.assertIn("_routesApiRequest", js)
        self.assertIn("_applyRoutePolyline", js)
        self.assertIn("++this._routeToken", js)
        self.assertIn("token !== this._routeToken", js)
        self.assertNotIn("new google.maps.DirectionsService", js)
        self.assertNotIn("new google.maps.DirectionsRenderer", js)
        self.assertNotIn("_dirService", js)
        self.assertNotIn("_dirRenderer", js)
        self.assertNotIn("_highlightRenderer", js)

    def test_d_routes_url_matches_api_shape(self):
        """The computeRoutes URL + field mask are the v2 GA contract."""
        js = _js_source("static/src/js/driver_app.js")
        self.assertIn(
            "routes.distanceMeters,routes.duration,"
            "routes.polyline.encodedPolyline", js)
        self.assertIn("travelMode:\"DRIVE\"", js)
        self.assertIn("routingPreference:\"TRAFFIC_AWARE\"", js)
        self.assertIn("routeModifiers", js)

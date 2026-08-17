"""Canonical optimizer + route-order fix, booking phase (Manual-UAT part 5).

Runs in the prema_logistics_booking test phase (after prema_dispatch is
loaded, so both dispatch AND booking models exist — including
logistics.saved.location, which the prema_dispatch phase lacks).

Covers:
  * optimize_route() delegates movement-bearing jobs to the planner-
    backed adviser (canonical unification) and fixes the Booking-185
    backtracking bug: Brampton → Belleville → Belleville → Ottawa, with
    same-town stops always consecutive.
  * Legacy jobs (no items) get the same same-city clustering plus the
    point-to-point distance fix (never chained-leg misreads).
  * ItineraryPlanner clusters same-city stops directly, precedence-safe.
  * A USA-detour dip surfaced by Google routing appears in the adviser
    report warnings.
"""
import datetime

from odoo.tests import TransactionCase

from odoo.addons.prema_dispatch.services.route_adviser_service import (
    RouteAdviserService,
)
from odoo.addons.prema_dispatch.services.optimization_service import (
    DispatchOptimizationService,
)
from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
    ItineraryPlanner,
)


def _dt(hour, minute=0):
    """Naive UTC (Odoo convention; container TZ is UTC): 12:00 = 08:00
    Toronto (EDT) — deterministic timezone conversions."""
    return datetime.datetime(2026, 8, 19, 12, 0) + datetime.timedelta(
        hours=hour - 12, minutes=minute
    )


def _encode_polyline(points):
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


class TestRouteOrderFixBooking(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].search([], limit=1)
        cls.stage_draft = cls.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1)
        # Booking 185 corridor coordinates.
        cls.BRAMPTON = (43.755959, -79.692568)
        cls.BELLEVILLE = (44.183661, -77.394851)
        cls.NOFRILLS = (44.176200, -77.407900)
        cls.OTTAWA = (45.421500, -75.697200)
        cls.open_24 = {str(d): [0.0, 24.0] for d in range(7)}

    def _make_job(self):
        return self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "scheduled_pickup": _dt(12, 0),
        })

    def _city_location(self, name, city, latlng):
        # Dispatch stops' saved_location_id points at the internal
        # prema.dispatch.location mirror, not the commercial booking
        # saved location — create the mirror with its city key AND pin:
        # stop.create() → _apply_saved_location copies location.pin_lat /
        # pin_lng over the stop's own lat/lng (a pin-less location would
        # zero the stop's coordinates and short-circuit travel estimates).
        lat, lng = latlng
        return self.env["prema.dispatch.location"].create({
            "name": name, "city": city, "active": True,
            "address": name, "address_validated": True,
            "pin_lat": lat, "pin_lng": lng, "pin_set": True,
        })

    def _add_stop(self, job, stop_type, name, latlng, seq, location=None,
                  pallets_in=0, pallets_out=0):
        lat, lng = latlng
        values = {
            "job_id": job.id,
            "sequence": seq,
            "stop_type": stop_type,
            "address": name,
            "latitude": lat,
            "longitude": lng,
            "pallets_in": pallets_in,
            "pallets_out": pallets_out,
            "operating_hours_snapshot": self.open_24,
            "tz_name": "America/Toronto",
        }
        if location is not None:
            values["saved_location_id"] = location.id
        return self.env["prema.dispatch.stop"].create(values)

    def _add_item(self, job, name, pickup_stop, delivery_stop):
        return self.env["prema.dispatch.item"].create({
            "job_id": job.id,
            "name": name,
            "weight_lbs": 500.0,
            "pickup_stop_id": pickup_stop.id,
            "delivery_stop_id": delivery_stop.id,
        })

    def _booking185_job(self, with_items=True):
        """United Dairy Brampton pickup; Healthy Planet + NOFRILLS
        Belleville deliveries; McDonough's Ottawa delivery — the exact
        Booking-185 shape whose old order put Ottawa between the two
        Belleville stops."""
        job = self._make_job()
        loc_ud = self._city_location("United Dairy", "Brampton", self.BRAMPTON)
        loc_hp = self._city_location("Healthy Planet", "Belleville",
                                     self.BELLEVILLE)
        loc_nf = self._city_location("NOFRILLS 211", "Belleville",
                                     self.NOFRILLS)
        loc_mc = self._city_location("McDonough's", "Ottawa", self.OTTAWA)
        ud = self._add_stop(job, "pickup", "United Dairy Brampton",
                            self.BRAMPTON, 10, location=loc_ud, pallets_in=4)
        hp = self._add_stop(job, "dropoff", "Healthy Planet Belleville",
                            self.BELLEVILLE, 20, location=loc_hp, pallets_out=2)
        mc = self._add_stop(job, "dropoff", "McDonough's Ottawa",
                            self.OTTAWA, 30, location=loc_mc, pallets_out=1)
        nf = self._add_stop(job, "dropoff", "NOFRILLS Bell Blvd",
                            self.NOFRILLS, 40, location=loc_nf, pallets_out=1)
        if with_items:
            self._add_item(job, "P-1", ud, hp)
            self._add_item(job, "P-2", ud, hp)
            self._add_item(job, "P-3", ud, mc)
            self._add_item(job, "P-4", ud, nf)
        return job, ud, hp, mc, nf

    # ── Canonical delegation + route-order fix ───────────────────────

    def test_01_optimizer_delegates_to_planner_and_clusters_cities(self):
        """Booking-185 shape: the canonical path (movement-bearing job)
        produces Brampton → Belleville → Belleville → Ottawa — same-town
        stops consecutive, never split by Ottawa."""
        job, ud, hp, mc, nf = self._booking185_job(with_items=True)
        result = DispatchOptimizationService(self.env).optimize_route(job.id)
        self.assertEqual(result.get("basis"), "planner")
        order = result["new_order"]
        self.assertEqual(order[0], ud.id, "Pickup must stay first")
        idx = {sid: i for i, sid in enumerate(order)}
        self.assertEqual(abs(idx[hp.id] - idx[nf.id]), 1,
                         "Belleville stops must be consecutive")
        self.assertLess(idx[hp.id], idx[mc.id],
                        "Belleville before Ottawa (no backtracking)")
        self.assertLess(idx[nf.id], idx[mc.id],
                        "Belleville before Ottawa (no backtracking)")
        # The rewrite is stable: rerunning yields the same order.
        rerun = DispatchOptimizationService(self.env).optimize_route(job.id)
        self.assertEqual(rerun["new_order"], order)

    def test_02_legacy_job_without_items_clusters_cities(self):
        """Legacy path (no item movements): nearest-neighbour with
        point-to-point distances still serves the two Belleville stops
        consecutively before Ottawa."""
        job, ud, hp, mc, nf = self._booking185_job(with_items=False)
        result = DispatchOptimizationService(self.env).optimize_route(job.id)
        self.assertNotEqual(result.get("basis"), "planner")
        order = result["new_order"]
        idx = {sid: i for i, sid in enumerate(order)}
        self.assertEqual(order[0], ud.id)
        self.assertEqual(abs(idx[hp.id] - idx[nf.id]), 1)
        self.assertLess(idx[hp.id], idx[mc.id])
        self.assertLess(idx[nf.id], idx[mc.id])

    def test_03_planner_clusters_same_city_deliveries(self):
        """Planner-level guarantee: city-tagged stops are clustered even
        when scored independently (travel_fn=None, built-in fallback)."""
        planner = ItineraryPlanner(self.env)
        stops = [
            {"stop_key": "pu", "stop_type": "pickup", "city": "Brampton",
             "latitude": self.BRAMPTON[0], "longitude": self.BRAMPTON[1],
             "timing_type": "flexible", "operating_hours_snapshot": self.open_24,
             "service_time_minutes": 15, "timezone": "America/Toronto"},
            {"stop_key": "hp", "stop_type": "delivery", "city": "Belleville",
             "latitude": self.BELLEVILLE[0], "longitude": self.BELLEVILLE[1],
             "timing_type": "flexible", "operating_hours_snapshot": self.open_24,
             "service_time_minutes": 15, "timezone": "America/Toronto"},
            {"stop_key": "nf", "stop_type": "delivery", "city": "Belleville",
             "latitude": self.NOFRILLS[0], "longitude": self.NOFRILLS[1],
             "timing_type": "flexible", "operating_hours_snapshot": self.open_24,
             "service_time_minutes": 15, "timezone": "America/Toronto"},
            {"stop_key": "mc", "stop_type": "delivery", "city": "Ottawa",
             "latitude": self.OTTAWA[0], "longitude": self.OTTAWA[1],
             "timing_type": "flexible", "operating_hours_snapshot": self.open_24,
             "service_time_minutes": 15, "timezone": "America/Toronto"},
        ]
        movements = [
            {"key": "p1", "pickup_stop_key": "pu", "delivery_stop_keys": ["hp"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "p2", "pickup_stop_key": "pu", "delivery_stop_keys": ["hp"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "p3", "pickup_stop_key": "pu", "delivery_stop_keys": ["mc"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "p4", "pickup_stop_key": "pu", "delivery_stop_keys": ["nf"],
             "shared": False, "weight_lbs": 500.0},
        ]
        result = planner.recommend_route(
            stops, movements, _dt(12, 0), vehicle_max=13)
        self.assertTrue(result["feasible"])
        route = result["recommended"]
        self.assertEqual(route[0], "pu")
        idx = {k: i for i, k in enumerate(route)}
        self.assertEqual(abs(idx["hp"] - idx["nf"]), 1)
        self.assertLess(idx["hp"], idx["mc"])
        self.assertLess(idx["nf"], idx["mc"])

    def test_04_adviser_surfaces_usa_dip_warning(self):
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
                        (43.80, -79.60), (42.80, -78.00), (45.42, -75.70)])},
                }],
            })

        self.patch(requests, "get", fake_get)
        job, ud, hp, mc, nf = self._booking185_job(with_items=True)
        report = RouteAdviserService(self.env).adviser_report(job)
        self.assertTrue(any("USA" in w for w in report["warnings"]),
                        "Warnings must surface the USA-detour flag")


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

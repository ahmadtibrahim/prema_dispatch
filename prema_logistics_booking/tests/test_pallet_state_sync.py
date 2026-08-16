"""Pallet state synchronization — submitted allocation records must always
match the submitted physical pallet count, and the pricing session stores
the SAME physical pallets / weight / allocations as the portal screen."""
import datetime
import json

from odoo import fields

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.controllers.booking_portal import (
    _allocate_transportation,
    _allocated_stop_weights,
    _build_stop_pricing,
    _reconcile_pallet_allocations,
)
from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestPalletStateSync(TransactionCase):

    def test_01_reconcile_pads_missing_pallets(self):
        allocs = [{"pallet": 1, "stops": [2], "shared": False}]
        result = _reconcile_pallet_allocations(4, allocs)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["stops"], [2])  # existing preserved
        for record in result[1:]:
            self.assertEqual(record["stops"], [])
            self.assertFalse(record["shared"])

    def test_02_reconcile_truncates_extra_pallets(self):
        allocs = [{"pallet": i, "stops": [], "shared": False} for i in range(1, 7)]
        result = _reconcile_pallet_allocations(5, allocs)
        self.assertEqual(len(result), 5)
        self.assertEqual([r["pallet"] for r in result], [1, 2, 3, 4, 5])

    def test_03_reconcile_noop_when_equal(self):
        allocs = [{"pallet": 1, "stops": [1], "shared": False},
                  {"pallet": 2, "stops": [1, 2], "shared": True}]
        result = _reconcile_pallet_allocations(2, allocs)
        self.assertEqual(result, allocs)

    def test_04_empty_allocations_built_for_all_pallets(self):
        result = _reconcile_pallet_allocations(4, [])
        self.assertEqual(len(result), 4)
        self.assertEqual([r["pallet"] for r in result], [1, 2, 3, 4])

    def test_05_session_stores_submitted_values_end_to_end(self):
        """Screen shows 4 pallets / 2,000 lb with 2 delivery stops — the
        pricing session must store 4 / 2,000 / 4 allocation records."""
        cls = type(self)
        env = self.env(context=dict(self.env.context, skip_departure_reconcile=True))
        country = env.ref("base.ca")
        country.logistics_network_enabled = True
        state = env["res.country.state"].search(
            [("country_id", "=", country.id), ("code", "=", "ON")], limit=1)
        state.logistics_network_enabled = True

        def _square(lng, lat):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - 0.05, lat - 0.04], [lng + 0.05, lat - 0.04],
                [lng + 0.05, lat + 0.04], [lng - 0.05, lat + 0.04],
                [lng - 0.05, lat - 0.04]]]})

        gta = env["logistics.region"].create({
            "code": "PSS-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": country.id,
            "state_id": state.id, "polygon_geojson": _square(-87.5, 49.0)})
        ott = env["logistics.region"].create({
            "code": "PSS-OTT", "name": "Ottawa", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": country.id,
            "state_id": state.id, "polygon_geojson": _square(-86.5, 49.8)})
        env["logistics.hub"].create({
            "name": "PSS Hub", "public_name": "PSS Hub", "code": "PSS-HUB",
            "canonical_region_id": gta.id, "is_default": True,
            "latitude": 49.0, "longitude": -87.5})
        corridor = env["logistics.corridor"].create({
            "name": "PSS-Eastbound", "direction": "eastbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
        })
        env["logistics.corridor.stop"].create([
            {"corridor_id": corridor.id, "sequence": 10, "region_id": gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": corridor.id, "sequence": 20, "region_id": ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 507.6},
        ])
        env["logistics.pallet.volume.tier"].create([
            {"corridor_id": corridor.id, "min_pallets": 4, "max_pallets": 6,
             "discount_pct": 10.0, "pricing_type": "ltl"},
        ])
        partner = env["res.partner"].create({"name": "PSS Customer"})
        wednesday = datetime.date.today()
        while wednesday.strftime("%A").lower() != "wednesday":
            wednesday += datetime.timedelta(days=1)

        svc = BookingOrchestrationService(env)
        norm = svc.normalize_request({
            "partner_id": partner.id,
            "pickup_stops": [{"latitude": 49.0, "longitude": -87.5, "postal_code": "X0Z"}],
            "delivery_stops": [
                {"city": "Belleville", "latitude": 49.6, "longitude": -87.0, "postal_code": "X1Y"},
                {"city": "Ottawa", "latitude": 49.8, "longitude": -86.5, "postal_code": "X2W"},
            ],
            "pallets": 4,
            "weight_lbs": 2000.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": wednesday,
            "pallet_allocations": _reconcile_pallet_allocations(4, [
                {"pallet": 1, "stops": [1], "shared": False},
            ]),
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1)
        self.assertEqual(session.physical_pallets, 4)
        self.assertAlmostEqual(session.weight_lbs, 2000.0, places=2)
        self.assertEqual(len(session._get_pallet_allocations()), 4)
        self.assertEqual(len(session.delivery_stop_ids), 2)


    def test_06_stop_pricing_reconciles_to_total(self):
        """The displayed breakdown components must sum to the session's
        calculated_price by construction."""
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.env["res.partner"].search([], limit=1).id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": 2000.0,
            "calculated_price": 647.19,
            "price_snapshot": [
                {"label": "Leg 1 — route", "amount": 761.40},
                {"label": "Volume discount (15%)", "amount": -114.21},
            ],
            "expires_at": fields.Datetime.now(),
        })
        for index in (1, 2):
            self.env["logistics.pricing.session.stop"].create({
                "session_id": session.id, "sequence": index * 10,
                "location_name": "Stop %d" % index, "city": "Test",
            })
        breakdown = _build_stop_pricing(session)
        total = breakdown["route_transportation"] \
            + sum(s["amount"] for s in breakdown["stops"]) \
            + sum(b["amount"] for b in breakdown["booking_level"])
        self.assertAlmostEqual(total, 647.19, places=2)
        self.assertAlmostEqual(breakdown["total"], 647.19, places=2)
        # One leg + two stops → honest route-level attribution, no fake
        # per-stop price.
        self.assertAlmostEqual(breakdown["route_transportation"], 761.40, places=2)
        self.assertTrue(all(s["amount"] == 0.0 for s in breakdown["stops"]))


    # ── Explanatory stop-cost allocation (display-only) ─────────────

    def test_07_allocation_sums_to_route_total(self):
        amounts = _allocate_transportation(761.40, [312.1, 507.6], [4, 1])
        self.assertAlmostEqual(sum(amounts), 761.40, places=2)
        self.assertTrue(all(a >= 0 for a in amounts))

    def test_08_three_stop_allocation_reconciles(self):
        amounts = _allocate_transportation(1000.00, [100.0, 300.0, 500.0],
                                           [6, 3, 1])
        self.assertAlmostEqual(sum(amounts), 1000.00, places=2)

    def test_09_allocation_reacts_to_pallet_assignment(self):
        # 4 onboard to Belleville vs 1 onboard: different shares.
        heavy_first = _allocate_transportation(761.40, [312.1, 507.6], [4, 1])
        equalish = _allocate_transportation(761.40, [312.1, 507.6], [2, 2])
        self.assertGreater(heavy_first[0], equalish[0])
        self.assertLess(heavy_first[1], equalish[1])

    def test_10_distance_fallback_without_onboard(self):
        amounts = _allocate_transportation(761.40, [312.1, 507.6], [0, 0])
        self.assertAlmostEqual(sum(amounts), 761.40, places=2)

    def test_11_residual_assigned_to_final_stop(self):
        amounts = _allocate_transportation(100.01, [100.0, 200.0], [1, 1])
        # Halves of 100.01 are 50.005 → rounding must land exactly on the
        # final stop and the total must reconcile.
        self.assertAlmostEqual(sum(amounts), 100.01, places=2)
        self.assertTrue(abs(amounts[0] - 50.0) <= 0.01)
        self.assertAlmostEqual(amounts[1], 100.01 - amounts[0], places=2)


    # ── Stop weight allocation display ──────────────────────────────

    def _session(self, weight, allocations):
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.env["res.partner"].search([], limit=1).id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": weight,
            "calculated_price": 100.0,
            "price_snapshot": [{"_pallet_allocs": allocations}],
            "expires_at": fields.Datetime.now(),
        })
        for index in (1, 2):
            self.env["logistics.pricing.session.stop"].create({
                "session_id": session.id, "sequence": index,
                "location_name": "Stop %d" % index, "city": "Test",
            })
        return session

    def test_12_two_two_split_1000_each(self):
        session = self._session(2000.0, [
            {"pallet": 1, "stops": [1], "shared": False},
            {"pallet": 2, "stops": [1], "shared": False},
            {"pallet": 3, "stops": [2], "shared": False},
            {"pallet": 4, "stops": [2], "shared": False},
        ])
        stops = _allocated_stop_weights(session, [
            {"name": "A", "weight_lbs": 500.0}, {"name": "B", "weight_lbs": 500.0}])
        self.assertEqual(stops[0]["weight_lbs"], 1000.0)
        self.assertEqual(stops[1]["weight_lbs"], 1000.0)

    def test_13_three_one_split_1500_500(self):
        session = self._session(2000.0, [
            {"pallet": 1, "stops": [1], "shared": False},
            {"pallet": 2, "stops": [1], "shared": False},
            {"pallet": 3, "stops": [1], "shared": False},
            {"pallet": 4, "stops": [2], "shared": False},
        ])
        stops = _allocated_stop_weights(session, [
            {"name": "A", "weight_lbs": 500.0}, {"name": "B", "weight_lbs": 500.0}])
        self.assertEqual(stops[0]["weight_lbs"], 1500.0)
        self.assertEqual(stops[1]["weight_lbs"], 500.0)

    def test_14_manual_override_2400(self):
        session = self._session(2400.0, [
            {"pallet": 1, "stops": [1], "shared": False},
            {"pallet": 2, "stops": [1], "shared": False},
            {"pallet": 3, "stops": [1], "shared": False},
            {"pallet": 4, "stops": [2], "shared": False},
        ])
        stops = _allocated_stop_weights(session, [
            {"name": "A", "weight_lbs": 500.0}, {"name": "B", "weight_lbs": 500.0}])
        self.assertEqual(stops[0]["weight_lbs"], 1800.0)
        self.assertEqual(stops[1]["weight_lbs"], 600.0)

    def test_15_shared_pallet_counted_once(self):
        session = self._session(2000.0, [
            {"pallet": 1, "stops": [1, 2], "shared": True},
            {"pallet": 2, "stops": [1], "shared": False},
            {"pallet": 3, "stops": [1], "shared": False},
            {"pallet": 4, "stops": [2], "shared": False},
        ])
        stops = _allocated_stop_weights(session, [
            {"name": "A", "weight_lbs": 500.0}, {"name": "B", "weight_lbs": 500.0}])
        self.assertEqual(stops[0]["weight_lbs"], 1500.0)
        self.assertEqual(stops[1]["weight_lbs"], 500.0)

    def test_16_single_stop_equals_total(self):
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.env["res.partner"].search([], limit=1).id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": 2000.0,
            "calculated_price": 100.0,
            "price_snapshot": [{"_pallet_allocs": [
                {"pallet": i, "stops": [1], "shared": False} for i in range(1, 5)]}],
            "expires_at": fields.Datetime.now(),
        })
        stops = _allocated_stop_weights(session, [{"name": "A", "weight_lbs": 500.0}])
        self.assertEqual(stops[0]["weight_lbs"], 2000.0)

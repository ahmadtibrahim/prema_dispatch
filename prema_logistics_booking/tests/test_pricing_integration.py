"""Pricing integration — canonical selections, temperature persistence, route snapshot.

Corridor-era fixture (same clean numbers as test_pricing.py):
100 km × 16 $/km ÷ 8 planned pallets = $2/pallet-km → $200/pallet.
"""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class _CorridorFixtureMixin:
    """Shared corridor fixture: 100 km direct segment at 16 $/km."""

    @classmethod
    def _build_fixture(cls, codes=("X1", "X2"), fsa_codes=("X1A", "X2B"),
                       corridor_name="TEST-PI Corridor", **corridor_extra):
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Corridor = cls.env["logistics.corridor"]
        cls.CStop = cls.env["logistics.corridor.stop"]
        cls.r1 = cls.Region.create({"code": codes[0], "name": f"Test {codes[0]}"})
        cls.r2 = cls.Region.create({"code": codes[1], "name": f"Test {codes[1]}"})
        cls.fsa1 = cls.Fsa.create({
            "fsa": fsa_codes[0], "region_id": cls.r1.id, "display_city": "City A",
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.fsa2 = cls.Fsa.create({
            "fsa": fsa_codes[1], "region_id": cls.r2.id, "display_city": "City B",
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.corridor = cls.Corridor.with_context(skip_departure_reconcile=True).create(dict({
            "name": corridor_name,
            "direction": "eastbound",
            "rate_per_km": 16.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "departure_horizon_weeks": 8,
        }, **corridor_extra))
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 10,
            "region_id": cls.r1.id, "distance_from_origin_km": 0.0, "day_offset": 0,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 20,
            "region_id": cls.r2.id, "distance_from_origin_km": 100.0, "day_offset": 0,
        })


class TestCanonicalSelections(_CorridorFixtureMixin, TransactionCase):
    """LTL pricing, FTL fallback contract, Dry/Reefer persistence, Chilled/Frozen compat."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._build_fixture()

    def test_01_ltl_offering_selected(self):
        svc = PricingService(self.env)
        r = svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, r.reason or "not available")
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    def test_02_ftl_request_on_ltl_only_corridor_prices_as_ltl(self):
        """Corridor contract: when Full Truckload is disabled, an FTL
        request is never rejected — it continues through normal LTL
        pricing untouched (the old 'reject FTL when no FTL offering'
        behavior is gone)."""
        corridor2 = self.env["logistics.corridor"].with_context(
            skip_departure_reconcile=True).create({
                "name": "TEST-PI Corridor LTL-only",
                "direction": "eastbound",
                "rate_per_km": 16.0,
                "planned_pallets": 8,
                "included_weight_per_pallet": 500.0,
                "minimum_booking_charge": 0.0,
                "departure_horizon_weeks": 8,
                "enable_ftl": False,
            })
        self.env["logistics.corridor.stop"].create({
            "corridor_id": corridor2.id, "sequence": 10,
            "region_id": self.r1.id, "distance_from_origin_km": 0.0, "day_offset": 0,
        })
        self.env["logistics.corridor.stop"].create({
            "corridor_id": corridor2.id, "sequence": 20,
            "region_id": self.r2.id, "distance_from_origin_km": 100.0, "day_offset": 0,
        })
        r = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ftl", "dry", 12, 9600)
        self.assertTrue(r.available, r.reason or "not available")
        self.assertFalse(r.route_snapshot["ftl_priced"],
                         "FTL request on an LTL-only corridor must not price as FTL")

    def test_03_invalid_shipment_type(self):
        r = PricingService(self.env).calculate(self.fsa1, self.fsa2, "invalid", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertIn("invalid", r.reason)

    def test_04_reefer_persistence(self):
        """Reefer needs an explicit temperature; the canonical mode persists
        in the route snapshot."""
        r = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "reefer", 1, 500, required_temperature_c=2.0)
        self.assertTrue(r.available, r.reason or "not available")
        self.assertEqual(r.route_snapshot["temperature_mode"], "reefer")

    def test_05_chilled_maps_to_reefer(self):
        r = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "chilled", 1, 500, required_temperature_c=2.0)
        self.assertTrue(r.available, r.reason or "not available")
        self.assertEqual(r.route_snapshot["temperature_mode"], "reefer")


class TestSnapshotPersistence(_CorridorFixtureMixin, TransactionCase):
    """Route snapshot stored in session, copied to booking, never recalculated."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._build_fixture(codes=("S1", "S2"), fsa_codes=("S1A", "S2B"),
                           corridor_name="TEST-PI Snapshot Corridor")

    def test_01_session_stores_snapshot(self):
        result = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "dry", 2, 1000)
        self.assertTrue(result.available, result.reason or "not available")
        snap = result.route_snapshot
        self.assertTrue(snap, "route_snapshot must not be empty")
        self.assertEqual(snap["leg_count"], 1)
        self.assertEqual(snap["calculated_price"], result.calculated_price)
        self.assertEqual(snap["pallets"], 2)
        self.assertEqual(snap["weight_lbs"], 1000)
        self.assertEqual(snap["pricing_authority"], "corridor_per_km")
        leg = snap["legs"][0]
        self.assertEqual(leg["corridor_id"], self.corridor.id)
        self.assertEqual(leg["origin_region"], "S1")
        self.assertEqual(leg["dest_region"], "S2")
        self.assertEqual(leg["distance_km"], 100.0)
        # pickup/delivery dates are departure-derived: they are only
        # populated with resolve_departures=True (see test_booking_invoice).

    # test_02_capacity_gate_13_pallets REMOVED — dead concept. Corridor
    # pricing never gates at 13 pallets: capacity is enforced at
    # dispatch/load-plan level (Task #10 shared-capacity + FTL threshold
    # architecture); test_pricing.py::test_05_large_ltl_load_still_prices
    # asserts the live behavior.

    # test_03_nearest_5_rounding REMOVED — dead concept. The old
    # _compute_v4_formula nearest-$5 rounding was deleted in the corridor
    # consolidation; corridor $/km pricing is the sole authority
    # (test_pricing.py asserts exact formula totals).

    def test_04_no_per_km_leakage(self):
        """Accessorials must never add charges to corridor pricing."""
        r = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "dry", 1, 500,
            liftgate_pickup=True, liftgate_delivery=True, residential=True)
        self.assertTrue(r.available)
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2,
            msg="Accessorials must not add charges")

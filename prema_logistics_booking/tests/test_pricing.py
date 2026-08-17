"""Pricing engine tests — corridor $/km formula: distance × pallets × ($/km ÷ planned pallets).

Corridor: 100 km × 16 $/km ÷ 8 planned pallets = $2/pallet-km → $200/pallet
(the same clean numbers the legacy rate-plan fixture produced, so the
per-pallet assertions read identically to the original suite).
"""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestPricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Corridor = cls.env["logistics.corridor"]
        cls.CStop = cls.env["logistics.corridor.stop"]

        # Regions
        cls.r1 = cls.Region.create({"code": "T1", "name": "Test Region 1"})
        cls.r2 = cls.Region.create({"code": "T2", "name": "Test Region 2"})

        # FSAs
        cls.fsa1 = cls.Fsa.create({
            "fsa": "T1A", "region_id": cls.r1.id, "display_city": "Test City 1",
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.fsa2 = cls.Fsa.create({
            "fsa": "T2B", "region_id": cls.r2.id, "display_city": "Test City 2",
            "pickup_supported": True, "delivery_supported": True,
        })

        # Corridor: 100 km × 16 $/km ÷ 8 planned pallets = 2 $/pallet-km.
        # included_weight_per_pallet = 500 lb (corridor default).
        # LTL-only on purpose: with enable_ftl the FTL threshold (default
        # 10 pallets, auto_price) would convert every ≥10-pallet load to a
        # flat FTL price — the LTL math below must stay pure. FTL behavior
        # is exercised on dedicated corridors in test_14/test_14b/test_16.
        cls.corridor = cls.Corridor.with_context(skip_departure_reconcile=True).create({
            "name": "TEST-PRC Corridor",
            "direction": "eastbound",
            "rate_per_km": 16.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "departure_horizon_weeks": 8,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 10,
            "region_id": cls.r1.id, "distance_from_origin_km": 0.0,
            "day_offset": 0,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 20,
            "region_id": cls.r2.id, "distance_from_origin_km": 100.0,
            "day_offset": 0,
        })

    # ── Corridor formula tests ($2/pallet-km × 100 km = $200/pallet) ──

    def test_01_one_pallet_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_02_two_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 2, 1000)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 400.00, places=2)

    def test_03_five_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 5, 2500)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1000.00, places=2)

    def test_04_eight_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 8, 4000)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    def test_05_large_ltl_load_still_prices(self):
        """13+ pallets: corridor pricing is not capped — capacity is
        enforced at dispatch/load-plan level (shared-capacity tests,
        Task #10), never silently at the quote."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 13, 6500)
        self.assertTrue(result.available)
        # 100 km × 13 × $2/pallet-km = 2600; 6500 lb > 13×500 included → excess
        expected = PricingService.calculate_leg_per_km(
            100.0, 16.0, 8, 13, 500.0, 6500.0)["subtotal"]
        self.assertAlmostEqual(result.calculated_price, expected, places=2)

    # ── Reefer tests ──

    def test_06_one_pallet_reefer(self):
        """Reefer books at the same corridor rate (no surcharge in
        corridor pricing), but requires an explicit temperature."""
        result = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "reefer", 1, 500, required_temperature_c=2.0)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_07_eight_pallets_reefer(self):
        result = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "reefer", 8, 4000, required_temperature_c=2.0)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    def test_06b_reefer_without_temperature_rejected(self):
        """Reefer without required_temperature_c must never auto-price."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "reefer", 1, 500)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "required_temperature_c_missing")

    # ── Chilled / Frozen canonicalize to Reefer ──

    def test_08_chilled_prices_as_reefer(self):
        """chilled → reefer via temperature_compat; temperature required."""
        result = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "chilled", 1, 500, required_temperature_c=2.0)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_09_frozen_prices_as_reefer(self):
        result = PricingService(self.env).calculate(
            self.fsa1, self.fsa2, "ltl", "frozen", 1, 500, required_temperature_c=2.0)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    # ── No legacy additions ──

    def test_10_no_additional_charges(self):
        """Price must be exactly formula-based, no extra amounts."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_11_same_price_different_pickup_fsa_same_region(self):
        """Two FSAs in same region must give same price (region-level pricing)."""
        fsa1b = self.Fsa.create({
            "fsa": "T1B", "region_id": self.r1.id, "display_city": "Test City 1B",
            "pickup_supported": True, "delivery_supported": True,
        })
        r1 = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        r2 = PricingService(self.env).calculate(fsa1b, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r1.available and r2.available)
        self.assertAlmostEqual(r1.calculated_price, r2.calculated_price, places=2)

    # ── Edge cases ──

    def test_12_fsa_not_supported(self):
        unsupported = self.Fsa.create({
            "fsa": "Z9Z", "pickup_supported": False, "delivery_supported": False,
        })
        result = PricingService(self.env).calculate(unsupported, self.fsa2, "ltl", "dry", 1, 500)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "pickup_fsa_not_supported")

    def test_13_heavy_load_excess_weight_charge(self):
        """Weight above pallets × included_weight_per_pallet is charged."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 15, 12000)
        self.assertTrue(result.available)
        # 100 km × 15 × $2 = 3000 base; 12000 − 15×500 = 4500 excess lb ×
        # 100 km × (16 ÷ (8×500)) $/lb-km = 1800 → 4800 total.
        expected = PricingService.calculate_leg_per_km(
            100.0, 16.0, 8, 15, 500.0, 12000.0)["subtotal"]
        self.assertAlmostEqual(result.calculated_price, expected, places=2)

    def test_14_ftl_available_when_corridor_ftl_configured(self):
        """FTL prices on a corridor with FTL $/km configured (corridor
        default pricing: distance × ftl_rate_per_km). Dedicated region pair
        so this corridor is the sole pricing anchor for the request."""
        r3, r4, fsa3, fsa4, corridor = self._dedicated_pair(
            "T3", "T4", "T3A", "T4B", "TEST-PRC FTL Corridor",
            enable_ftl=True, ftl_rate_per_km=16.0,
        )
        result = PricingService(self.env).calculate(fsa3, fsa4, "ftl", "dry", 12, 9600)
        self.assertTrue(result.available, result.reason or "not available")
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    def test_14b_ftl_on_corridor_without_ftl_rate_rejected(self):
        r5, r6, fsa5, fsa6, corridor = self._dedicated_pair(
            "T5", "T6", "T5A", "T6B", "TEST-PRC No-FTL-Rate Corridor",
            enable_ftl=True, ftl_rate_per_km=0.0,
        )
        result = PricingService(self.env).calculate(fsa5, fsa6, "ftl", "dry", 12, 9600)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "ftl_rate_not_configured")

    def test_16_ftl_threshold_auto_prices_large_load(self):
        """Corridor contract (Task #10): at/above the FTL threshold with
        auto_price behavior, an LTL request prices as FTL — never as the
        larger LTL total."""
        r7, r8, fsa7, fsa8, corridor = self._dedicated_pair(
            "T7", "T8", "T7A", "T8B", "TEST-PRC Threshold Corridor",
            enable_ftl=True, ftl_rate_per_km=16.0,
            ftl_threshold_pallets=10, ftl_behavior="auto_price",
        )
        result = PricingService(self.env).calculate(fsa7, fsa8, "ltl", "dry", 12, 9600)
        self.assertTrue(result.available, result.reason or "not available")
        self.assertTrue(result.route_snapshot["ftl_priced"])
        # FTL corridor default: distance × ftl_rate_per_km = 1600.
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    def _dedicated_pair(self, rcode1, rcode2, fcode1, fcode2, name, **corridor_extra):
        """Build a corridor on its own region pair (sole pricing anchor)."""
        r1 = self.Region.create({"code": rcode1, "name": f"Test {rcode1}"})
        r2 = self.Region.create({"code": rcode2, "name": f"Test {rcode2}"})
        fsa1 = self.Fsa.create({
            "fsa": fcode1, "region_id": r1.id, "display_city": f"City {fcode1}",
            "pickup_supported": True, "delivery_supported": True,
        })
        fsa2 = self.Fsa.create({
            "fsa": fcode2, "region_id": r2.id, "display_city": f"City {fcode2}",
            "pickup_supported": True, "delivery_supported": True,
        })
        corridor = self.Corridor.with_context(skip_departure_reconcile=True).create(dict({
            "name": name,
            "direction": "eastbound",
            "rate_per_km": 16.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "departure_horizon_weeks": 8,
        }, **corridor_extra))
        self.CStop.create({
            "corridor_id": corridor.id, "sequence": 10,
            "region_id": r1.id, "distance_from_origin_km": 0.0, "day_offset": 0,
        })
        self.CStop.create({
            "corridor_id": corridor.id, "sequence": 20,
            "region_id": r2.id, "distance_from_origin_km": 100.0, "day_offset": 0,
        })
        return r1, r2, fsa1, fsa2, corridor

    # ── Immutable quote: final line amount matches calculated price ──

    def test_15_price_lines_match_total(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 3, 1500)
        self.assertTrue(result.available)
        final_line = result.price_lines[-1]["amount"]
        self.assertAlmostEqual(final_line, result.calculated_price, places=2)
        self.assertAlmostEqual(result.calculated_price, 600.00, places=2)

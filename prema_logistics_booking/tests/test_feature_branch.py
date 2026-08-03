"""Feature branch tests — codex/dispatch-unification validation."""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestPerKmPricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.svc = PricingService(cls.env)
        cls.currency = cls.env.company.currency_id

    def test_01_basic_one_pallet(self):
        """R=$3/km, T=8, P=1, I=500lb, W=500lb, D=100km → $37.50"""
        r = self.svc.calculate_leg_per_km(
            distance_km=100, rate_per_km=3.0, target_pallets=8,
            booked_pallets=1, included_weight_per_pallet=500,
            actual_weight_lbs=500, currency=self.currency,
        )
        self.assertAlmostEqual(r["subtotal"], 37.50, places=2)

    def test_02_excess_weight(self):
        """Same but W=1000lb → $75.00"""
        r = self.svc.calculate_leg_per_km(
            distance_km=100, rate_per_km=3.0, target_pallets=8,
            booked_pallets=1, included_weight_per_pallet=500,
            actual_weight_lbs=1000, currency=self.currency,
        )
        self.assertAlmostEqual(r["subtotal"], 75.00, places=2)

    def test_03_full_truck(self):
        """P=8, W=4000lb → $300.00"""
        r = self.svc.calculate_leg_per_km(
            distance_km=100, rate_per_km=3.0, target_pallets=8,
            booked_pallets=8, included_weight_per_pallet=500,
            actual_weight_lbs=4000, currency=self.currency,
        )
        self.assertAlmostEqual(r["subtotal"], 300.00, places=2)

    def test_04_hub_transfer_two_legs(self):
        """Two legs: 120km@$3 + 540km@$2.85"""
        itinerary = self.svc.calculate_itinerary_price([
            {"distance_km": 120, "rate_per_km": 3.0, "target_pallets": 8,
             "booked_pallets": 1, "included_weight_per_pallet": 500, "actual_weight_lbs": 500},
            {"distance_km": 540, "rate_per_km": 2.85, "target_pallets": 8,
             "booked_pallets": 1, "included_weight_per_pallet": 500, "actual_weight_lbs": 500},
        ])
        expected = 45.00 + 192.375
        self.assertAlmostEqual(itinerary["total_subtotal"], round(expected, 2), places=2)
        self.assertEqual(itinerary["leg_count"], 2)

    def test_05_currency_rounding_used(self):
        """Odoo currency rounding produces CAD-standard 2-decimal results."""
        r = self.svc.calculate_leg_per_km(
            distance_km=100, rate_per_km=3.0, target_pallets=8,
            booked_pallets=1, included_weight_per_pallet=500,
            actual_weight_lbs=1000, currency=self.currency,
        )
        # $75.00 should be exactly 75.00 with CAD rounding
        self.assertEqual(r["subtotal"], 75.00)

    def test_06_rejects_zero_target_pallets(self):
        """Zero target pallets must raise ValueError, not silently default."""
        with self.assertRaises(ValueError):
            self.svc.calculate_leg_per_km(
                distance_km=100, rate_per_km=3.0, target_pallets=0,
                booked_pallets=1, included_weight_per_pallet=500,
                actual_weight_lbs=500,
            )

    def test_07_rejects_negative_target_pallets(self):
        with self.assertRaises(ValueError):
            self.svc.calculate_leg_per_km(
                distance_km=100, rate_per_km=3.0, target_pallets=-1,
                booked_pallets=1, included_weight_per_pallet=500,
                actual_weight_lbs=500,
            )

    def test_08_rejects_zero_included_weight(self):
        with self.assertRaises(ValueError):
            self.svc.calculate_leg_per_km(
                distance_km=100, rate_per_km=3.0, target_pallets=8,
                booked_pallets=1, included_weight_per_pallet=0,
                actual_weight_lbs=500,
            )


class TestDirectionPreservation(TransactionCase):
    def test_01_local_value_preserved(self):
        """Corridor 3 has direction='local' — must survive feature branch."""
        cor = self.env['logistics.corridor'].browse(3)
        self.assertIn(cor.direction, ['local', 'local_loop'],
                      f"Corridor 3 direction={cor.direction} must be valid")


class TestSharedConstants(TransactionCase):
    def test_01_constants_importable(self):
        """Constants module is importable and has expected values."""
        from odoo.addons.prema_logistics_booking.constants import (
            SERVICE_MODE, LOAD_TYPE, EQUIPMENT_REQUIREMENT,
        )
        self.assertEqual(SERVICE_MODE[0][0], "dedicated")
        self.assertEqual(LOAD_TYPE[0][0], "ltl")
        self.assertEqual(EQUIPMENT_REQUIREMENT[0][0], "dry")

    def test_02_booking_uses_constants(self):
        """Booking model fields import from shared constants."""
        field = self.env['logistics.booking']._fields['service_mode']
        self.assertEqual(field.selection[0][0], "dedicated")
        self.assertEqual(field.selection[1][0], "expedited")

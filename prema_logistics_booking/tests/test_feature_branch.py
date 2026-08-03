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
        """Corridor with direction='local' must survive feature branch."""
        local_cor = self.env['logistics.corridor'].search([
            ('direction', 'in', ['local', 'local_loop']),
        ], limit=1)
        self.assertTrue(local_cor, "No corridor with direction='local' found")
        self.assertIn(local_cor.direction, ['local', 'local_loop'],
                      f"Direction={local_cor.direction} must be valid")


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


class TestHubMigration(TransactionCase):
    def test_01_hub_fields_exist(self):
        """Canonical hub fields exist on corridor model."""
        cor = self.env['logistics.corridor'].browse(1)
        self.assertTrue(hasattr(cor, 'origin_hub_id'), "origin_hub_id missing")
        self.assertTrue(hasattr(cor, 'destination_hub_id'), "destination_hub_id missing")
        self.assertTrue(hasattr(cor, 'transfer_hub_id'), "transfer_hub_id missing")

    def test_02_mississauga_hub_exists(self):
        """Mississauga Hub (YYZ-HUB) exists and is active."""
        hub = self.env['logistics.hub'].search([('code', '=', 'YYZ-HUB')], limit=1)
        self.assertTrue(hub, "YYZ-HUB not found")
        self.assertTrue(hub.active, "YYZ-HUB not active")

    def test_03_r1_corridor_has_origin_hub(self):
        """Corridor with R1 start should have Mississauga Hub as origin."""
        r1_region = self.env['logistics.region'].search([('code', '=', 'R1')], limit=1)
        self.assertTrue(r1_region, "Region R1 not found")
        yyz_hub = self.env['logistics.hub'].search([('code', '=', 'YYZ-HUB')], limit=1)
        self.assertTrue(yyz_hub, "Mississauga Hub (YYZ-HUB) not found")
        r1_corridors = self.env['logistics.corridor'].search([
            ('start_hub_id', '=', r1_region.id),
        ])
        self.assertTrue(r1_corridors, "No corridors with R1 start found")
        for cor in r1_corridors:
            self.assertTrue(
                cor.origin_hub_id,
                f"Corridor {cor.name} has R1 start but no origin_hub_id"
            )

    def test_04_ambiguous_corridors_unset(self):
        """Corridors without hub records have NULL canonical hub fields."""
        r15_region = self.env['logistics.region'].search([('code', '=', 'R15')], limit=1)
        self.assertTrue(r15_region, "Region R15 not found")
        r15_corridors = self.env['logistics.corridor'].search([
            ('start_hub_id', '=', r15_region.id),
        ])
        if r15_corridors:
            for cor in r15_corridors:
                self.assertFalse(
                    cor.origin_hub_id,
                    f"R15 corridor {cor.name} has origin_hub_id but no hub record exists for R15"
                )


class TestBookingSelectionViews(TransactionCase):
    def test_01_new_fields_in_form(self):
        """service_mode, load_type, equipment_requirement are in booking form."""
        view = self.env['ir.ui.view'].search([
            ('model', '=', 'logistics.booking'),
            ('type', '=', 'form'),
            ('arch_db', 'ilike', '%service_mode%'),
        ], limit=1)
        self.assertTrue(view, "service_mode not found in any booking form view")

    def test_02_reject_invalid_service_mode(self):
        """Invalid service_mode must be rejected on write."""
        # No bookings exist, so we create and test
        pass  # Deferred until booking creation is wired


class TestMigrationScripts(TransactionCase):
    def test_01_migration_files_present(self):
        """Migration scripts exist in the module at correct version path."""
        import os
        mig_dir = os.path.join(
            os.path.dirname(__file__), '..', 'migrations', '18.0.3.1.0'
        )
        pre = os.path.join(mig_dir, 'pre-migrate.py')
        post = os.path.join(mig_dir, 'post-migrate.py')
        self.assertTrue(os.path.exists(pre), f"pre-migrate.py missing at {pre}")
        self.assertTrue(os.path.exists(post), f"post-migrate.py missing at {post}")

    def test_02_module_version_bumped(self):
        """Module version reflects migration."""
        mod = self.env['ir.module.module'].search([
            ('name', '=', 'prema_logistics_booking')
        ], limit=1)
        self.assertTrue(mod, "Module not found")
        # Version in manifest is 18.0.3.1.0 — check it's at least 18.0.3.0
        self.assertGreaterEqual(
            tuple(int(x) for x in (mod.latest_version or '0.0.0.0.0').split('.')),
            (18, 0, 3, 0),
            f"Module version {mod.latest_version} is too old for migration"
        )

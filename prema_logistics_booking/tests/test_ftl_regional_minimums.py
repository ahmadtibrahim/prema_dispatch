"""FTL Regional Pricing — corridor-level unit tests for pricing types.

Covers flat-rate / per-km / corridor-default rules, validation, uniqueness,
legacy-field isolation and the 18.0.8.0.0 migration backfill.
"""
import importlib.util
import os

from psycopg2 import errors as pg_errors

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase


def _load_backfill_helper():
    path = os.path.join(
        os.path.dirname(__file__), "..", "migrations", "18.0.8.0.0", "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("post_migrate_180800", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.backfill_legacy_pricing_types


class TestFtlRegionalMinimums(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        cls.origin = Region.create({
            "code": "FTR-GTA", "name": "GTA", "is_official_ltl_region": True,
        })
        cls.dest_northumberland = Region.create({
            "code": "FTR-NOR", "name": "Northumberland", "is_official_ltl_region": True,
        })
        cls.dest_ottawa = Region.create({
            "code": "FTR-OTT", "name": "Ottawa", "is_official_ltl_region": True,
        })
        cls.dest_monteregie = Region.create({
            "code": "FTR-MON", "name": "Montérégie", "is_official_ltl_region": True,
        })
        cls.dest_quebec = Region.create({
            "code": "FTR-QUE", "name": "Québec City", "is_official_ltl_region": True,
        })

        # Corridor with corridor-level FTL rate $3.00/km and a legacy
        # corridor-wide minimum of $750 that must NEVER floor new pricing.
        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "FTR-Corridor",
            "direction": "eastbound",
            "enable_ftl": True,
            "ftl_threshold_pallets": 10,
            "ftl_rate_per_km": 3.0,
            "ftl_minimum_charge": 750.0,
            "ftl_reserve_entire_truck": True,
            "ftl_behavior": "auto_price",
        })
        cls.rule_flat = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.origin.id,
            "destination_region_id": cls.dest_northumberland.id,
            "pricing_type": "flat_rate",
            "flat_rate": 550.0,
        })
        cls.rule_per_km = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.dest_ottawa.id,
            "destination_region_id": cls.dest_monteregie.id,
            "pricing_type": "per_km",
            "ftl_rate_per_km_override": 3.25,
        })
        cls.rule_default = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.origin.id,
            "destination_region_id": cls.dest_ottawa.id,
            "pricing_type": "corridor_default",
            "ftl_rate_per_km_override": 9.99,  # must be ignored
        })

    # ── Pricing types ─────────────────────────────────────────────────

    def test_01_flat_rate_returns_exact_flat_rate(self):
        result = self.corridor.compute_ftl_price(
            self.origin, self.dest_northumberland, 130.0,
        )
        self.assertEqual(result["pricing_type"], "flat_rate")
        self.assertEqual(result["regional_rule"], self.rule_flat)
        self.assertAlmostEqual(result["price"], 550.00, places=2)

    def test_02_flat_rate_does_not_change_with_distance(self):
        for distance in (33.5, 130.0, 450.0):
            result = self.corridor.compute_ftl_price(
                self.origin, self.dest_northumberland, distance,
            )
            self.assertAlmostEqual(result["price"], 550.00, places=2)

    def test_03_per_km_rule_uses_regional_rate(self):
        result = self.corridor.compute_ftl_price(
            self.dest_ottawa, self.dest_monteregie, 300.0,
        )
        self.assertEqual(result["pricing_type"], "per_km")
        self.assertAlmostEqual(result["rate_per_km"], 3.25, places=6)
        self.assertAlmostEqual(result["distance_price"], 975.00, places=2)
        self.assertAlmostEqual(result["price"], 975.00, places=2)

    def test_04_corridor_default_rule_uses_corridor_rate(self):
        result = self.corridor.compute_ftl_price(
            self.origin, self.dest_ottawa, 450.0,
        )
        self.assertEqual(result["pricing_type"], "corridor_default")
        # The rule's 9.99 override is ignored for corridor_default.
        self.assertAlmostEqual(result["rate_per_km"], 3.00, places=6)
        self.assertAlmostEqual(result["price"], 1350.00, places=2)

    def test_05_no_rule_uses_corridor_rate_and_ignores_legacy_minimum(self):
        result = self.corridor.compute_ftl_price(
            self.origin, self.dest_quebec, 200.0,
        )
        self.assertFalse(result["regional_rule"])
        self.assertEqual(result["pricing_type"], "corridor_default")
        self.assertAlmostEqual(result["price"], 600.00, places=2)
        # $600 — the legacy $750 corridor-wide minimum must NOT floor this.

    def test_06_inactive_rule_ignored(self):
        self.rule_flat.active = False
        result = self.corridor.compute_ftl_price(
            self.origin, self.dest_northumberland, 130.0,
        )
        self.assertFalse(result["regional_rule"])
        self.assertAlmostEqual(result["price"], 390.00, places=2)

    def test_07_duplicate_active_origin_destination_rule_blocked(self):
        """No duplicate ACTIVE rule for the same corridor + pair — enforced
        by the ORM and by the partial unique index at the SQL level,
        regardless of pricing type."""
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.origin.id,
                "destination_region_id": self.dest_northumberland.id,
                "pricing_type": "per_km",
                "ftl_rate_per_km_override": 2.0,
            })

        with self.assertRaises(pg_errors.UniqueViolation):
            with self.env.cr.savepoint():
                self.env.cr.execute("""
                    INSERT INTO logistics_ftl_regional_minimum
                        (corridor_id, origin_region_id, destination_region_id,
                         pricing_type, flat_rate, ftl_rate_per_km_override,
                         currency_id, active, create_uid, write_uid,
                         create_date, write_date)
                    VALUES (%s, %s, %s, 'per_km', 0.0, 2.0, %s, TRUE, %s, %s,
                            now(), now())
                """, (
                    self.corridor.id, self.origin.id,
                    self.dest_northumberland.id,
                    self.env.company.currency_id.id, self.env.uid, self.env.uid,
                ))

        # An INACTIVE duplicate of the same pair remains legal — and editing
        # it (while it stays inactive) must not trip the duplicate guard.
        inactive = self.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": self.corridor.id,
            "origin_region_id": self.origin.id,
            "destination_region_id": self.dest_northumberland.id,
            "pricing_type": "per_km",
            "ftl_rate_per_km_override": 2.0,
            "active": False,
        })
        inactive.ftl_rate_per_km_override = 2.5
        self.assertFalse(inactive.active)

        with self.assertRaises(ValidationError):
            inactive.active = True

    def test_08_flat_rate_zero_or_negative_rejected(self):
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.origin.id,
                "destination_region_id": self.dest_quebec.id,
                "pricing_type": "flat_rate",
                "flat_rate": 0.0,
            })
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.origin.id,
                "destination_region_id": self.dest_quebec.id,
                "pricing_type": "flat_rate",
                "flat_rate": -100.0,
            })

    def test_09_per_km_zero_or_negative_rejected(self):
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.origin.id,
                "destination_region_id": self.dest_quebec.id,
                "pricing_type": "per_km",
                "ftl_rate_per_km_override": 0.0,
            })
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.origin.id,
                "destination_region_id": self.dest_quebec.id,
                "pricing_type": "per_km",
                "ftl_rate_per_km_override": -1.0,
            })

    def test_10_ftl_threshold_and_reserve_behavior_unchanged(self):
        """Threshold / Reserve Entire Truck / threshold behavior stay
        exactly as configured and never influence regional FTL pricing."""
        self.assertEqual(self.corridor.ftl_threshold_pallets, 10)
        self.assertTrue(self.corridor.ftl_reserve_entire_truck)
        self.assertEqual(self.corridor.ftl_behavior, "auto_price")

        baseline = self.corridor.compute_ftl_price(
            self.dest_ottawa, self.dest_monteregie, 300.0,
        )
        self.corridor.write({
            "ftl_threshold_pallets": 999,
            "ftl_reserve_entire_truck": False,
            "ftl_behavior": "recommend",
        })
        after = self.corridor.compute_ftl_price(
            self.dest_ottawa, self.dest_monteregie, 300.0,
        )
        self.assertAlmostEqual(after["price"], baseline["price"], places=2)
        self.assertAlmostEqual(after["price"], 975.00, places=2)

    def test_11_legacy_rule_minimum_charge_not_read(self):
        """The retired regional minimum_ftl_charge is never read, even when
        set to a large value on a flat-rate rule."""
        self.rule_flat.minimum_ftl_charge = 9999.0
        result = self.corridor.compute_ftl_price(
            self.origin, self.dest_northumberland, 130.0,
        )
        self.assertAlmostEqual(result["price"], 550.00, places=2)

    def test_12_exact_origin_destination_pairing_respected(self):
        north = self.corridor.get_ftl_regional_rule(
            self.origin, self.dest_northumberland,
        )
        ottawa = self.corridor.get_ftl_regional_rule(
            self.origin, self.dest_ottawa,
        )
        self.assertEqual(north, self.rule_flat)
        self.assertEqual(ottawa, self.rule_default)
        self.assertNotEqual(north.id, ottawa.id)

    # ── 18.0.8.0.0 migration backfill ─────────────────────────────────

    def _insert_legacy_row(self, pair, minimum, override):
        self.env.cr.execute("""
            INSERT INTO logistics_ftl_regional_minimum
                (corridor_id, origin_region_id, destination_region_id,
                 pricing_type, flat_rate, ftl_rate_per_km_override,
                 minimum_ftl_charge, currency_id, active,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, 'corridor_default', 0.0, %s, %s, %s, TRUE,
                    %s, %s, now(), now())
            RETURNING id
        """, (
            self.corridor.id, pair[0].id, pair[1].id,
            override, minimum, self.env.company.currency_id.id,
            self.env.uid, self.env.uid,
        ))
        return self.env.cr.fetchone()[0]

    def test_13_migration_converts_minimum_only_rule_to_flat_rate(self):
        row_id = self._insert_legacy_row(
            (self.dest_ottawa, self.dest_quebec), minimum=800.0, override=0.0,
        )
        _load_backfill_helper()(self.env.cr)
        self.env.cr.execute(
            "SELECT pricing_type, flat_rate, ftl_rate_per_km_override "
            "FROM logistics_ftl_regional_minimum WHERE id = %s", [row_id],
        )
        pricing_type, flat_rate, override = self.env.cr.fetchone()
        self.assertEqual(pricing_type, "flat_rate")
        self.assertAlmostEqual(flat_rate, 800.0, places=2)
        self.assertAlmostEqual(override, 0.0, places=2)

    def test_14_migration_converts_override_rule_to_per_km(self):
        row_id = self._insert_legacy_row(
            (self.dest_ottawa, self.dest_quebec), minimum=0.0, override=2.75,
        )
        _load_backfill_helper()(self.env.cr)
        self.env.cr.execute(
            "SELECT pricing_type, flat_rate, ftl_rate_per_km_override "
            "FROM logistics_ftl_regional_minimum WHERE id = %s", [row_id],
        )
        pricing_type, flat_rate, override = self.env.cr.fetchone()
        self.assertEqual(pricing_type, "per_km")
        self.assertAlmostEqual(flat_rate, 0.0, places=2)
        self.assertAlmostEqual(override, 2.75, places=2)

    def test_15_migration_leaves_empty_rule_as_corridor_default(self):
        row_id = self._insert_legacy_row(
            (self.dest_ottawa, self.dest_quebec), minimum=0.0, override=0.0,
        )
        _load_backfill_helper()(self.env.cr)
        self.env.cr.execute(
            "SELECT pricing_type FROM logistics_ftl_regional_minimum WHERE id = %s",
            [row_id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], "corridor_default")

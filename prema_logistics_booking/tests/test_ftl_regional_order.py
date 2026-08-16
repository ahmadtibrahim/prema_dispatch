"""FTL Regional Pricing — manual drag-and-drop ordering tests.

Ordering is display-only: sequence changes must never affect pricing,
rule matching, or duplicate validation.
"""
import importlib.util
import os

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase


def _load_renumber_helper():
    path = os.path.join(
        os.path.dirname(__file__), "..", "migrations", "18.0.9.0.0", "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("post_migrate_180900", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.renumber_ftl_regional_pricing


class TestFtlRegionalOrder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        cls.r_york = Region.create({"code": "FTO-YRK", "name": "York", "is_official_ltl_region": True})
        cls.r_durham = Region.create({"code": "FTO-DUR", "name": "Durham", "is_official_ltl_region": True})
        cls.r_northumberland = Region.create({"code": "FTO-NOR", "name": "Northumberland", "is_official_ltl_region": True})

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "FTO-Corridor",
            "direction": "eastbound",
            "enable_ftl": True,
            "ftl_rate_per_km": 3.0,
        })
        Rule = cls.env["logistics.ftl.regional.minimum"]
        cls.rule_york = Rule.create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.r_york.id,
            "destination_region_id": cls.r_northumberland.id,
            "pricing_type": "flat_rate",
            "flat_rate": 300.0,
        })
        cls.rule_durham = Rule.create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.r_durham.id,
            "destination_region_id": cls.r_northumberland.id,
            "pricing_type": "per_km",
            "ftl_rate_per_km_override": 2.75,
        })
        cls.rule_north = Rule.create({
            "corridor_id": cls.corridor.id,
            "origin_region_id": cls.r_york.id,
            "destination_region_id": cls.r_durham.id,
            "pricing_type": "corridor_default",
        })

    def _order_ids(self):
        return self.corridor.ftl_regional_minimum_ids.ids

    def test_01_migration_renumbers_existing_rows_10_20_30(self):
        """Pre-upgrade rows (all default sequence 10) are renumbered per
        corridor preserving the previous visible order, without touching
        pricing values."""
        cursor = self.env.cr
        cursor.execute("""
            INSERT INTO logistics_ftl_regional_minimum
                (corridor_id, origin_region_id, destination_region_id,
                 pricing_type, flat_rate, ftl_rate_per_km_override,
                 currency_id, sequence, active,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, 'per_km', 0.0, 2.25, %s, 10, TRUE, %s, %s,
                    now(), now())
            RETURNING id
        """, (
            self.corridor.id, self.r_durham.id, self.r_york.id,
            self.env.company.currency_id.id, self.env.uid, self.env.uid,
        ))
        inserted_id = cursor.fetchone()[0]

        _load_renumber_helper()(cursor)

        cursor.execute(
            "SELECT sequence, ftl_rate_per_km_override "
            "FROM logistics_ftl_regional_minimum WHERE id = %s",
            [inserted_id],
        )
        sequence, override = cursor.fetchone()
        # Old visible order was (origin region, destination region, id).
        # York-origin rows sort before Durham rows (York was created
        # first), so the inserted Durham→York row is third → sequence 30.
        self.assertEqual(sequence, 30)
        # Pricing untouched by renumbering.
        self.assertAlmostEqual(override, 2.25, places=2)

    def test_02_dragging_changes_sequence_and_order(self):
        """Moving a row (as the handle widget does) updates its sequence and
        the displayed order."""
        before = self._order_ids()
        self.assertEqual(before, [self.rule_york.id, self.rule_durham.id, self.rule_north.id])

        # Drag the last row to the top (the handle widget writes sequence,
        # then the list re-sorts on re-render).
        self.rule_north.sequence = 5
        self.corridor.invalidate_recordset(["ftl_regional_minimum_ids"])
        after = self._order_ids()
        self.assertEqual(after, [self.rule_north.id, self.rule_york.id, self.rule_durham.id])

    def test_03_order_persists_after_refresh(self):
        self.rule_north.sequence = 5
        self.corridor.invalidate_recordset(["ftl_regional_minimum_ids"])
        fresh = self.env["logistics.corridor"].browse(self.corridor.id)
        self.assertEqual(
            fresh.ftl_regional_minimum_ids.ids,
            [self.rule_north.id, self.rule_york.id, self.rule_durham.id],
        )

    def test_04_pricing_identical_after_reorder(self):
        prices_before = {
            self.rule_york.id: self.corridor.compute_ftl_price(
                self.r_york, self.r_northumberland, 33.5,
            )["price"],
            self.rule_durham.id: self.corridor.compute_ftl_price(
                self.r_durham, self.r_northumberland, 85.7,
            )["price"],
            self.rule_north.id: self.corridor.compute_ftl_price(
                self.r_york, self.r_durham, 52.2,
            )["price"],
        }
        self.rule_north.sequence = 5
        self.rule_durham.sequence = 15
        prices_after = {
            self.rule_york.id: self.corridor.compute_ftl_price(
                self.r_york, self.r_northumberland, 33.5,
            )["price"],
            self.rule_durham.id: self.corridor.compute_ftl_price(
                self.r_durham, self.r_northumberland, 85.7,
            )["price"],
            self.rule_north.id: self.corridor.compute_ftl_price(
                self.r_york, self.r_durham, 52.2,
            )["price"],
        }
        self.assertEqual(prices_after, prices_before)
        # Rule matching is also order-independent.
        self.assertEqual(
            self.corridor.get_ftl_regional_rule(self.r_york, self.r_northumberland),
            self.rule_york,
        )

    def test_05_duplicate_validation_unchanged_after_reorder(self):
        self.rule_north.sequence = 5
        with self.assertRaises(ValidationError):
            self.env["logistics.ftl.regional.minimum"].create({
                "corridor_id": self.corridor.id,
                "origin_region_id": self.r_york.id,
                "destination_region_id": self.r_northumberland.id,
                "pricing_type": "per_km",
                "ftl_rate_per_km_override": 2.0,
            })

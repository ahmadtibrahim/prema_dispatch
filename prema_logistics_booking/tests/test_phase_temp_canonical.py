# -*- coding: utf-8 -*-
"""18-section work order §3-§4: canonical temperature model + C/F UX.

Covers the canonical-model work on top of test_phase4_temperature's engine
matrix (which already proves conversion, intersection, conflicts, overrides):
- mixin wiring on all five booking models (boundary validation)
- create-time mirror + supplied-flag behavior (0°C survival)
- booking → pallet → item → job canonical snapshots at creation
- booking→job freeze via _create_dispatch_operation
- phone + book-load wizard F→C unit intake
- schema surface via information_schema

Run: --test-tags /prema_logistics_booking/tests/test_phase_temp_canonical
"""
import datetime

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase

from ..services.temperature_service import validate_range

_MIXIN_MODELS = [
    "logistics.pricing.session",
    "logistics.recurring.agreement",
    "logistics.recurring.job",
    "logistics.weekly.plan.reservation",
    "logistics.custom.quote",
]

# Canonical columns that every model in the chain must expose, by table.
_CANONICAL_COLUMNS = (
    "target_temperature_c", "minimum_temperature_c", "maximum_temperature_c",
    "temperature_tolerance_c", "temperature_supplied",
    "minimum_temperature_supplied", "maximum_temperature_supplied",
    "submitted_temperature_unit", "temperature_requirement_source",
)

_CANONICAL_TABLES = [
    "logistics_pricing_session",
    "logistics_recurring_agreement",
    "logistics_recurring_job",
    "logistics_weekly_plan_reservation",
    "logistics_custom_quote",
    "logistics_booking_pallet",
    "prema_dispatch_item",
    "prema_dispatch_job",
]

# The JOB's canonical target IS the legacy required_temperature_c itself
# (no target_temperature_c mirror on prema.dispatch.job).
_JOB_EXPECTED = set(_CANONICAL_COLUMNS) - {"target_temperature_c"}


class TestPhaseTempCanonical(TransactionCase):
    """§3 data model + §4 C/F UX."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "TC Customer"})
        cls.location = cls.env["prema.dispatch.location"].create({
            "name": "TC Location",
            "address": "123 Canonical Way, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    # ── §3: mixin wiring ─────────────────────────────────────────────

    def test_a_mixin_boundary_validation_all_five_models(self):
        """min > max must raise on every mixin model — validation travels
        with the shared block, not per-model duplicates."""
        for model in _MIXIN_MODELS:
            rec = self.env[model].new({
                "target_temperature_c": 2.0,
                "minimum_temperature_c": 10.0,   # above the max → invalid
                "maximum_temperature_c": 4.0,
                "temperature_supplied": True,
                "minimum_temperature_supplied": True,
                "maximum_temperature_supplied": True,
            })
            with self.assertRaises(ValidationError, msg=model):
                rec._check_temperature_boundaries()

    def test_b_mixin_created_supplied_flags_and_mirror(self):
        """Transient pricing session: create syncs the legacy pair and
        derives the supplied flags from raw existence (0.0 counts)."""
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.partner.id,
            "temperature_mode": "reefer",
            "required_temperature_c": -5.0,
            "pallets": 1, "weight_lbs": 1000.0,
            "expires_at": datetime.datetime(2026, 9, 2, 12, 0),
        })
        self.assertEqual(session.target_temperature_c, -5.0)
        self.assertTrue(session.temperature_supplied)
        self.assertEqual(session.submitted_temperature_unit, "c")

    def test_c_zero_celsius_survives_create(self):
        """0°C is a real reefer setpoint, never 'not set'."""
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.partner.id,
            "temperature_mode": "reefer",
            "required_temperature_c": 0.0,
            "pallets": 1, "weight_lbs": 1000.0,
            "expires_at": datetime.datetime(2026, 9, 2, 12, 0),
        })
        self.assertTrue(session.temperature_supplied)
        self.assertEqual(session.target_temperature_c, 0.0)
        errors, _effective = validate_range(
            0.0, None, None, None,
            target_supplied=session.temperature_supplied)
        self.assertFalse(errors)

    # ── §4: wizard unit intake ───────────────────────────────────────

    def test_d_phone_wizard_f_to_c_conversion(self):
        """Phone booking: F intake converts to canonical C at both create
        points; dry carries nothing."""
        wizard = self.env["logistics.phone.booking"].new({
            "temperature_mode": "reefer",
            "required_temperature_c": 32.0,
            "submitted_temperature_unit": "f",
        })
        self.assertEqual(wizard._canonical_required_temperature(), 0.0)
        wizard2 = self.env["logistics.phone.booking"].new({
            "temperature_mode": "reefer",
            "required_temperature_c": 33.8,
            "submitted_temperature_unit": "f",
        })
        self.assertAlmostEqual(wizard2._canonical_required_temperature(), 1.0)
        dry = self.env["logistics.phone.booking"].new({
            "temperature_mode": "dry",
            "required_temperature_c": 5.0,
        })
        self.assertIsNone(dry._canonical_required_temperature())

    def test_e_bookload_wizard_f_to_c_conversion(self):
        """Invoice book-load wizard: same unit intake; dry → None."""
        Wizard = self.env["prema.dispatch.book.load.wizard"]
        reefer_f = Wizard.new({
            "equipment_type": "reefer",
            "required_temperature_c": 32.0,
            "submitted_temperature_unit": "f",
        })
        self.assertEqual(reefer_f._canonical_required_temperature(), 0.0)
        reefer_c = Wizard.new({
            "equipment_type": "reefer",
            "required_temperature_c": 2.0,
            "submitted_temperature_unit": "c",
        })
        self.assertEqual(reefer_c._canonical_required_temperature(), 2.0)
        dry = Wizard.new({
            "equipment_type": "dry",
            "required_temperature_c": -18.0,
        })
        self.assertIsNone(dry._canonical_required_temperature())

    # ── §3: snapshot chain ───────────────────────────────────────────

    def _reefer_booking(self):
        return self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "temperature_mode": "reefer",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "reefer", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "estimated_delivery_date": datetime.date(2026, 9, 1),
            "required_temperature_c": -5.0,
            "minimum_temperature_c": -8.0,
            "maximum_temperature_c": -2.0,
            "temperature_tolerance_c": 3.0,
            "submitted_temperature_unit": "f",
            "price_snapshot": [{
                "line": "TC test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })

    def test_f_booking_to_pallet_snapshot(self):
        """A pallet created without explicit temp values snapshots the
        booking's canonical requirement (unit + source included)."""
        booking = self._reefer_booking()
        stop = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10,
            "stop_type": "pickup", "saved_location_id": self.location.id,
            "city": "Pickup City", "pallet_count": 1,
        })
        pallet = self.env["logistics.booking.pallet"].create({
            "booking_id": booking.id, "pickup_stop_id": stop.id,
            "weight_lbs": 2400.0,
        })
        self.assertTrue(pallet.temperature_supplied)
        self.assertEqual(pallet.target_temperature_c, -5.0)
        self.assertEqual(pallet.minimum_temperature_c, -8.0)
        self.assertEqual(pallet.maximum_temperature_c, -2.0)
        self.assertEqual(pallet.submitted_temperature_unit, "f")
        self.assertEqual(pallet.temperature_requirement_source, "customer")
        # Explicit intake wins over the snapshot.
        explicit = self.env["logistics.booking.pallet"].create({
            "booking_id": booking.id, "pickup_stop_id": stop.id,
            "weight_lbs": 2400.0, "target_temperature_c": 1.0,
            "temperature_supplied": True,
        })
        self.assertEqual(explicit.target_temperature_c, 1.0)

    def test_g_booking_to_job_freeze(self):
        """_create_dispatch_operation freezes the canonical block onto the
        job at creation (mirror of the legacy required_temperature_c)."""
        booking = self._reefer_booking()
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self.location.id, "city": "Pickup City",
             "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self.location.id, "city": "Delivery City",
             "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        self.assertTrue(job.temperature_supplied)
        # The job's canonical target IS the legacy required_temperature_c.
        self.assertEqual(job.required_temperature_c, -5.0)
        self.assertEqual(job.submitted_temperature_unit, "f")
        self.assertEqual(job.temperature_requirement_source, "customer")

    def test_h_item_snapshot_prefers_pallet(self):
        """Dispatch item creation snapshots from the linked booking pallet
        (fallback: the job's frozen requirement)."""
        booking = self._reefer_booking()
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self.location.id, "city": "Pickup City",
             "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self.location.id, "city": "Delivery City",
             "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        item = self.env["prema.dispatch.item"].create({
            "job_id": job.id, "name": "TC Skid",
            "logistics_booking_pallet_id": job.item_ids[0].logistics_booking_pallet_id.id,
        })
        self.assertTrue(item.temperature_supplied)
        self.assertEqual(item.target_temperature_c, -5.0)
        self.assertEqual(item.minimum_temperature_c, -8.0)
        self.assertEqual(item.maximum_temperature_c, -2.0)

    def test_i_dry_chain_carries_nothing(self):
        """Dry bookings leave every temperature field unset end to end."""
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "dry", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 500.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "TC dry",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        self.assertFalse(booking.temperature_supplied)
        self.assertFalse(booking.target_temperature_c)
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self.location.id, "city": "Pickup City",
             "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self.location.id, "city": "Delivery City",
             "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        self.assertFalse(job.temperature_supplied)
        self.assertFalse(job.required_temperature_c)

    # ── schema surface ───────────────────────────────────────────────

    def test_j_canonical_columns_exist_everywhere(self):
        """Every canonical column on every table of the chain — the view
        layer and the engine read these fields unconditionally."""
        self.env.cr.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_name = ANY(%s)"
            "   AND column_name = ANY(%s)",
            (_CANONICAL_TABLES, list(_CANONICAL_COLUMNS)))
        present = {}
        for table, column in self.env.cr.fetchall():
            present.setdefault(table, set()).add(column)
        for table in _CANONICAL_TABLES:
            expected = (_JOB_EXPECTED if table == "prema_dispatch_job"
                        else set(_CANONICAL_COLUMNS))
            missing = expected - present.get(table, set())
            self.assertFalse(missing, f"{table} missing: {sorted(missing)}")

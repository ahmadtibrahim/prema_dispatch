# -*- coding: utf-8 -*-
"""Phase 4 targeted tests — 18-section work order Sections 3-6.

Canonical temperature model (Celsius storage, C/F intake), safe
multi-shipment engine (intersection, conflict — never average), dynamic
reefer states (pre-cool / on / off), override authorization, driver
acknowledgment, timeline events, driver payload.

Run: --test-tags /prema_logistics_booking/tests/test_phase4_temperature
"""
import datetime

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase

from ..services.temperature_service import (
    C_TO_F, F_TO_C, _temp_supplied, format_dual, format_temp,
    parse_temperature, range_dual, validate_range,
)
from ..services.temperature_engine import TemperatureEngine


class TestPhase4Temperature(TransactionCase):
    """§3-§6 temperature model + engine matrix."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})  # deterministic pre-cool labels
        cls.partner = env["res.partner"].create({"name": "P4 Reefer Customer"})
        cls.partner2 = env["res.partner"].create({"name": "P4 Second Customer"})
        brand = env["fleet.vehicle.model.brand"].create({"name": "P4 Brand"})
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P4 Truck Model", "brand_id": brand.id})
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P4-TRUCK-01", "license_plate": "P4-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id,
            "x_reefer": True,
        })
        cls.layout = env["prema.dispatch.vehicle.layout.template"].create({
            "name": "P4 Layout", "layout_type": "straight",
            "is_verified": True,
            "position_ids": [
                (0, 0, {"position_code": c, "sequence": i * 10})
                for i, c in enumerate(("A1", "A2", "A3"), start=1)
            ],
        })

    # ── fixtures ─────────────────────────────────────────────────────

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"789 P4 Reefer Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    @classmethod
    def _booking(cls, partner=None, target=None, min_c=None, max_c=None,
                 tolerance=None, pallets=1, pre_cool_minutes=None,
                 **extra):
        """Reefer booking with the canonical temperature fields."""
        env = cls.env
        partner = partner or cls.partner
        vals = {
            "partner_id": partner.id,
            "shipment_type": "ltl",
            "temperature_mode": "reefer",
            "service_mode": "dedicated",
            "load_type": "ltl",
            "equipment_requirement": "reefer",
            "pallets": pallets,
            "physical_pallets": pallets,
            "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "estimated_delivery_date": datetime.date(2026, 9, 1),
            "commodity": "Reefer freight",
            "price_snapshot": [{
                "line": "P4 test",
                "_pallet_allocs": [
                    {"pallet": p, "stops": [1], "shared": False}
                    for p in range(1, pallets + 1)
                ],
            }],
        }
        if target is not None:
            vals["required_temperature_c"] = target
        if min_c is not None:
            vals["minimum_temperature_c"] = min_c
        if max_c is not None:
            vals["maximum_temperature_c"] = max_c
        if tolerance is not None:
            vals["temperature_tolerance_c"] = tolerance
        if pre_cool_minutes is not None:
            vals["reefer_pre_cool_minutes"] = pre_cool_minutes
        vals.update(extra)
        booking = env["logistics.booking"].create(vals)
        stops = env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": cls._location("P4 Pickup").id,
             "city": "Pickup City", "pallet_count": pallets},
            {"booking_id": booking.id, "sequence": 20,
             "stop_type": "delivery",
             "saved_location_id": cls._location("P4 Delivery").id,
             "city": "Delivery City", "pallet_count": pallets},
        ])
        # Canonical booking-pallet rows (production confirm creates them):
        # the dispatch items' logistics_booking_pallet_id bridge is what
        # ties a pallet to ITS booking even after job consolidation.
        pickup_stop = stops.filtered(lambda s: s.stop_type == "pickup")[:1]
        env["logistics.booking.pallet"].create([
            {
                "booking_id": booking.id,
                "sequence": p * 10,
                "label": "P4-P%d" % p,
                "pickup_stop_id": pickup_stop.id,
                "weight_lbs": 2400.0 / pallets,
            }
            for p in range(1, pallets + 1)
        ])
        return booking

    @classmethod
    def _dry_booking(cls):
        """Dry booking with a pallet allocation + stops, so a dispatch job
        actually gets items (the engine must see reefer-less freight)."""
        env = cls.env
        booking = env["logistics.booking"].create({
            "partner_id": cls.partner.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "dry", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 500.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "P4 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": cls._location("P4 Dry Pickup").id,
             "city": "Dry Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20,
             "stop_type": "delivery",
             "saved_location_id": cls._location("P4 Dry Delivery").id,
             "city": "Dry Delivery City", "pallet_count": 1},
        ])
        return booking

    @classmethod
    def _job(cls, booking, depart=True, status="loaded", **extra):
        """Booking → dispatch job with items in the given status."""
        job = booking._create_dispatch_job()
        job.pickup_saved_location_id = (
            booking.stop_ids.filtered(lambda s: s.stop_type == "pickup")
            [:1].saved_location_id.id)
        if depart:
            job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup").write({
                    "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 0)})
        if status:
            job.item_ids.write({"status": status})
        if extra:
            job.write(extra)
        return job

    # ── §3: conversion + formatting ──────────────────────────────────

    def test_a_cf_conversion_roundtrip(self):
        self.assertAlmostEqual(C_TO_F(2.0), 35.6, places=1)
        self.assertAlmostEqual(F_TO_C(35.6), 2.0, places=1)
        self.assertAlmostEqual(F_TO_C(C_TO_F(-18.0)), -18.0, places=6)
        self.assertAlmostEqual(C_TO_F(F_TO_C(41.0)), 41.0, places=6)
        self.assertAlmostEqual(C_TO_F(0.0), 32.0, places=6)

    def test_b_zero_celsius_never_missing(self):
        # parse: ''/None → None (missing), '0' → 0.0 (supplied)
        self.assertIsNone(parse_temperature(""))
        self.assertIsNone(parse_temperature(None))
        self.assertEqual(parse_temperature("0"), 0.0)
        self.assertEqual(parse_temperature(0), 0.0)
        # _temp_supplied distinguishes unset (False/None/'') from 0.0 —
        # valid only on RAW boundary values (create/write vals, intake).
        self.assertFalse(_temp_supplied(False))
        self.assertFalse(_temp_supplied(None))
        self.assertFalse(_temp_supplied(""))
        self.assertTrue(_temp_supplied(0.0))
        self.assertTrue(_temp_supplied(2.0))
        # Formatting: unset renders BLANK, never '0°C'
        self.assertEqual(format_temp(False), "")
        self.assertEqual(format_temp(0.0), "0°C")
        self.assertEqual(format_dual(0.0), "0°C / 32°F")
        self.assertEqual(format_temp(2.0, "f"), "35.6°F")
        self.assertEqual(format_dual(2.0), "2°C / 35.6°F")
        self.assertEqual(format_dual(2.0, f_first=True), "35.6°F / 2°C")
        self.assertEqual(range_dual(1.0, 3.0), "1°C – 3°C (33.8°F – 37.4°F)")

    def test_c_negative_and_decimal(self):
        self.assertEqual(parse_temperature("-18"), -18.0)
        self.assertEqual(parse_temperature("2.5"), 2.5)
        self.assertEqual(format_temp(-18.0), "-18°C")
        self.assertEqual(format_temp(2.5), "2.5°C")
        self.assertAlmostEqual(parse_temperature("35.6", "f"), 2.0, places=1)

    def test_d_booking_supplied_flag_and_mirror(self):
        # Dry booking, no temp → not supplied; NULL (never 0.0)
        dry = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "dry", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 500.0,
            "pickup_date": datetime.date(2026, 9, 1),
        })
        # Unset = NOT supplied (flag), even though Odoo 18 reads the
        # float back as 0.0 — presence is never inferred from the float.
        self.assertFalse(dry.temperature_supplied)
        self.assertFalse(dry.minimum_temperature_supplied)
        self.assertFalse(dry.maximum_temperature_supplied)
        # Legacy 0.0 → target mirror 0.0 + supplied True (0°C is real)
        b = self._booking(target=0.0)
        self.assertEqual(b.target_temperature_c, 0.0)
        self.assertTrue(b.temperature_supplied)
        # Mirror is bidirectional: writing the target updates the legacy
        # canonical field AND the supplied flag follows the raw write.
        b.target_temperature_c = -2.0
        self.assertEqual(b.required_temperature_c, -2.0)
        self.assertTrue(b.temperature_supplied)
        # Clearing the requirement (raw False) flips the flag off.
        b.write({"required_temperature_c": False})
        self.assertFalse(b.temperature_supplied)

    def test_e_portal_f_to_c_intake(self):
        # The controller intake contract: raw value in submitted unit,
        # server converts F→C (parse_temperature unit='f').
        c = parse_temperature("35.6", unit="f")
        self.assertAlmostEqual(c, 2.0, places=1)
        c0 = parse_temperature("32", unit="f")
        self.assertEqual(c0, 0.0)  # 32°F → 0°C — still supplied!
        self.assertTrue(_temp_supplied(c0))
        # 0°F is a real cold temperature, not missing
        self.assertAlmostEqual(parse_temperature("0", unit="f"), -17.7778,
                               places=3)

    # ── §4: validation ───────────────────────────────────────────────

    def test_f_boundary_validation(self):
        errors, _r = validate_range(2.0, 5.0, 1.0, None)
        self.assertTrue(errors)  # min > max
        with self.assertRaises(ValidationError):
            self._booking(target=2.0, min_c=5.0, max_c=1.0)
        with self.assertRaises(ValidationError):
            self._booking(target=10.0, min_c=1.0, max_c=5.0)
        with self.assertRaises(ValidationError):
            self._booking(target=2.0, min_c=1.0, max_c=5.0, tolerance=-1.0)
        # Valid: target inside range
        ok = self._booking(target=3.0, min_c=1.0, max_c=5.0)
        self.assertTrue(ok.temperature_supplied)
        # 0.0 bounds are REAL bounds
        ok0 = self._booking(target=0.0, min_c=-2.0, max_c=0.0)
        self.assertEqual(ok0.minimum_temperature_c, -2.0)
        self.assertEqual(ok0.maximum_temperature_c, 0.0)

    # ── §5: engine — per-item requirement ────────────────────────────

    def test_g_item_requirement_resolution(self):
        # Target only → exact setpoint range
        b = self._booking(target=2.0)
        job = self._job(b)
        engine = TemperatureEngine(self.env)
        req = engine.item_requirement(job.item_ids[0])
        self.assertIsNotNone(req)
        self.assertEqual((req["range_min_c"], req["range_max_c"]), (2.0, 2.0))
        # Tolerance applied when no explicit min/max
        b2 = self._booking(target=2.0, tolerance=1.0)
        req2 = engine.item_requirement(self._job(b2).item_ids[0])
        self.assertEqual((req2["range_min_c"], req2["range_max_c"]), (1.0, 3.0))
        # Explicit min/max WINS over tolerance
        b3 = self._booking(target=2.0, min_c=-2.0, max_c=8.0, tolerance=1.0)
        req3 = engine.item_requirement(self._job(b3).item_ids[0])
        self.assertEqual((req3["range_min_c"], req3["range_max_c"]), (-2.0, 8.0))
        # Dry freight → None (a dispatch item exists; no requirement)
        dry_job = self._job(self._dry_booking())
        self.assertIsNone(engine.item_requirement(dry_job.item_ids[0]))
        # 0°C requirement is real
        b0 = self._booking(target=0.0)
        req0 = engine.item_requirement(self._job(b0).item_ids[0])
        self.assertIsNotNone(req0)
        self.assertEqual(req0["target_c"], 0.0)

    # ── §5: engine — states ──────────────────────────────────────────

    def test_h_dry_job_state_none(self):
        job = self._job(self._dry_booking())
        state = TemperatureEngine(self.env).recalc(job)
        self.assertEqual(state["state"], "none")
        self.assertFalse(state["conflict"])
        self.assertEqual(job.temperature_state, "none")

    def test_i_reefer_on_exact_setpoint(self):
        job = self._job(self._booking(target=2.0))
        state = TemperatureEngine(self.env).recalc(job)
        self.assertEqual(state["state"], "on")
        self.assertEqual(state["setpoint_c"], 2.0)
        self.assertTrue(state["compatible"])
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, 2.0)
        self.assertIn("REEFER", state["message"])
        # Driver payload exposes the dual-unit instruction
        payload = job._driver_temperature_payload()
        self.assertTrue(payload["required"])
        self.assertEqual(payload["state"], "on")
        self.assertEqual(payload["setpoint"], "2°C / 35.6°F")
        self.assertEqual(payload["range"], "2°C – 2°C (35.6°F – 35.6°F)")
        self.assertEqual(payload["display_unit"], "c")
        self.assertFalse(payload["setpoint_acknowledged"])

    def test_j_compatible_intersection(self):
        """Two shipments with overlapping ranges: safe intersection used,
        setpoint = a shipment target inside every range."""
        b_a = self._booking(target=2.0, tolerance=1.0)   # [1,3]
        b_b = self._booking(target=3.0, tolerance=1.0)   # [2,4]
        job_a = self._job(b_a)
        job_b = self._job(b_b, vehicle_id=self.vehicle.id)
        # Combine both bookings' items onto ONE job (consolidated visit).
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        state = TemperatureEngine(self.env).recalc(job_a)
        self.assertEqual(state["state"], "on")
        self.assertEqual(state["safe_min_c"], 2.0)
        self.assertEqual(state["safe_max_c"], 3.0)
        self.assertTrue(2.0 <= state["setpoint_c"] <= 3.0,
                        f"setpoint {state['setpoint_c']} must sit inside "
                        "the intersection")
        self.assertIn(state["setpoint_c"], (2.0, 3.0))
        self.assertEqual(state["onboard_count"], 2)
        self.assertFalse(state["conflict"])

    def test_k_conflict_never_averaged(self):
        """Incompatible ranges → conflict; the setpoint is NEVER the
        average (6°C here); conflicting bookings are identified."""
        b_a = self._booking(target=2.0)    # exact [2,2]
        b_b = self._booking(target=10.0, partner=self.partner2)  # [10,10]
        job_a = self._job(b_a)
        job_b = self._job(b_b, vehicle_id=self.vehicle.id)
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        state = TemperatureEngine(self.env).recalc(job_a)
        self.assertEqual(state["state"], "conflict")
        self.assertFalse(state["compatible"])
        self.assertNotEqual(state["setpoint_c"], 6.0)
        self.assertIsNone(state["setpoint_c"])
        self.assertEqual(len(state["conflict_items"]), 2)
        customers = {c["customer_id"] for c in state["conflict_items"]}
        self.assertEqual(customers, {self.partner.id, self.partner2.id})
        self.assertIn("TEMPERATURE CONFLICT", state["message"])
        # Stored state + timeline event
        self.assertTrue(job_a.temperature_conflict)
        self.assertEqual(job_a.temperature_state, "conflict")
        events = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job_a.id),
            ("event_type", "=", "temperature_conflict")])
        self.assertEqual(len(events), 1)

    def test_l_override_records_and_applies(self):
        b_a = self._booking(target=2.0)
        b_b = self._booking(target=10.0, partner=self.partner2)
        job_a = self._job(b_a)
        job_b = self._job(b_b, vehicle_id=self.vehicle.id)
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        TemperatureEngine(self.env).recalc(job_a)
        self.assertTrue(job_a.temperature_conflict)

        # Authorized override: setpoint 4°C + reason → conflict resolved.
        override, state = TemperatureEngine(self.env).apply_override(
            job_a, 4.0, "Shipper accepted shared setpoint (P4 test)",
        )
        self.assertEqual(state["state"], "on")
        self.assertFalse(state["conflict"])
        self.assertEqual(job_a.temperature_instruction_c, 4.0)
        self.assertFalse(job_a.temperature_conflict)
        # The override record snapshots the ORIGINAL requirements.
        self.assertEqual(override.job_id.id, job_a.id)
        self.assertEqual(override.selected_setpoint_c, 4.0)
        self.assertTrue(override.original_requirements_json)
        import json
        orig = json.loads(override.original_requirements_json)
        self.assertEqual(len(orig["requirements"]), 2)
        # Driver acknowledgment flows to the override + job + timeline.
        override.action_driver_acknowledged()
        self.assertTrue(override.driver_acknowledged)
        self.assertTrue(job_a.reefer_acknowledged)
        events = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job_a.id), ("event_type", "=", "temperature")])
        self.assertTrue(events)

    def test_m_precool_phase_and_begin_time(self):
        """Reefer pickup ahead, nothing onboard → PRE-COOL state with the
        configured duration (default param 40 min; booking override 90)."""
        b = self._booking(target=2.0, pre_cool_minutes=90)
        job = self._job(b, depart=False, status=False)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        # Scheduled service 04:00 UTC → begin 02:30 UTC (90 min).
        # customer_eta_at is the engine's binding anchor. A scheduled_time
        # write normally triggers an ETA recompute (it is a
        # RECALC_TRIGGER_FIELD); here the ETA engine has "already run" —
        # the test writes the same values it would produce, under the
        # engine-write bypass flag.
        pickup.with_context(_eta_engine_write=True).write({
            "scheduled_time": datetime.datetime(2026, 9, 1, 4, 0),
            "customer_eta_at": datetime.datetime(2026, 9, 1, 4, 0),
        })
        state = TemperatureEngine(self.env).recalc(job)
        self.assertEqual(state["state"], "precool")
        self.assertEqual(state["setpoint_c"], 2.0)
        self.assertIn("PRE-COOL REEFER TO", state["message"])
        self.assertIn("2:30 am", state["message"])
        self.assertEqual(job.temperature_state, "precool")

        # Default parameter when booking has no duration.
        b2 = self._booking(target=2.0)
        job2 = self._job(b2, depart=False, status=False)
        pickup2 = job2.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        pickup2.with_context(_eta_engine_write=True).write({
            "scheduled_time": datetime.datetime(2026, 9, 1, 4, 0),
            "customer_eta_at": datetime.datetime(2026, 9, 1, 4, 0),
        })
        state2 = TemperatureEngine(self.env).recalc(job2)
        self.assertEqual(state2["state"], "precool")
        self.assertIn("3:20 am", state2["message"])  # 40-min default

    def test_n_reefer_off_only_when_safe(self):
        """After the last delivery the reefer turns OFF; while freight is
        still onboard it STAYS ON."""
        b = self._booking(target=2.0, pallets=2)
        job = self._job(b)
        TemperatureEngine(self.env).recalc(job)
        self.assertEqual(job.temperature_state, "on")
        # One pallet delivered, one still onboard → STAY ON.
        job.item_ids[0].write({"status": "delivered"})
        TemperatureEngine(self.env).recalc(job)
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, 2.0)
        # All delivered → OFF (safe: nothing onboard, no upcoming pickup).
        job.item_ids[1].write({"status": "delivered"})
        TemperatureEngine(self.env).recalc(job)
        self.assertEqual(job.temperature_state, "off")
        self.assertIn("REEFER OFF", job.temperature_message)
        # Off-ack flow.
        job.write({"reefer_off_acknowledged": True,
                   "reefer_off_ack_at": datetime.datetime(2026, 9, 1, 16, 0)})
        self.assertTrue(job.reefer_off_acknowledged)

    def test_o_restore_recalc_idempotent_no_timeline_noise(self):
        """Re-running the engine with no change produces NO new timeline
        events (refresh-safe)."""
        job = self._job(self._booking(target=2.0))
        engine = TemperatureEngine(self.env)
        engine.recalc(job)
        before = len(self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job.id)]))
        for _ in range(3):
            engine.recalc(job)
        after = len(self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job.id)]))
        self.assertEqual(before, after)

    def test_p_pallet_change_recalculates(self):
        """_sync_actual_pallet_items (pickup confirmation) recomputes the
        state: adding a conflicting pallet flips the job to conflict."""
        b_a = self._booking(target=2.0)
        b_b = self._booking(target=10.0, partner=self.partner2)
        job_a = self._job(b_a)
        # Bring the second booking's item onboard at the SAME pickup.
        job_b = self._job(b_b, vehicle_id=self.vehicle.id)
        pickup = job_a.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        job_b.item_ids.write({
            "job_id": job_a.id, "sequence": 30,
            "pickup_stop_id": pickup.id,
            "status": "loaded",
        })
        # The pallet-count sync path triggers the recalc.
        job_a._sync_actual_pallet_items(2, pickup_stop=pickup)
        self.assertTrue(job_a.temperature_conflict)
        self.assertEqual(job_a.temperature_state, "conflict")

    def test_q_driver_acknowledgment_route_contract(self):
        job = self._job(self._booking(target=2.0))
        TemperatureEngine(self.env).recalc(job)
        # setpoint ack
        job.write({"reefer_acknowledged": True,
                   "reefer_ack_at": datetime.datetime(2026, 9, 1, 8, 0)})
        self.assertTrue(job.reefer_acknowledged)
        payload = job._driver_temperature_payload()
        self.assertTrue(payload["setpoint_acknowledged"])

    def test_r_legacy_bookings_still_work(self):
        """Legacy-architecture records (no explicit source/unit) behave
        exactly as before: target from required_temperature_c, engine
        states, NULL dry."""
        b = self._booking(target=-18.0)
        b.write({"submitted_temperature_unit": False})
        self.assertEqual(b.target_temperature_c, -18.0)
        job = self._job(b)
        state = TemperatureEngine(self.env).recalc(job)
        self.assertEqual(state["state"], "on")
        self.assertEqual(state["setpoint_c"], -18.0)
        self.assertEqual(
            job._driver_temperature_payload()["setpoint"], "-18°C / -0.4°F")

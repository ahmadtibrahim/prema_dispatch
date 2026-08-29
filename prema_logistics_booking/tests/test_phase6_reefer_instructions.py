# -*- coding: utf-8 -*-
"""18-section work order §6: dynamic reefer instructions.

The engine's state machine recomputes on a fixed trigger set; this file
proves the triggers fire from the real write paths (not just direct engine
calls) and that the driver-acknowledgment timeline behaves:

- pickup completion drives precool → on (setpoint from the requirement)
- last reefer delivery completion drives on → off
- restore of a completed stop reverts off → on
- stop reorder with no requirement change recomputes noise-free
- relay transfer (driver/vehicle handoff) keeps the same instruction
- a CHANGED instruction invalidates the previous ack (stale acks are
  never presented as current); unchanged refresh leaves it intact
- steady conflict emits exactly ONE timeline event (no per-refresh noise)

Run: --test-tags /prema_logistics_booking/tests/test_phase6_reefer_instructions
"""
import datetime

from odoo.tests import TransactionCase


class TestPhase6ReeferInstructions(TransactionCase):
    """§6 dynamic instructions + ack timeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "P6 Customer"})
        brand = cls.env["fleet.vehicle.model.brand"].create(
            {"name": "P6 Brand"})
        vehicle_model = cls.env["fleet.vehicle.model"].create({
            "name": "P6 Truck Model", "brand_id": brand.id})
        cls.vehicle = cls.env["fleet.vehicle"].create({
            "name": "P6-TRUCK-01", "license_plate": "P6-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id, "x_reefer": True,
        })

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"789 P6 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _reefer_booking(self, target=-5.0, partner=None):
        return self.env["logistics.booking"].create({
            "partner_id": (partner or self.partner).id,
            "shipment_type": "ltl", "temperature_mode": "reefer",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "reefer", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "estimated_delivery_date": datetime.date(2026, 9, 1),
            "required_temperature_c": target,
            "minimum_temperature_c": target - 3.0,
            "maximum_temperature_c": target + 3.0,
            "submitted_temperature_unit": "f",
            "price_snapshot": [{
                "line": "P6 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })

    def _job(self, booking, depart=True):
        """Booking → dispatch job with pickup+delivery stops and items."""
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P6 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P6 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        if depart:
            job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup").write({
                    "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 0)})
            job.item_ids.write({"status": "loaded"})
        return job

    def _timeline_events(self, job, event_type=None):
        events = self.env["prema.dispatch.timeline.event"].search(
            [("job_id", "=", job.id)])
        if event_type:
            events = events.filtered(lambda e: e.event_type == event_type)
        return events

    # ── trigger verification ─────────────────────────────────────────

    def test_a_pickup_completion_drives_precool_to_on(self):
        """Departing+completing the pickup flips precool → on with the
        booking's setpoint, via the stop status write path."""
        booking = self._reefer_booking(target=-5.0)
        job = self._job(booking, depart=False)
        self.assertEqual(job.temperature_state, "precool")
        pickup = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        # The REAL trigger: stop status write (action_mark_completed goes
        # through the same write()) → EtaEngine → temperature recalc.
        pickup.write({
            "status": "completed",
            "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 30),
        })
        job.item_ids.write({"status": "loaded"})
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, -5.0)
        self.assertFalse(job.temperature_conflict)
        self.assertTrue(
            any(e.notes.startswith("Reefer setpoint")
                for e in self._timeline_events(job, "temperature")))

    def test_b_last_delivery_completion_drives_off(self):
        """Completing the only delivery → 'off' with the switch-off
        timeline event."""
        booking = self._reefer_booking()
        job = self._job(booking)
        self.assertEqual(job.temperature_state, "on")
        delivery = job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff")[:1]
        delivery.write({"status": "completed"})
        job.item_ids.write({"status": "delivered"})
        self.assertEqual(job.temperature_state, "off")
        self.assertTrue(
            any("switched off" in e.notes
                for e in self._timeline_events(job, "temperature")))

    def test_c_restore_reverts_off_to_on(self):
        """Restoring the completed delivery returns the instruction to
        'on' — the restore path (status → pending) is a §6 trigger."""
        booking = self._reefer_booking()
        job = self._job(booking)
        delivery = job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff")[:1]
        delivery.write({"status": "completed"})
        job.item_ids.write({"status": "delivered"})
        self.assertEqual(job.temperature_state, "off")
        # Restore path (action_restore_stop writes the same fields, then
        # _rebuild_item_custody reverts the item statuses — simulate).
        delivery.write({"status": "pending", "actual_departure_time": False})
        job.item_ids.write({"status": "loaded"})
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, -5.0)

    def test_d_reorder_is_noise_free(self):
        """A sequence reorder with no requirement change recomputes the
        same instruction without emitting a new timeline event."""
        booking = self._reefer_booking()
        job = self._job(booking)
        before = len(self._timeline_events(job, "temperature"))
        stops = job.stop_ids.sorted("sequence")
        first = stops[0]
        first.write({"sequence": 99})
        first.write({"sequence": 10})
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, -5.0)
        self.assertEqual(
            len(self._timeline_events(job, "temperature")), before,
            "unchanged instruction must not re-emit timeline events")

    def test_e_relay_transfer_keeps_instruction(self):
        """The relay transfer (driver/vehicle handoff at a transfer stop)
        is a §6 trigger — recomputes to the SAME instruction, no noise."""
        booking = self._reefer_booking()
        job = self._job(booking)
        driver2 = self.env["res.partner"].create({"name": "P6 Driver Two"})
        before = len(self._timeline_events(job, "temperature"))
        job.write({"driver_id": driver2.id, "vehicle_id": self.vehicle.id})
        self.assertEqual(job.temperature_state, "on")
        self.assertEqual(job.temperature_instruction_c, -5.0)
        self.assertEqual(
            len(self._timeline_events(job, "temperature")), before)

    # ── acknowledgment timeline ──────────────────────────────────────

    def test_f_changed_instruction_invalidates_stale_ack(self):
        """A changed setpoint clears the driver's previous ack — the app
        must never present a stale ack as current. An unchanged refresh
        leaves the ack intact."""
        booking = self._reefer_booking(target=-5.0)
        job = self._job(booking)
        job.write({
            "reefer_acknowledged": True,
            "reefer_ack_at": datetime.datetime(2026, 9, 1, 12, 0),
            "reefer_ack_user_id": self.env.uid,
        })
        # Refresh with NO change → ack survives.
        job._recalc_temperature()
        self.assertTrue(job.reefer_acknowledged)
        # The requirement changes (dispatcher corrects the target) →
        # setpoint moves → the ack is stale and must be cleared.
        booking.write({"required_temperature_c": 3.0,
                       "minimum_temperature_c": 0.0,
                       "maximum_temperature_c": 6.0})
        job._recalc_temperature()
        self.assertNotEqual(job.temperature_instruction_c, -5.0)
        self.assertFalse(job.reefer_acknowledged)
        # Fresh ack of the new setpoint sticks.
        job.write({"reefer_acknowledged": True})
        job._recalc_temperature()
        self.assertTrue(job.reefer_acknowledged)

    def test_g_steady_conflict_emits_one_event(self):
        """While a conflict persists, refresh recomputes stay quiet — the
        conflict timeline event (and the booking override-required flag
        mirror) fire on entry, never per refresh."""
        from ..services.temperature_engine import TemperatureEngine
        b_a = self._reefer_booking(target=2.0, partner=self.partner)
        b_b = self._reefer_booking(
            target=10.0, partner=self.env["res.partner"].create(
                {"name": "P6 Second Customer"}))
        job_a = self._job(b_a)
        job_b = self._job(b_b)
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        engine = TemperatureEngine(self.env)
        state = engine.recalc(job_a)
        self.assertEqual(state["state"], "conflict")
        self.assertTrue(
            any(e.event_type == "temperature_conflict"
                for e in self._timeline_events(job_a)))
        events_after_entry = len(self._timeline_events(job_a))
        self.assertTrue(b_a.temperature_override_required)
        # Three recompute refreshes — none may re-emit or toggle the flag.
        for _ in range(3):
            engine.recalc(job_a)
        events = self._timeline_events(job_a)
        self.assertEqual(len(events), events_after_entry)
        self.assertTrue(b_a.temperature_override_required)

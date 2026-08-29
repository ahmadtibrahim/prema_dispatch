# -*- coding: utf-8 -*-
"""18-section work order §8: Live Map truck progress payload.

The dispatcher's truck popup (live-map) is built by
prema.dispatch.job._live_map_truck_progress. Covers:

- reefer instruction rendered in the DISPATCHER's display unit
  (res.users.temperature_display_unit), never hardcoded C
- no phantom setpoint in off/conflict states (unset-Float read-back of
  stored NULL → 0.0 must not render '0 °C')
- conflict carries conflict=True + the dispatch-review message
- legacy /web/dispatch/live-map/data endpoint removed (live_map.js has
  used the ORM get_live_map_data feed for a long time — one authority)

Run: --test-tags /prema_logistics_booking/tests/test_phase8_live_map
"""
import datetime

from odoo.tests import TransactionCase


class TestPhase8LiveMap(TransactionCase):
    """§8 live-map truck progress payload."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P8 Customer"})
        cls.partner2 = env["res.partner"].create(
            {"name": "P8 Second Customer"})
        brand = env["fleet.vehicle.model.brand"].create(
            {"name": "P8 Brand"})
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P8 Truck Model", "brand_id": brand.id})
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P8-TRUCK-01", "license_plate": "P8-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id,
            "x_reefer": True, "x_operational_logistics": True,
        })

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"202 P8 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _reefer_booking(self, target, partner=None):
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
            "submitted_temperature_unit": "c",
            "price_snapshot": [{
                "line": "P8 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })

    def _job(self, booking):
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P8 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P8 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        job.write({"vehicle_id": self.vehicle.id})
        job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup").write({
                "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 0)})
        job.item_ids.write({"status": "loaded"})
        return job

    def _stops_payload(self, job):
        return [{
            "id": s.id, "type": s.stop_type, "status": s.status,
            "address": s.address or "", "job_name": job.name,
            "job_id": job.id, "lat": s.latitude or 0, "lng": s.longitude or 0,
        } for s in job.stop_ids.sorted("sequence")]

    def _progress(self, job):
        return job._live_map_truck_progress(
            job, self._stops_payload(job), job.vehicle_id)

    def test_a_on_setpoint_in_dispatcher_unit(self):
        """The reefer instruction renders in the DISPATCHER's display
        unit — C by default, F when the dispatcher prefers F."""
        booking = self._reefer_booking(target=-5.0)
        job = self._job(booking)
        self.env.user.write({"temperature_display_unit": "c"})
        progress = self._progress(job)
        reefer = progress["reefer"]
        self.assertEqual(reefer["state"], "on")
        self.assertIn("°C", reefer["setpoint"])
        self.assertIn("°C", reefer["range"])
        # Dispatcher switches their preference → the SAME job renders F.
        self.env.user.write({"temperature_display_unit": "f"})
        progress = self._progress(job)
        reefer = progress["reefer"]
        self.assertIn("°F", reefer["setpoint"])
        self.assertIn("°F", reefer["range"])
        self.env.user.write({"temperature_display_unit": "c"})

    def test_b_off_state_no_phantom_setpoint(self):
        """Completed reefer freight → state 'off' with an EMPTY setpoint —
        the stored-NULL read-back (0.0) must not render '0 °C'."""
        booking = self._reefer_booking(target=-5.0)
        job = self._job(booking)
        job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff").write({"status": "completed"})
        job.item_ids.write({"status": "delivered"})
        self.assertEqual(job.temperature_state, "off")
        reefer = self._progress(job)["reefer"]
        self.assertEqual(reefer["state"], "off")
        self.assertEqual(reefer["setpoint"], "")

    def test_c_conflict_payload(self):
        """Conflict → state conflict, conflict True, no setpoint, and the
        dispatch-review instruction reaches the dispatcher."""
        from ..services.temperature_engine import TemperatureEngine
        b_a = self._reefer_booking(target=2.0)
        b_b = self._reefer_booking(
            target=10.0, partner=self.partner2)
        job_a = self._job(b_a)
        job_b = self._job(b_b)
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        state = TemperatureEngine(self.env).recalc(job_a)
        self.assertEqual(state["state"], "conflict")
        reefer = self._progress(job_a)["reefer"]
        self.assertEqual(reefer["state"], "conflict")
        self.assertTrue(reefer["conflict"])
        self.assertEqual(reefer["setpoint"], "")
        self.assertIn("CONFLICT", reefer["instruction"])

    def test_d_legacy_endpoint_removed(self):
        """The legacy /web/dispatch/live-map/data controller is gone —
        live_map.js has long used the ORM get_live_map_data feed."""
        from odoo.addons.prema_dispatch.controllers.portal import (
            DispatchTrackingController)
        self.assertFalse(
            hasattr(DispatchTrackingController, "live_map_data"))

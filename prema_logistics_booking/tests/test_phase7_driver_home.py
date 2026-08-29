# -*- coding: utf-8 -*-
"""18-section work order §7: Driver Home — jobs[] payload.

Regression for the day-feed contract: /dispatch/driver/stops feeds
applyDay(), which reads S.jobs (per-stop temperature lookup + §17
start-plan card). The payload must carry `jobs` (only jobs with stops on
the requested date, earliest first), each with the start_plan and
temperature blocks — and every stop in the flat feed must resolve to a
job entry (the app's S.jobs.find(j=>j.id===stop.job_id)).

Run: --test-tags /prema_logistics_booking/tests/test_phase7_driver_home
"""
import datetime

from odoo.tests import TransactionCase


class TestPhase7DriverHome(TransactionCase):
    """§7 driver-home payload contract."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P7 Customer"})
        cls.driver = env["res.partner"].create({"name": "P7 Driver"})
        cls.driver_user = env["res.users"].create({
            "name": "p7driver", "login": "p7driver@test.local",
            "partner_id": cls.driver.id, "tz": "UTC",
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id,
                env.ref("prema_dispatch.group_dispatch_driver").id,
            ])],
        })
        brand = env["fleet.vehicle.model.brand"].create(
            {"name": "P7 Brand"})
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P7 Truck Model", "brand_id": brand.id})
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P7-TRUCK-01", "license_plate": "P7-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id, "x_reefer": True,
        })

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"101 P7 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _booking(self, pickup_date, target=None):
        vals = {
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "pallets": 1, "physical_pallets": 1,
            "weight_lbs": 2400.0,
            "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pickup_date": pickup_date,
            "estimated_delivery_date": pickup_date,
            "price_snapshot": [{
                "line": "P7 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        }
        if target is not None:
            vals.update({
                "temperature_mode": "reefer",
                "equipment_requirement": "reefer",
                "required_temperature_c": target,
                "minimum_temperature_c": target - 3.0,
                "maximum_temperature_c": target + 3.0,
                "submitted_temperature_unit": "c",
            })
        return self.env["logistics.booking"].create(vals)

    def _driver_job(self, name, pickup_dt, delivery_dt, reefer_target=None):
        """One assigned driver job with scheduled stop times (UTC)."""
        booking = self._booking(pickup_dt.date(), reefer_target)
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location(f"{name} Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location(f"{name} Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        job.write({"driver_id": self.driver.id,
                   "scheduled_pickup": pickup_dt})
        # Schedule the physical stops (the dispatch stop carries
        # scheduled_time; booking stops feed it via the job build).
        job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup").write(
                {"scheduled_time": pickup_dt})
        job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff").write(
                {"scheduled_time": delivery_dt})
        # Onboard freight so the temperature block reports a live state.
        job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup").write({
                "actual_departure_time": pickup_dt})
        job.item_ids.write({"status": "loaded"})
        return job

    def test_jobs_payload_shape(self):
        """The day feed carries `jobs`: only this date's jobs, earliest
        first, each with start_plan + temperature blocks; every feed stop
        resolves to a job entry (the app's S.jobs lookup)."""
        today = datetime.date.today()
        job_a = self._driver_job(
            "P7A", datetime.datetime.combine(today, datetime.time(9, 0)),
            datetime.datetime.combine(today, datetime.time(12, 0)),
            reefer_target=-5.0)
        job_b = self._driver_job(
            "P7B", datetime.datetime.combine(today, datetime.time(14, 0)),
            datetime.datetime.combine(today, datetime.time(16, 0)))
        # Same driver, but its stops fall on another day — must NOT appear
        # in today's jobs (its pickup sits inside the ±2-day fetch window,
        # proving the payload filter, not the fetch window, decides).
        job_c = self._driver_job(
            "P7C",
            datetime.datetime.combine(today + datetime.timedelta(days=2),
                                      datetime.time(8, 0)),
            datetime.datetime.combine(today + datetime.timedelta(days=2),
                                      datetime.time(11, 0)))
        self.assertEqual(len(job_a.item_ids), 1)

        payload = self.env["prema.dispatch.job"].with_user(
            self.driver_user).get_driver_stops_for_date(today.isoformat())

        jobs = payload.get("jobs")
        self.assertIsNotNone(jobs, "payload must carry the jobs array")
        job_ids = [j["id"] for j in jobs]
        self.assertEqual(sorted(job_ids), sorted([job_a.id, job_b.id]),
                         "only jobs with stops on the requested date")
        self.assertEqual(jobs[0]["id"], job_a.id,
                         "earliest job first (S.jobs[0] is the day's first)")
        for entry in jobs:
            self.assertIn("start_plan", entry)
            self.assertIn("mode", entry["start_plan"])
            self.assertIn("temperature", entry)
            self.assertIn("stops", entry)
        # Reefer job: the §4-§6 temperature block is the server-computed,
        # driver-preference dual-unit setpoint.
        temp_a = jobs[0]["temperature"]
        self.assertEqual(temp_a["state"], "on")
        self.assertTrue(temp_a["setpoint"])
        self.assertIn("°", temp_a["setpoint"])
        # Every flat-feed stop resolves to a jobs[] entry (the app's
        # S.jobs.find(j => j.id === stop.job_id) must always hit).
        known = {j["id"] for j in jobs}
        for stop in payload.get("stops", []):
            self.assertIn(stop.get("job_id"), known,
                          "feed stop without a jobs[] entry")
        self.assertNotIn(job_c.id, known)

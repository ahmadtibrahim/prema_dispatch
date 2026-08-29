"""Phase 2 targeted tests — unified ETA engine, driver start profile,
live recalculation, HOURS NOT VERIFIED (work order Section C).

The Section P example route, honored verbatim in LOCAL (Toronto, EDT)
time — Odoo stores naive UTC, so the fixture times are UTC (EDT+4):
  leave home 03:00 (07:00Z) → hub 03:30 → pretrip 10m → United Dairy
  04:00 (08:00Z) → depart 04:30 (08:30Z) → Terra Freska 05:00 (09:00Z)
  → Healthy Planet ≥09:00 (13:00Z).

Facility hours live in LOCAL time (open/close floats evaluated in the
stop's tz); all Odoo Datetime fields are UTC-naive.

Every test rolls back — nothing commits. Coordinates are placed so the
straight-line ×1.4 @ 50 km/h fallback yields exact leg times:
  20 min ≈ Δlat 0.107°, 30 min ≈ Δlat 0.160°, 40 min ≈ Δlat 0.214°.
"""

from datetime import datetime

from odoo.tests.common import TransactionCase


def _hours(open_t, close_t, weekday="1"):
    """Operating-hours snapshot for one weekday (1 = Tuesday)."""
    return {weekday: [open_t, close_t]}


class TestEtaEngineBase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "P2 Test Driver",
            "home_latitude": 43.50,
            "home_longitude": -79.40,
        })
        brand = cls.env["fleet.vehicle.model.brand"].create(
            {"name": "P2 Test Brand"})
        model = cls.env["fleet.vehicle.model"].create(
            {"name": "P2 Test Model", "brand_id": brand.id})
        cls.vehicle = cls.env["fleet.vehicle"].create({
            "model_id": model.id,
            "license_plate": "TEST-P2",
            "odometer_unit": "kilometers",
            "power_unit": "power",
            "driver_id": cls.partner.id,
            "driver_start_mode": "driver_home",
            "driver_pretrip_minutes": 10,
            "driver_home_to_hub_minutes": 30,
            "x_home_base_lat": 43.000000,
            "x_home_base_lng": -79.000000,
        })
        # United Dairy / Terra Freska / Healthy Planet — the P example.
        # Pins MUST match the stops' manual coords: _apply_saved_location
        # writes location.pin_* onto the stop at create, and a pinless
        # location zeroes them (0.0 → the ETA walk loses the position).
        cls.ud = cls.env["prema.dispatch.location"].create({
            "name": "United Dairy", "address": "1 Dairy Rd",
            "pin_lat": 43.107, "pin_lng": -79.0, "pin_set": True,
            "hours_verified": True,
        })
        cls.tf = cls.env["prema.dispatch.location"].create({
            "name": "Terra Freska", "address": "2 Freska Rd",
            "pin_lat": 43.267, "pin_lng": -79.0, "pin_set": True,
            "hours_verified": True,
        })
        cls.hp = cls.env["prema.dispatch.location"].create({
            "name": "Healthy Planet", "address": "3 Planet Rd",
            "pin_lat": 43.481, "pin_lng": -79.0, "pin_set": True,
            "hours_verified": True,
        })
        cls.env["prema.dispatch.location.hours"].create({
            "facility_id": cls.ud.id, "day_of_week": "1",
            "service_scope": "general", "status": "custom",
            "open_time": 4.0, "close_time": 18.0, "sequence": 10,
        })
        cls.env["prema.dispatch.location.hours"].create({
            "facility_id": cls.tf.id, "day_of_week": "1",
            "service_scope": "general", "status": "custom",
            "open_time": 5.0, "close_time": 18.0, "sequence": 10,
        })
        cls.env["prema.dispatch.location.hours"].create({
            "facility_id": cls.hp.id, "day_of_week": "1",
            "service_scope": "general", "status": "custom",
            "open_time": 9.0, "close_time": 17.0, "sequence": 10,
        })
        cls.job = cls.env["prema.dispatch.job"].create({
            "name": "JOB-P2-ETA",
            "partner_id": cls.partner.id,
            "vehicle_id": cls.vehicle.id,
            "driver_id": cls.partner.id,
            "scheduled_pickup": datetime(2026, 9, 8, 7, 40),
            "planned_operational_start": datetime(2026, 9, 8, 7, 40),
        })

    @classmethod
    def _stop(cls, job, stop_type, lat, sequence, service=15,
              scheduled=None, loc=None, hours=None, **extra):
        vals = {
            "job_id": job.id,
            "stop_type": stop_type,
            "address": "P2 Stop Rd",
            "latitude": lat,
            "longitude": -79.0,
            "sequence": sequence,
            "status": "pending",
            "service_time_minutes": service,
            "tz_name": "America/Toronto",
            "operating_hours_snapshot": hours,
            "scheduled_time": scheduled or datetime(2026, 9, 8, 8, 0),
        }
        if loc:
            vals["saved_location_id"] = loc.id
        vals.update(extra)
        return cls.env["prema.dispatch.stop"].create(vals)

    def _make_route(self, **stop_extra):
        """The P-example route in UTC: United Dairy (08:00Z, svc 30) →
        Terra Freska (09:00Z, svc 30) → Healthy Planet (window opens
        13:00Z = 09:00 EDT, svc 15)."""
        ud = self._stop(
            self.job, "pickup", 43.107, 10, service=30,
            scheduled=datetime(2026, 9, 8, 8, 0),
            loc=self.ud, hours=_hours(4.0, 18.0), **stop_extra)
        tf = self._stop(
            self.job, "pickup", 43.267, 20, service=30,
            scheduled=datetime(2026, 9, 8, 9, 0),
            loc=self.tf, hours=_hours(5.0, 18.0))
        hp = self._stop(
            self.job, "dropoff", 43.481, 30, service=15,
            scheduled=datetime(2026, 9, 8, 13, 0),
            loc=self.hp, hours=_hours(9.0, 17.0))
        return ud, tf, hp

    def _engine(self):
        from ..services.eta_engine import EtaEngine
        return EtaEngine(self.env)


class TestSectionPExampleRoute(TestEtaEngineBase):
    """The work-order test-matrix route, end to end (UTC stored, EDT
    story): leave home 07:00Z (03:00 EDT) → hub 03:30 → pretrip 10m →
    United Dairy 08:00Z (04:00 EDT) → depart 08:30Z → Terra Freska
    09:00Z (05:00 EDT) → Healthy Planet ≥13:00Z (09:00 EDT)."""

    def test_example_route_exact_times(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")

        # Leave home: route start (07:40Z) − pretrip (10) − home→hub (30).
        self.assertEqual(
            self.job.recommended_driver_leave_home_at,
            datetime(2026, 9, 8, 7, 0))
        # United Dairy: travel 20 from hub → arrive 08:00Z (= 04:00 EDT,
        # the facility's opening hour — no wait), service 30.
        self.assertEqual(ud.travel_arrival_at, datetime(2026, 9, 8, 8, 0))
        self.assertEqual(ud.facility_service_start_at, datetime(2026, 9, 8, 8, 0))
        self.assertEqual(ud.planned_departure_at, datetime(2026, 9, 8, 8, 30))
        self.assertEqual(ud.customer_eta_at, datetime(2026, 9, 8, 8, 0))
        # Terra Freska: travel 30 → arrive 09:00Z (= 05:00 EDT open).
        self.assertEqual(tf.travel_arrival_at, datetime(2026, 9, 8, 9, 0))
        self.assertEqual(tf.facility_service_start_at, datetime(2026, 9, 8, 9, 0))
        self.assertEqual(tf.planned_departure_at, datetime(2026, 9, 8, 9, 30))
        # Healthy Planet: arrival 10:10Z (06:10 EDT, from Terra's 09:30Z
        # departure + 40), window opens 09:00 EDT (13:00Z) → the truck
        # waits at the door.
        self.assertEqual(hp.travel_arrival_at, datetime(2026, 9, 8, 10, 10))
        self.assertEqual(hp.facility_service_start_at, datetime(2026, 9, 8, 13, 0))
        self.assertEqual(hp.customer_eta_at, datetime(2026, 9, 8, 13, 0))
        self.assertEqual(hp.planned_departure_at, datetime(2026, 9, 8, 13, 15))
        # Sources: planning walk, delay measured against the schedule.
        self.assertEqual(ud.eta_source, "scheduled")
        self.assertEqual(ud.eta_delay_minutes, 0.0)
        self.assertEqual(hp.eta_delay_minutes, 0.0)

    def test_depot_mode_has_no_leave_home(self):
        self.vehicle.driver_start_mode = "depot"
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertFalse(self.job.recommended_driver_leave_home_at)
        # First leg now measured from the DEPOT (x_home_base), not the hub.
        self.assertEqual(ud.travel_arrival_at, datetime(2026, 9, 8, 8, 0))

    def test_engine_never_writes_schedule(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.scheduled_time, datetime(2026, 9, 8, 8, 0))
        self.assertEqual(tf.scheduled_time, datetime(2026, 9, 8, 9, 0))
        self.assertEqual(hp.scheduled_time, datetime(2026, 9, 8, 13, 0))


class TestLiveRecalculation(TestEtaEngineBase):
    """Transitions shift downstream ETAs; the window authority absorbs
    delays when the arrival stays before opening."""

    def test_completion_shifts_downstream_but_window_wins(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")

        # United Dairy completes 30 min late (arrival 08:30Z, depart 09:00Z).
        ud.write({
            "status": "completed",
            "actual_arrival_time": datetime(2026, 9, 8, 8, 30),
            "actual_departure_time": datetime(2026, 9, 8, 9, 0),
        })
        # Write guard recomputed the job: Terra Freska shifts +30 (05:30 EDT,
        # still inside its hours → service immediately).
        self.assertEqual(ud.eta_source, "actual")
        self.assertEqual(ud.actual_service_start, datetime(2026, 9, 8, 8, 30))
        self.assertEqual(tf.facility_service_start_at, datetime(2026, 9, 8, 9, 30))
        # Healthy Planet arrival 10:10Z (06:10 EDT) < 09:00 EDT opening →
        # still 13:00Z (09:00 EDT).
        self.assertEqual(hp.facility_service_start_at, datetime(2026, 9, 8, 13, 0))

    def test_arrival_holds_eta_passes_delay(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        held = tf.facility_service_start_at
        # Driver marks en route — ETA holds, downstream unchanged.
        tf.action_mark_en_route()
        self.assertEqual(tf.facility_service_start_at, held)
        self.assertEqual(tf.eta_source, "live")

    def test_schedule_edit_recomputes_delay(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        # Dispatcher pushes United Dairy's schedule 1h later: the engine
        # does NOT re-anchor (planned_operational_start is the anchor) —
        # the delay vs the new schedule is what changes.
        ud.write({"scheduled_time": datetime(2026, 9, 8, 9, 0)})
        self.assertEqual(ud.eta_delay_minutes, -60.0)
        # And the downstream walk is unchanged by a schedule-only edit.
        self.assertEqual(tf.facility_service_start_at, datetime(2026, 9, 8, 9, 0))

    def test_override_wins_whole(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        override = datetime(2026, 9, 8, 11, 0)
        tf.write({"eta_override": override})
        # The write guard recomputed already; assert the override path.
        for field in ("travel_arrival_at", "facility_service_start_at",
                      "planned_departure_at", "customer_eta_at", "eta_live"):
            self.assertEqual(getattr(tf, field), override)
        self.assertEqual(tf.eta_source, "override")
        self.assertEqual(tf.eta_confidence, "high")

    def test_restore_clears_actuals(self):
        ud, tf, hp = self._make_route()
        ud.action_mark_arrived()
        self.assertEqual(ud.actual_service_start, ud.actual_arrival_time)
        ud.action_restore_stop()
        self.assertFalse(ud.actual_service_start)
        self.assertFalse(ud.actual_arrival_time)
        self.assertEqual(ud.status, "pending")


class TestHoursVerifiedFlag(TestEtaEngineBase):
    def test_unverified_hours_low_confidence_and_payload_flag(self):
        self.ud.hours_verified = False
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_confidence, "low")
        payload = self.job._driver_stop_dict(ud)
        self.assertTrue(payload["hours_not_verified"])
        self.assertFalse(payload["hours_verified"])

    def test_verified_hours_medium_confidence(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_confidence, "medium")
        payload = self.job._driver_stop_dict(ud)
        self.assertTrue(payload["hours_verified"])
        self.assertFalse(payload["hours_not_verified"])

    def test_exact_appointment_verified_is_high(self):
        ud, tf, hp = self._make_route()
        ud.write({
            "time_window_type": "exact",
            "exact_time": datetime(2026, 9, 8, 8, 0),
        })
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_confidence, "high")


class TestPlannerAndTrackingFixes(TestEtaEngineBase):
    def test_open_24h_no_crash(self):
        """The hour=24 ValueError: an open_24h snapshot must not break
        the backward recommendation."""
        ud, tf, hp = self._make_route()
        ud.operating_hours_snapshot = _hours(0.0, 24.0)
        result = self.job._recommended_operational_start()
        self.assertTrue(result)

    def test_tracking_eta_has_timezone_offset(self):
        """Customer-facing ETAs carry an explicit offset — never raw
        naive UTC (the pre-fix bug)."""
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        iso = self.env["logistics.booking"]._eta_iso_local(
            ud.customer_eta_at, ud)
        # 08:00Z on Sep 8 (EDT) = 04:00-04:00 — the offset is present.
        self.assertTrue(iso.endswith("-04:00"), f"missing offset: {iso}")

    def test_driver_payload_carries_eta_values(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        payload = self.job._driver_stop_dict(ud)
        self.assertEqual(payload["travel_arrival_at"], "2026-09-08T08:00:00Z")
        self.assertEqual(payload["facility_service_start_at"], "2026-09-08T08:00:00Z")
        self.assertEqual(payload["planned_departure_at"], "2026-09-08T08:30:00Z")
        self.assertEqual(payload["customer_eta_at"], "2026-09-08T08:00:00Z")
        self.assertEqual(payload["eta_source"], "scheduled")
        self.assertEqual(payload["eta_confidence"], "medium")
        self.assertEqual(payload["recommended_driver_leave_home_at"],
                         "2026-09-08T07:00:00Z")

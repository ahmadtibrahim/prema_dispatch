"""§15–17 targeted tests — ETA provisional/wait taxonomy, service-time
learning, driver start/hub recommendation (work order Sections 15–17).

Reuses the Section P fixture from test_phase2_eta_engine (United Dairy →
Terra Freska → Healthy Planet, UTC stored / EDT story):
  leave home 07:00Z (03:00 EDT) → hub 03:30 → pretrip 10m →
  United Dairy 08:00Z (04:00 EDT) → depart 08:30Z →
  Terra Freska 09:00Z (05:00 EDT) → Healthy Planet window 13:00Z
  (09:00 EDT) — the truck waits 170 min at the door.

Every test rolls back — nothing commits.
"""

from datetime import datetime

from odoo.tests.common import TransactionCase

from .test_phase2_eta_engine import TestEtaEngineBase, _hours


class TestEtaWaitAndProvisional(TestEtaEngineBase):
    """§15: the wait at the door and the opening it waits for are kept
    separate from service and never promised as arrival; hours unverified
    → the 'HOURS NOT VERIFIED — ETA PROVISIONAL' grade."""

    def test_early_arrival_waits_for_opening(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")

        # Physical arrival 10:10Z (06:10 EDT) — Healthy Planet opens
        # 09:00 EDT (13:00Z): service start is the OPENING, never the
        # arrival; the wait is explicit and the opening is named.
        self.assertEqual(hp.travel_arrival_at, datetime(2026, 9, 8, 10, 10))
        self.assertEqual(hp.customer_eta_at, datetime(2026, 9, 8, 13, 0))
        self.assertEqual(hp.facility_service_start_at,
                         datetime(2026, 9, 8, 13, 0))
        self.assertEqual(hp.waiting_minutes, 170.0)
        self.assertEqual(hp.facility_opening_at, datetime(2026, 9, 8, 13, 0))
        self.assertEqual(hp.eta_source, "facility_adjusted")
        self.assertEqual(hp.eta_confidence, "medium")

    def test_no_wait_has_no_opening_and_plain_source(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        # United Dairy: arrival 08:00Z == its opening hour — no wait.
        self.assertEqual(ud.waiting_minutes, 0.0)
        self.assertFalse(ud.facility_opening_at)
        self.assertEqual(ud.eta_source, "scheduled")

    def test_hours_unverified_provisional(self):
        ud, tf, hp = self._make_route()
        ud.saved_location_id.hours_verified = False
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_source, "provisional")
        self.assertEqual(ud.eta_confidence, "low")
        # Healthy Planet (hours still verified) keeps its adjusted grade.
        self.assertEqual(hp.eta_source, "facility_adjusted")
        # Verifying the hours clears the provisional grade.
        ud.saved_location_id.hours_verified = True
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_source, "scheduled")

    def test_driver_stop_dict_carries_wait_and_hours_flags(self):
        ud, tf, hp = self._make_route()
        hp.saved_location_id.hours_verified = False
        self._engine().compute_job_eta(self.job, source="scheduled")
        d = self.job._driver_stop_dict(hp)
        self.assertIn("waiting_minutes", d)
        self.assertIn("facility_opening_at", d)
        self.assertEqual(d["waiting_minutes"], 170.0)
        # Payload timestamps are explicit-UTC ISO strings (Z suffix).
        self.assertEqual(d["facility_opening_at"], "2026-09-08T13:00:00Z")
        self.assertFalse(d["hours_verified"])
        self.assertTrue(d["hours_not_verified"])

    def test_override_clears_wait_taxonomy(self):
        ud, tf, hp = self._make_route()
        hp.eta_override = datetime(2026, 9, 8, 12, 30)
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(hp.eta_source, "override")
        self.assertEqual(hp.eta_confidence, "high")
        self.assertEqual(hp.waiting_minutes, 0.0)
        self.assertFalse(hp.facility_opening_at)

    def test_workday_start_recomputes_live(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        self.assertEqual(ud.eta_source, "scheduled")
        day = self.env["prema.dispatch.driver.workday"]._get_or_create_for(
            self.partner.id, datetime(2026, 9, 8).date())
        day.action_start_work()
        ud.invalidate_recordset()
        self.assertEqual(ud.eta_source, "live")


class TestServiceTimeLearning(TestEtaEngineBase):
    """§16: per-type history, per-facility defaults, the explicit
    auto-learning gate, the appointment/check-in buffer, per-pallet
    minutes, and the departure−service-start sample rule."""

    def _samples_loc(self, **extra):
        vals = {
            "name": "DC Samples", "address": "9 DC Rd",
            "pin_lat": 43.300, "pin_lng": -79.0, "pin_set": True,
            "hours_verified": True, "use_count": 6,
        }
        vals.update(extra)
        return self.env["prema.dispatch.location"].create(vals)

    def _sample(self, loc, stop_type, service):
        return self.env["prema.dispatch.location.visit.sample"].create({
            "location_id": loc.id,
            "visited_at": datetime(2026, 9, 8, 8, 0),
            "stop_type": stop_type,
            "dwell_minutes": service,
            "service_minutes": service,
            "wait_minutes": 0.0,
        })

    def test_service_time_per_type_history(self):
        loc = self._samples_loc()
        for _ in range(5):
            self._sample(loc, "pickup", 20.0)
        for _ in range(5):
            self._sample(loc, "dropoff", 60.0)
        loc._recompute_sample_stats()
        # A distribution centre unloads far longer than it loads.
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 20)
        self.assertEqual(loc.planning_service_time_minutes("delivery"), 60)

    def test_auto_learn_gate(self):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("prema_dispatch.service_time_auto_learn", "0")
        loc = self._samples_loc(default_delivery_service_minutes=45)
        for _ in range(5):
            self._sample(loc, "pickup", 20.0)
        loc._recompute_sample_stats()
        # History ignored while the explicit setting is off…
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 15)
        # …the per-facility delivery default still applies…
        self.assertEqual(loc.planning_service_time_minutes("delivery"), 45)
        # …and the manual override ALWAYS wins.
        loc.manual_service_time_minutes = 33
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 33)
        # Back on: history returns (manual override cleared first — it
        # always wins while set).
        loc.manual_service_time_minutes = 0
        param.set_param("prema_dispatch.service_time_auto_learn", "1")
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 20)

    def test_per_facility_type_defaults_without_history(self):
        loc = self._samples_loc(use_count=0,
                                default_pickup_service_minutes=25,
                                default_delivery_service_minutes=45)
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 25)
        self.assertEqual(loc.planning_service_time_minutes("delivery"), 45)
        # No stop type → class default / 15 baseline.
        self.assertEqual(loc.planning_service_time_minutes(), 15)

    def test_appointment_buffer_and_per_pallet_in_engine(self):
        ud, tf, hp = self._make_route()
        ud.saved_location_id.write({
            "appointment_buffer_minutes": 15,
            "per_pallet_service_minutes": 2.0,
        })
        # No stop-level service time → the facility authority runs:
        # 15 (planning default) + 10 pallets × 2 per-pallet = 35.
        ud.write({"service_time_minutes": 0, "pallets_in": 10})
        self._engine().compute_job_eta(self.job, source="scheduled")
        # Arrival 08:00Z → check-in buffer 15 → service start 08:15Z;
        # service = 15 base + 10 pallets × 2 = 35 → depart 08:50Z.
        self.assertEqual(ud.travel_arrival_at, datetime(2026, 9, 8, 8, 0))
        self.assertEqual(ud.facility_service_start_at,
                         datetime(2026, 9, 8, 8, 15))
        self.assertEqual(ud.waiting_minutes, 15.0)
        self.assertEqual(ud.planned_departure_at,
                         datetime(2026, 9, 8, 8, 50))

    def test_sample_uses_service_start_not_arrival(self):
        loc = self._samples_loc()
        stop = self._stop(self.job, "pickup", 43.107, 10, service=15,
                          scheduled=datetime(2026, 9, 8, 8, 0), loc=loc,
                          hours=_hours(4.0, 18.0))
        # Driver waited 30 min after arriving (08:00Z) before serving
        # (08:30Z); service ended 09:00Z. Dwell = 60, SERVICE = 30.
        stop.write({
            "status": "completed",
            "actual_arrival_time": datetime(2026, 9, 8, 8, 0),
            "actual_service_start": datetime(2026, 9, 8, 8, 30),
            "actual_departure_time": datetime(2026, 9, 8, 9, 0),
        })
        loc.record_visit_stats(stop)
        self.assertEqual(loc.avg_loading_minutes, 30.0)
        self.assertGreaterEqual(loc.service_sample_count, 1)
        self.assertTrue(loc.last_service_time_calculated_at)
        # The learned loading average feeds future pickup ETAs.
        self.assertEqual(loc.planning_service_time_minutes("pickup"), 30)

    def test_corrupt_samples_rejected(self):
        loc = self._samples_loc()
        self.env["prema.dispatch.location.visit.sample"].create({
            "location_id": loc.id,
            "visited_at": datetime(2026, 9, 8, 8, 0),
            "stop_type": "pickup",
            "dwell_minutes": 1000.0,
            "service_minutes": 1000.0,
            "wait_minutes": 0.0,
        })
        loc._recompute_sample_stats()
        # 1000-minute service is corrupt — counted nowhere.
        self.assertEqual(loc.service_sample_count, 0)
        self.assertFalse(loc.avg_loading_minutes)


class TestDriverStartPlan(TestEtaEngineBase):
    """§17: the backward chain — leave home → arrive hub → [dwell] →
    pre-trip → [buffer] → depart hub → travel → first service start.
    Depart hub = first service start − travel to the first stop."""

    def test_start_plan_driver_home_exact_chain(self):
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        self.assertEqual(plan["mode"], "driver_home")
        # The P-example chain, verbatim: 03:00 / 03:30 / 03:30–03:40 /
        # 03:40 / 04:00 (EDT) — i.e. 07:00Z / 07:30Z / 07:30–07:40Z /
        # 07:40Z / 08:00Z, with United Dairy ETA 04:00 and Terra Freska
        # service 05:00 (09:00Z).
        self.assertEqual(plan["leave_home_at"], datetime(2026, 9, 8, 7, 0))
        self.assertEqual(plan["arrive_hub_at"], datetime(2026, 9, 8, 7, 30))
        self.assertEqual(plan["pretrip_start"], datetime(2026, 9, 8, 7, 30))
        self.assertEqual(plan["pretrip_end"], datetime(2026, 9, 8, 7, 40))
        self.assertEqual(plan["depart_hub_at"], datetime(2026, 9, 8, 7, 40))
        self.assertEqual(plan["first_eta_at"], datetime(2026, 9, 8, 8, 0))
        self.assertEqual(plan["next_eta_at"], datetime(2026, 9, 8, 9, 0))

    def test_start_plan_matches_walk_leave_home(self):
        """ONE chain: the walk's recommended_driver_leave_home_at and the
        plan's leave_home_at agree."""
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        self.assertEqual(self.job.recommended_driver_leave_home_at,
                         plan["leave_home_at"])

    def test_start_plan_hub_mode(self):
        self.vehicle.driver_start_mode = "hub"
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        self.assertEqual(plan["mode"], "hub")
        self.assertEqual(plan["arrive_hub_at"], datetime(2026, 9, 8, 7, 30))
        self.assertEqual(plan["depart_hub_at"], datetime(2026, 9, 8, 7, 40))
        self.assertFalse(plan["leave_home_at"])

    def test_start_plan_custom_mode(self):
        self.vehicle.driver_start_mode = "custom"
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        # Dispatcher-owned start: depart AT the first service start, no
        # backward pre-steps — but the binding ETAs still display.
        self.assertEqual(plan["depart_hub_at"], datetime(2026, 9, 8, 8, 0))
        self.assertEqual(plan["first_eta_at"], datetime(2026, 9, 8, 8, 0))
        self.assertEqual(plan["next_eta_at"], datetime(2026, 9, 8, 9, 0))
        self.assertFalse(plan["leave_home_at"])
        self.assertFalse(plan["pretrip_start"])

    def test_start_plan_prior_day_end(self):
        self.vehicle.driver_start_mode = "prior_day_end"
        # The truck ended yesterday at the hub coordinates.
        prior = self.env["prema.dispatch.job"].create({
            "name": "JOB-P2-PRIOR",
            "partner_id": self.partner.id,
            "vehicle_id": self.vehicle.id,
            "scheduled_pickup": datetime(2026, 9, 7, 7, 0),
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": prior.id,
            "stop_type": "dropoff",
            "address": "Prior Rd",
            "latitude": 43.0,
            "longitude": -79.0,
            "sequence": 10,
            "status": "completed",
            "actual_arrival_time": datetime(2026, 9, 7, 13, 0),
            "actual_departure_time": datetime(2026, 9, 7, 13, 15),
        })
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        # Travel 20 from yesterday's end → depart 07:40Z, pre-trip before.
        self.assertEqual(plan["depart_hub_at"], datetime(2026, 9, 8, 7, 40))
        self.assertEqual(plan["pretrip_start"], datetime(2026, 9, 8, 7, 30))
        self.assertFalse(plan["leave_home_at"])

    def test_start_plan_no_vehicle_fallback(self):
        ud, tf, hp = self._make_route()
        self.job.vehicle_id = False
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        self.assertEqual(plan["mode"], "depot")
        # No vehicle → depot defaults everywhere, no crash, no home steps.
        # The walk anchored on the dispatcher's operational start (07:40Z
        # depart) and pushed the first service start out by the default
        # depot leg (135 min) — the plan must depart EXACTLY when the
        # walk started (the depart↔anchor invariant).
        self.assertEqual(plan["depart_hub_at"],
                         self.job.planned_operational_start)
        self.assertEqual(plan["first_eta_at"], ud.customer_eta_at)
        self.assertFalse(plan["leave_home_at"])

    def test_start_plan_dwell_and_buffer(self):
        self.vehicle.write({
            "driver_hub_dwell_minutes": 15,
            "driver_departure_buffer_minutes": 5,
        })
        ud, tf, hp = self._make_route()
        self._engine().compute_job_eta(self.job, source="scheduled")
        plan = self._engine().driver_start_plan(self.job)
        # Depart unchanged (service − travel); pre-trip ends 5 min before
        # depart; arrive hub 15 min before pre-trip; leave home 30 earlier.
        self.assertEqual(plan["depart_hub_at"], datetime(2026, 9, 8, 7, 40))
        self.assertEqual(plan["pretrip_end"], datetime(2026, 9, 8, 7, 35))
        self.assertEqual(plan["pretrip_start"], datetime(2026, 9, 8, 7, 25))
        self.assertEqual(plan["arrive_hub_at"], datetime(2026, 9, 8, 7, 10))
        self.assertEqual(plan["leave_home_at"], datetime(2026, 9, 8, 6, 40))

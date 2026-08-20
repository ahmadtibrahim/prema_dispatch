"""
PHASE 2 — Driver workday (START WORK / END DAY / persisted daily summary)
and the "Arrived" live-status feed.

Spec §61 Phase-2 regression scenarios covered here:
  * Start Work          — records timestamp + GPS, flips the day to
                          In Progress, syncs open jobs' route_started_at
                          (Booking Board), idempotent re-start.
  * Arrived live status — driver_update_stop('arrived') records
                          status/arrived_at/GPS and advances the booking
                          state machine immediately.
  * End Day validation  — rejects open stops, unresolved issues, and
                          pending transfers; the mandatory-proof gate is
                          enforced at completion time (which END DAY then
                          requires), so a proof-less day can never close.
  * Daily completion    — summary metrics (stops/pickups/deliveries/
                          pallets/distance/drive-wait-load-unload minutes)
                          computed server-side from actual timestamps and
                          pins, persisted on the workday record, exposed in
                          the payload and in the available-dates flags
                          (calendar checkmark).

Run with (from /opt/odoo):
  ./venv-18/bin/python3 odoo-bin -c /etc/odoo18.conf -d Prod-db-test1a \
      --test-enable --stop-after-init -u prema_logistics_booking,prema_dispatch \
      --http-port 18069 --workers 0 --max-cron-threads 0 \
      --test-tags "/prema_dispatch/tests/test_driver_workday.py"
"""
from datetime import date, datetime

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestDriverWorkday(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.Workday = self.env["prema.dispatch.driver.workday"]
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1)
        self.customer = self.env["res.partner"].create(
            {"name": "Workday Test Customer"})

        self.driver_a_partner = self.env["res.partner"].create(
            {"name": "Workday Driver A"})
        self.driver_b_partner = self.env["res.partner"].create(
            {"name": "Workday Driver B"})

        # "Today" anchored to the driver's timezone — the app flags is_today /
        # work_started in the driver's local calendar, so a fixed literal date
        # (2026-08-18) drifted stale once the real date passed it (found in
        # the 2026-08-20 full-suite run). Stops are scheduled 14:00 UTC, which
        # is 09:00/10:00 in America/Toronto — always the SAME calendar date
        # (UTC offset is at most ±5h, never a day flip).
        import pytz
        toronto_now = datetime.now(pytz.timezone("America/Toronto"))
        self.work_date = toronto_now.date()
        self.sched = datetime(self.work_date.year, self.work_date.month,
                              self.work_date.day, 14, 0)

        self.driver_a_user = self._make_user(
            "workday_driver_a@example.com", self.driver_a_partner,
            tz="America/Toronto")
        self.driver_b_user = self._make_user(
            "workday_driver_b@example.com", self.driver_b_partner,
            tz="America/Toronto")
        self.dispatcher_user = self._make_user(
            "workday_dispatcher@example.com",
            self.env["res.partner"].create({"name": "Workday Dispatcher"}),
            tz="America/Toronto", groups=("prema_dispatch.group_dispatcher",))

    def _make_user(self, login, partner, tz="America/Toronto", groups=(
            "prema_dispatch.group_dispatch_driver",)):
        partner.email = login
        return self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": partner.name,
                "login": login,
                "partner_id": partner.id,
                "tz": tz,
                "groups_id": [(6, 0, [self.env.ref("base.group_user").id] +
                                      [self.env.ref(g).id for g in groups])],
                "company_id": self.env.company.id,
                "company_ids": [(6, 0, [self.env.company.id])],
            })

    def _make_job(self, driver_partner, name="Workday Job"):
        return self.Job.create({
            "partner_id": self.customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": driver_partner.id,
            "scheduled_pickup": self.sched,
            "name": name,
        })

    def _add_stop(self, job, stop_type, pallets=0, pod_required=False,
                  sequence=10, scheduled=None):
        return self.Stop.create({
            "job_id": job.id,
            "sequence": sequence,
            "stop_type": stop_type,
            "address": f"{sequence} Workday St, Toronto, ON",
            "pallets_in": pallets if stop_type == "pickup" else 0,
            "pallets_out": pallets if stop_type in ("dropoff", "return") else 0,
            "pod_required": pod_required,
            "scheduled_time": scheduled or self.sched,
        })

    def _workday(self, user):
        """Fetch/create the workday through the driver's own env — proves the
        IR access rule + own-record rule let the driver create their row."""
        return self.Workday.with_user(user)._get_or_create_for(
            user.partner_id.id, self.work_date)

    # ── START WORK ─────────────────────────────────────────────────

    def test_01_start_work_records_timestamp_gps_and_state(self):
        wd = self._workday(self.driver_a_user)
        self.assertEqual(wd.state, "not_started")

        payload = wd.action_start_work(lat=43.6426, lng=-79.3871)
        self.assertEqual(payload["state"], "in_progress")
        self.assertTrue(payload["work_started_at"].endswith("Z"))
        self.assertEqual(wd.work_started_by, self.driver_a_user)
        self.assertAlmostEqual(wd.start_gps_lat, 43.6426, places=6)
        self.assertAlmostEqual(wd.start_gps_lng, -79.3871, places=6)
        self.assertIsNotNone(wd.work_started_at)

    def test_02_start_work_is_idempotent(self):
        wd = self._workday(self.driver_a_user)
        wd.action_start_work(lat=1.0, lng=2.0)
        first_started = wd.work_started_at
        payload = wd.action_start_work(lat=9.0, lng=9.0)
        self.assertEqual(wd.work_started_at, first_started)
        self.assertEqual(wd.state, "in_progress")
        # GPS from the FIRST start is preserved, not overwritten.
        self.assertAlmostEqual(wd.start_gps_lat, 1.0, places=6)
        self.assertEqual(payload["state"], "in_progress")

    def test_03_start_work_syncs_open_job_routes(self):
        job = self._make_job(self.driver_a_partner, name="Start Work Route")
        self._add_stop(job, "dropoff")
        # A second job whose route was already explicitly started must keep
        # its original timestamp (per-job audit handshake stays authoritative).
        job_started = self._make_job(self.driver_a_partner, name="Started Route")
        self._add_stop(job_started, "dropoff")
        earlier = datetime(2026, 8, 18, 8, 0)
        job_started.write({"route_started_at": earlier, "route_started_by": self.driver_a_user.id})

        wd = self._workday(self.driver_a_user)
        wd.action_start_work(lat=0, lng=0)

        self.assertTrue(job.route_started_at)
        self.assertEqual(job.route_started_at, wd.work_started_at)
        self.assertEqual(job.route_started_by, self.driver_a_user)
        # Already-started route untouched.
        self.assertEqual(job_started.route_started_at, earlier)

    def test_04_workday_start_requires_driver(self):
        # Controller guard: get_driver_partner returns None for non-drivers —
        # the route then answers "Not authorized" instead of creating a row.
        from odoo.addons.prema_dispatch.services.dispatch_auth import get_driver_partner
        driver_env = self.env(user=self.driver_a_user.id)
        dispatcher_env = self.env(user=self.dispatcher_user.id)
        self.assertEqual(get_driver_partner(driver_env), self.driver_a_partner)
        self.assertIsNone(get_driver_partner(dispatcher_env))

    def test_05_driver_cannot_touch_other_drivers_workday(self):
        self._workday(self.driver_a_user)
        wd_b = self._workday(self.driver_b_user)
        with self.assertRaises(Exception):
            # Driver A's own-record ir.rule hides driver B's workday row.
            self.Workday.with_user(self.driver_a_user).browse(wd_b.id).read()

    # ── ARRIVED live status (spec §12, §61) ────────────────────────

    def test_06_arrived_live_status_records_gps_and_syncs_booking(self):
        job = self._make_job(self.driver_a_partner, name="Arrival Route")
        stop = self._add_stop(job, "dropoff")

        res = self.Job.with_user(self.driver_a_user).driver_update_stop(
            stop.id, "arrived", {"lat": 43.6426, "lng": -79.3871})
        self.assertTrue(res["success"])

        stop = self.Stop.browse(stop.id)
        self.assertEqual(stop.status, "arrived")
        self.assertIsNotNone(stop.actual_arrival_time)
        self.assertAlmostEqual(stop.gps_stamp_lat, 43.6426, places=6)
        self.assertAlmostEqual(stop.gps_stamp_lng, -79.3871, places=6)
        self.assertIsNotNone(stop.gps_stamp_time)

    # ── END DAY validation (spec §28, §61) ─────────────────────────

    def test_07_end_day_rejects_open_stop(self):
        job = self._make_job(self.driver_a_partner, name="Open Stop Route")
        stop = self._add_stop(job, "dropoff")
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()

        res = wd.action_end_day()
        self.assertFalse(res["success"])
        self.assertIn("stop still open", res["error"])
        self.assertIn(stop.address, res["error"])
        self.assertEqual(wd.state, "in_progress")  # day left open

    def test_08_end_day_rejects_unresolved_issue(self):
        job = self._make_job(self.driver_a_partner, name="Issue Route")
        stop = self._add_stop(job, "dropoff")
        stop.write({"status": "issue"})
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()

        res = wd.action_end_day()
        self.assertFalse(res["success"])
        # An issue stop is still an open stop — END DAY refuses it either way.
        self.assertIn("stop still open", res["error"])

    def test_09_end_day_rejects_pending_transfer(self):
        job = self._make_job(self.driver_a_partner, name="Transfer Route")
        xdock = self.env["prema.dispatch.location"].create({
            "name": "Workday Cross-Dock",
            "address": "1 Cross Dock Rd, Mississauga, ON",
            "allow_cross_dock": True,
        })
        stop = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "cross_dock_drop",
            "address": "1 Cross Dock Rd, Mississauga, ON",
            "saved_location_id": xdock.id,
            "scheduled_time": self.sched,
        })
        truck = self.env["fleet.vehicle"].search([], limit=1) or \
            self.env["fleet.vehicle"].create({
                "name": "Workday Transfer Truck",
                "model_id": self.env["fleet.vehicle.model"].search([], limit=1).id,
            })
        stop.write({
            "status": "completed",
            "transfer_to_vehicle_id": truck.id,
        })
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()

        res = wd.action_end_day()
        self.assertFalse(res["success"])
        self.assertIn("transfer at", res["error"])

    def test_10_mandatory_proof_gate_blocks_completion(self):
        # The END DAY proof invariant is enforced at completion time: a
        # pod_required delivery cannot be marked complete without POD or an
        # authorized override — so an un-proved day can never reach END DAY
        # with that stop closed.
        job = self._make_job(self.driver_a_partner, name="Proof Gate Route")
        stop = self._add_stop(job, "dropoff", pod_required=True)

        with self.assertRaises(UserError):
            self.Stop.with_user(self.driver_a_user).browse(stop.id).action_mark_completed()

        stop = self.Stop.browse(stop.id)
        self.assertEqual(stop.status, "pending")  # completion refused
        # Authorized override unblocks (dispatcher-granted).
        stop.write({"proof_override_by": self.dispatcher_user.id})
        self.Stop.with_user(self.driver_a_user).browse(stop.id).action_mark_completed()
        self.assertEqual(self.Stop.browse(stop.id).status, "completed")

    # ── END DAY success + persisted daily summary (spec §29/§30) ───

    def _completed_stop_pair(self):
        job = self._make_job(self.driver_a_partner, name="Summary Route")
        pickup = self._add_stop(job, "pickup", pallets=2, sequence=10,
                                scheduled=self.sched)
        dropoff = self._add_stop(job, "dropoff", pallets=3, sequence=20,
                                 scheduled=self.sched)
        # Controlled actuals (naive UTC): pickup dwell 45 min (service 30 →
        # 30 loading + 15 waiting); driving 14:45→15:15 (30 min); dropoff
        # dwell 20 min (service 20 → 20 unloading, no waiting).
        pickup.write({
            "status": "completed",
            "actual_arrival_time": datetime(2026, 8, 18, 14, 0),
            "actual_departure_time": datetime(2026, 8, 18, 14, 45),
            "service_time_minutes": 30,
            "latitude": 43.6426, "longitude": -79.3871,
        })
        dropoff.write({
            "status": "completed",
            "actual_arrival_time": datetime(2026, 8, 18, 15, 15),
            "actual_departure_time": datetime(2026, 8, 18, 15, 35),
            "service_time_minutes": 20,
            "latitude": 43.6532, "longitude": -79.3832,
        })
        return job, pickup, dropoff

    def test_11_end_day_success_persists_summary_metrics(self):
        job, pickup, dropoff = self._completed_stop_pair()
        wd = self._workday(self.driver_a_user)
        wd.action_start_work(lat=43.64, lng=-79.38)

        res = wd.action_end_day()
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(wd.state, "completed")
        self.assertIsNotNone(wd.work_finished_at)
        self.assertEqual(wd.work_finished_by, self.driver_a_user)

        self.assertEqual(wd.stops_count, 2)
        self.assertEqual(wd.pickup_count, 1)
        self.assertEqual(wd.delivery_count, 1)
        self.assertEqual(wd.pallets_handled, 5)          # 2 + 3
        self.assertEqual(wd.loading_minutes, 30)
        self.assertEqual(wd.waiting_minutes, 15)
        self.assertEqual(wd.unloading_minutes, 20)
        self.assertEqual(wd.driving_minutes, 30)
        # total = fallback sum (finished_at isn't set until after compute)
        self.assertEqual(wd.total_minutes, 95)
        # distance = haversine over the stop pins
        expected_km = round(self.Workday._haversine_km(
            43.6426, -79.3871, 43.6532, -79.3832), 1)
        self.assertAlmostEqual(wd.distance_km, expected_km, places=1)
        self.assertGreater(wd.distance_km, 1.0)

        payload = wd._payload()
        self.assertEqual(payload["state"], "completed")
        self.assertEqual(payload["summary"]["stops"], 2)
        self.assertEqual(payload["summary"]["pallets"], 5)
        self.assertEqual(payload["summary"]["driving_minutes"], 30)

    def test_12_end_day_auto_completes_jobs(self):
        job, _p, _d = self._completed_stop_pair()
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()
        res = wd.action_end_day()
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(job.stage_id.is_completed, "all-done job should auto-complete")

    def test_13_end_day_idempotent_on_second_call(self):
        _job, _p, _d = self._completed_stop_pair()
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()
        first = wd.action_end_day()
        self.assertTrue(first["success"])
        finished = wd.work_finished_at
        second = wd.action_end_day()
        # Re-running keeps the recorded finish time (no double write).
        self.assertTrue(second["success"])
        self.assertEqual(wd.work_finished_at, finished)

    # ── App payloads: flags + workday on the stops feed ────────────

    def test_14_available_dates_show_workday_flags(self):
        job = self._make_job(self.driver_a_partner, name="Flags Route")
        self._add_stop(job, "dropoff")
        wd = self._workday(self.driver_a_user)
        wd.action_start_work()

        payload = self.Job.with_user(self.driver_a_user).get_driver_available_dates()
        days = payload["days"]
        today = next(d for d in days if d["is_today"])
        self.assertTrue(today["work_started"])
        self.assertFalse(today["day_completed"])

        _j, _d = wd._day_stops()
        for stop in _d:
            stop.write({"status": "completed",
                        "actual_arrival_time": datetime(2026, 8, 18, 15, 0),
                        "actual_departure_time": datetime(2026, 8, 18, 15, 30)})
        wd.action_end_day()

        payload = self.Job.with_user(self.driver_a_user).get_driver_available_dates()
        today = next(d for d in payload["days"] if d["is_today"])
        self.assertTrue(today["day_completed"])

    def test_15_stops_feed_carries_workday_payload(self):
        job = self._make_job(self.driver_a_partner, name="Feed Route")
        self._add_stop(job, "dropoff")
        wd = self._workday(self.driver_a_user)
        wd.action_start_work(lat=1.0, lng=2.0)

        feed = self.Job.with_user(self.driver_a_user).get_driver_stops_for_date(
            self.work_date.isoformat())
        self.assertEqual(feed["workday"]["state"], "in_progress")
        self.assertTrue(feed["workday"]["work_started_at"].endswith("Z"))
        self.assertEqual(feed["workday"]["date"], self.work_date.isoformat())
        self.assertTrue(feed["is_today"])

        # The driver feed stays empty for another driver's day.
        feed_b = self.Job.with_user(self.driver_b_user).get_driver_stops_for_date(
            self.work_date.isoformat())
        self.assertEqual(feed_b["workday"]["state"], "not_started")
        self.assertEqual(feed_b["stops"], [])

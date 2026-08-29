# -*- coding: utf-8 -*-
"""Phase 9-10 targeted tests — 18-section work order Sections 9-10.

§9 Driver Updates panel → chronological operational feed: every event
names its job, driver, truck, stop, physical visit, customer and
evidence; feed rows are observability-only (never raise, never roll back
a driver action, never pollute the actionable alert list).

§10 Booking Board progress: the job form Progress page data — booking
summary, per-stop/per-pallet progress, timeline identifiers, and the
hard rule that a shared physical visit NEVER combines evidence into an
ambiguous shared bucket.

Run: --test-tags /prema_logistics_booking/tests/test_phase9_10
"""
import base64
import datetime
import logging

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase

from ..services.temperature_engine import TemperatureEngine

_logger = logging.getLogger(__name__)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestPhase9_10(TransactionCase):
    """§9 feed emission + §10 progress identifiers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P9 Customer"})
        cls.partner2 = env["res.partner"].create({"name": "P9 Second Customer"})
        cls.driver = env["res.partner"].create({"name": "P9 Driver"})
        # Driver user with UTC tz so workday date math is deterministic.
        env["res.users"].create({
            "name": "p9driver", "login": "p9driver@test.local",
            "partner_id": cls.driver.id, "tz": "UTC",
            "groups_id": [(6, 0, [env.ref("base.group_user").id])],
        })
        brand = env["fleet.vehicle.model.brand"].create({"name": "P9 Brand"})
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P9 Truck Model", "brand_id": brand.id})
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P9-TRUCK-01", "license_plate": "P9-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id,
            "x_reefer": True,
        })
        cls.layout = env["prema.dispatch.vehicle.layout.template"].create({
            "name": "P9 Layout", "layout_type": "straight",
            "is_verified": True,
            "position_ids": [
                (0, 0, {"position_code": c, "sequence": i * 10})
                for i, c in enumerate(("A1", "A2", "A3"), start=1)
            ],
        })

    # ── fixtures ─────────────────────────────────────────────────────

    @classmethod
    def _location(cls, name, pin_lat=43.63, pin_lng=-79.46):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"789 P9 Ave #{cls._loc_n}, Ontario",
            "pin_lat": pin_lat, "pin_lng": pin_lng,
        })

    @classmethod
    def _booking(cls, pallets=1, partner=None, pickup_loc=None,
                 delivery_loc=None, **extra):
        env = cls.env
        partner = partner or cls.partner
        vals = {
            "partner_id": partner.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "dry",
            "pallets": pallets, "physical_pallets": pallets,
            "weight_lbs": 1200.0 * pallets,
            "pickup_date": datetime.date(2026, 9, 1),
            "estimated_delivery_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "P9 test",
                "_pallet_allocs": [
                    {"pallet": p, "stops": [1], "shared": False}
                    for p in range(1, pallets + 1)
                ],
            }],
        }
        vals.update(extra)
        booking = env["logistics.booking"].create(vals)
        env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": (pickup_loc or cls._location("P9 Pickup")).id,
             "city": "Pickup City", "pallet_count": pallets},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": (delivery_loc or cls._location("P9 Delivery")).id,
             "city": "Delivery City", "pallet_count": pallets},
        ])
        return booking

    @classmethod
    def _job(cls, booking, vehicle=True, driver=True, depart=True,
             status="loaded", **extra):
        job = booking._create_dispatch_job()
        if vehicle:
            job.vehicle_id = cls.vehicle.id
        if driver:
            job.driver_id = cls.driver.id
        if depart:
            job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup").write({
                    "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 0)})
        # The job build flags proof as required by stop type (pickup → POP,
        # delivery → POD); these tests exercise the FEED + PROGRESS layers,
        # so drop the gate and let completion proceed without attached proof.
        job.stop_ids.write({"pop_required": False, "pod_required": False})
        if status:
            job.item_ids.write({"status": status})
        if extra:
            job.write(extra)
        return job

    def _png(self, tag):
        return base64.b64encode(PNG_1PX + tag.encode()).decode("ascii")

    def _feed(self, event_type, job=None, **domain_extra):
        domain = [("update_type", "=", event_type)]
        if job is not None:
            domain.append(("job_id", "=", job.id))
        for k, v in domain_extra.items():
            domain.append((k, "=", v))
        return self.env["prema.dispatch.driver.update"].search(
            domain, order="id desc")

    def _complete_all_stops(self, job):
        for stop in job.stop_ids.filtered(
                lambda s: not s.planning_only and s.status not in ("completed",)):
            stop.action_mark_completed()

    # ── §9: workday start / end ──────────────────────────────────────

    def test_a_workday_start_end_feed(self):
        booking = self._booking()
        job = self._job(booking)
        Workday = self.env["prema.dispatch.driver.workday"]
        wd = Workday.create({
            "driver_id": self.driver.id,
            "work_date": datetime.date(2026, 9, 1),
        })
        res = wd.action_start_work(lat=43.63, lng=-79.46)
        self.assertEqual(res["state"], "in_progress", res)
        start = self._feed("workday_start", job=job)
        self.assertEqual(len(start), 1)
        row = start[0]
        self.assertEqual(row.status, "resolved")
        self.assertFalse(row.is_alert)
        self.assertEqual(row.driver_id.id, self.driver.id)
        self.assertEqual(row.vehicle_id.id, self.vehicle.id)
        self.assertEqual(row.customer_id.id, self.partner.id)
        self.assertEqual(row.job_id.id, job.id)
        self.assertIn("started work", row.message)

        self._complete_all_stops(job)
        res = wd.action_end_day()
        self.assertTrue(res["success"], res)
        end = self._feed("workday_ended", job=job)
        self.assertEqual(len(end), 1)
        self.assertIn("Workday ended", end[0].message)

    # ── §9: stop action feeds ────────────────────────────────────────

    def test_b_stop_action_feeds(self):
        booking = self._booking()
        job = self._job(booking)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        # Dispatch stops are bridged as pickup/dropoff (logistics_booking.py
        # maps delivery → dropoff) — never filter dispatch stops by
        # "delivery".
        delivery = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")[:1]

        # Arrived → stop_arrival (+ pickup_start for a pickup stop).
        res = job.driver_update_stop(pickup.id, "arrived", {"lat": 43.6, "lng": -79.4})
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("stop_arrival", stop_id=pickup.id)), 1)
        self.assertEqual(len(self._feed("pickup_start", stop_id=pickup.id)), 1)

        # Completed pickup → pickup_complete.
        res = job.driver_update_stop(pickup.id, "completed")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("pickup_complete", stop_id=pickup.id)), 1)

        # En route → en_route.
        res = job.driver_update_stop(delivery.id, "en_route")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("en_route", stop_id=delivery.id)), 1)

        # Completed delivery → delivery_complete.
        res = job.driver_update_stop(delivery.id, "completed")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("delivery_complete", stop_id=delivery.id)), 1)

        # Restore → stop_restored.
        res = job.driver_update_stop(delivery.id, "restore")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("stop_restored", stop_id=delivery.id)), 1)

        # Skip → stop_skipped.
        res = job.driver_update_stop(delivery.id, "skipped")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("stop_skipped", stop_id=delivery.id)), 1)

    # ── §9: evidence feeds carry the evidence identifier ────────────

    def test_c_evidence_feeds(self):
        booking = self._booking(pallets=1)
        job = self._job(booking)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        delivery = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")[:1]
        item = job.item_ids[0]

        res = job.driver_add_evidence(
            pickup.id, "pop", self._png("pop1"), "POP_1.png",
            {"captured_at": "2026-09-01T09:00:00"})
        self.assertTrue(res["success"], res)
        res = job.driver_add_evidence(
            delivery.id, "pod", self._png("pod1"), "POD_1.png",
            {"captured_at": "2026-09-01T11:00:00"})
        self.assertTrue(res["success"], res)
        res = job.driver_add_evidence(
            pickup.id, "popp", self._png("popp1"), "POPP_1.png",
            {"pallet_id": item.id, "captured_at": "2026-09-01T09:05:00"})
        self.assertTrue(res["success"], res)
        res = job.driver_add_evidence(
            pickup.id, "scan", self._png("scan1"), "SCAN_1.png",
            {"scan_session": "s1", "scan_page_index": 1})
        self.assertTrue(res["success"], res)

        for ev_type, stop in (("evidence_pop", pickup), ("evidence_pod", delivery),
                              ("evidence_popp", pickup), ("scan_uploaded", pickup)):
            rows = self._feed(ev_type, stop_id=stop.id)
            self.assertEqual(len(rows), 1, ev_type)
            self.assertTrue(rows[0].evidence_id, ev_type)
            # The evidence row's canonical record links back to the SAME
            # logical stop — never a shared visit bucket.
            ev = rows[0].evidence_id
            self.assertEqual(ev.stop_id.id, stop.id, ev_type)
            if ev_type == "evidence_popp":
                self.assertEqual(ev.pallet_id.id, item.id)

        # Timeline events for evidence carry the same evidence_id (scan
        # PAGES emit the feed only — the merged document posts its own
        # document_scanned event after the scan session completes).
        tl = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job.id),
            ("event_type", "in", ("evidence_uploaded", "pod_uploaded",
                                  "popp_captured")),
        ])
        self.assertEqual(len(tl), 3)
        self.assertTrue(all(tl.mapped("evidence_id")),
                        "every evidence timeline event must name its evidence")

    # ── §9: pallet loaded (actual count growth) ─────────────────────

    def test_d_pallet_loaded_feed(self):
        booking = self._booking(pallets=1)
        job = self._job(booking)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        before = len(job.item_ids)
        items = job._sync_actual_pallet_items(3, pickup_stop=pickup)
        self.assertEqual(len(items), 3)
        loaded = self._feed("pallet_loaded", job=job)
        # 2 new pallets (CASE C growth) → 2 events, one per pallet.
        self.assertEqual(len(loaded), 3 - before)
        for row in loaded:
            self.assertTrue(row.item_id)
            self.assertTrue(row.stop_id)

    # ── §9: position assignment ─────────────────────────────────────

    def test_e_position_assigned_feed(self):
        booking = self._booking(pallets=1)
        job = self._job(booking)
        plan = self.env["prema.dispatch.load.plan"].create({
            "vehicle_id": self.vehicle.id,
            "operating_date": datetime.date(2026, 9, 1),
            "layout_template_id": self.layout.id,
        })
        item = job.item_ids[0]
        item.write({"load_plan_id": plan.id})
        pos = self.layout.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id)
        rows = self._feed("position_assigned", job=job)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item_id.id, item.id)
        self.assertIn(pos.position_code, rows[0].message)

        # Moving to another position is a separate event, never a silent
        # rewrite of the assignment.
        pos2 = self.layout.position_ids[1]
        plan.move_pallet(item.id, pos2.id)
        rows = self._feed("position_assigned", job=job)
        self.assertEqual(len(rows), 2)
        self.assertIn(pos2.position_code, rows[0].message)

    # ── §9: route started / completed ───────────────────────────────

    def test_f_route_started_completed_feed(self):
        booking = self._booking()
        job = self._job(booking)
        res = job.driver_start_route(job.id)
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._feed("route_started", job=job)), 1)

        self._complete_all_stops(job)
        self.assertEqual(len(self._feed("route_completed", job=job)), 1)

    # ── §9: temperature feeds ───────────────────────────────────────

    def test_g_temperature_feeds(self):
        env = self.env
        # Conflict: two incompatible setpoints on one job → URGENT feed.
        b_a = env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "temperature_mode": "reefer",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "reefer", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 2400.0,
            "required_temperature_c": 2.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "P9 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        env["logistics.booking.stop"].create([
            {"booking_id": b_a.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P9 Reefer Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": b_a.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P9 Reefer Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        b_b = env["logistics.booking"].create({
            "partner_id": self.partner2.id,
            "shipment_type": "ltl", "temperature_mode": "reefer",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "reefer", "pallets": 1,
            "physical_pallets": 1, "weight_lbs": 2400.0,
            "required_temperature_c": 10.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "P9 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        env["logistics.booking.stop"].create([
            {"booking_id": b_b.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P9 Reefer2 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": b_b.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P9 Reefer2 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job_a = b_a._create_dispatch_job()
        job_a.vehicle_id = self.vehicle.id
        job_b = b_b._create_dispatch_job()
        job_b.vehicle_id = self.vehicle.id
        job_b.item_ids.write({"job_id": job_a.id, "sequence": 30})
        # Onboard the freight: items are future-pickups until their pickup
        # stop departs, and the engine's onboard set requires a loaded
        # status — otherwise recalc sees only pre-cool candidates.
        for j in (job_a, job_b):
            j.stop_ids.filtered(
                lambda s: s.stop_type == "pickup").write({
                    "actual_departure_time": datetime.datetime(2026, 9, 1, 12, 0)})
            j.item_ids.write({"status": "loaded"})
        state = TemperatureEngine(env).recalc(job_a)
        self.assertEqual(state["state"], "conflict")
        rows = self._feed("temperature_conflict", job=job_a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].severity, "urgent")
        self.assertIn("TEMPERATURE CONFLICT", rows[0].message)

        # Authorized override → engine writes the resolved setpoint: a
        # temperature_changed event records the instruction change.
        override, state = TemperatureEngine(env).apply_override(
            job_a, 4.0, "P9 test override")
        self.assertEqual(state["state"], "on")
        rows = self._feed("temperature_changed", job=job_a)
        # Earlier transitions are legitimately recorded too (none → precool
        # when the reefer freight first appeared, severity warning). The
        # freight-change recompute (item → loaded, spec §6) records the
        # transient A-only instruction (2°C — B's pickup had not departed
        # yet), and the override's OWN persist is the NEWEST info row,
        # carrying the 4°C setpoint.
        applied = rows.filtered(lambda r: r.severity == "info")
        self.assertEqual(len(applied), 2, [
            (r.severity, r.message) for r in rows])
        self.assertIn("4°C", applied[0].message)
        self.assertIn("2°C", applied[1].message)

    # ── §9: physical visit → one feed event PER underlying job ──────

    def test_h_physical_visit_feed_isolation(self):
        shared_loc = self._location("P9 Shared Facility")
        b1 = self._booking(pickup_loc=self._location("P9 A Pickup"),
                           delivery_loc=shared_loc)
        b2 = self._booking(pickup_loc=self._location("P9 B Pickup"),
                           delivery_loc=shared_loc, partner=self.partner2)
        job1 = self._job(b1)
        job2 = self._job(b2)
        for s in (job1.stop_ids + job2.stop_ids).filtered(
                lambda s: s.stop_type == "delivery"):
            s.with_context(_eta_engine_write=True).write({
                "scheduled_time": datetime.datetime(2026, 9, 1, 10, 0)})

        Visit = self.env["prema.dispatch.route.visit"]
        stops = job1.stop_ids + job2.stop_ids
        visits = Visit.ensure_for_stops(stops)
        self.assertEqual(len(visits), 1, "the two deliveries share ONE visit")
        visit = visits[0]
        self.assertEqual(len(visit.stop_link_ids), 2)

        res = Visit.arrive_physical_visit(visit.id, lat=43.63, lng=-79.46)
        self.assertTrue(res["success"], res)
        # One stop_arrival feed event PER job — each carries the SAME
        # shared visit identifier but its own job/stop. Never a combined
        # bucket.
        arrivals = self._feed("stop_arrival")
        job_rows = arrivals.filtered(lambda r: r.job_id.id in (job1.id, job2.id))
        self.assertEqual(len(job_rows), 2)
        self.assertEqual(set(job_rows.mapped("job_id").ids), {job1.id, job2.id})
        self.assertEqual(set(job_rows.mapped("visit_id").ids), {visit.id})
        self.assertNotEqual(job_rows[0].stop_id.id, job_rows[1].stop_id.id)

    # ── §9: feed vs alert separation + staff gate ───────────────────

    def test_i_feed_alert_separation_and_access(self):
        booking = self._booking()
        job = self._job(booking)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        job.driver_update_stop(pickup.id, "arrived")
        # Feed rows are closed (resolved): never in the actionable alert list.
        updates = self.env["prema.dispatch.driver.update"].get_live_updates()
        feed_ids = self._feed("stop_arrival").ids
        self.assertFalse(
            set(feed_ids) & {u["id"] for u in updates["updates"]},
            "feed rows must never pollute the alert panel")

        feed = self.env["prema.dispatch.driver.update"].get_feed(limit=50)
        self.assertTrue(feed["updates"])
        self.assertIn("vehicle_name", feed["updates"][0])
        self.assertIn("visit_id", feed["updates"][0])
        self.assertIn("customer_name", feed["updates"][0])
        self.assertIn("is_alert", feed["updates"][0])
        # Newest first.
        stamps = [u["reported_at"] for u in feed["updates"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

        # Non-staff users are rejected server-side (UI hiding is not
        # security — the RPC itself must refuse).
        outsider = self.env["res.users"].create({
            "name": "p9outsider", "login": "p9outsider@test.local",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            self.env["prema.dispatch.driver.update"].with_user(
                outsider).get_feed()
        with self.assertRaises(AccessError):
            self.env["prema.dispatch.driver.update"].with_user(
                outsider).get_live_updates()

    # ── §9: observability-only — never raises ───────────────────────

    def test_j_feed_observability_never_raises(self):
        booking = self._booking()
        job = self._job(booking)
        # Garbage identifiers must never raise — the driver action
        # proceeds regardless.
        ok = job._emit_feed(
            "en_route", stop=False, message="still fine")
        self.assertTrue(ok)
        rec = self.env["prema.dispatch.driver.update"].sudo()
        res = rec._record_event(
            job, "en_route", stop=None, item=None,
            vehicle=self.env["fleet.vehicle"].browse(999999),
            visit=self.env["prema.dispatch.route.visit"].browse(999999),
            message="edge inputs")
        self.assertTrue(res)

    # ── §10: timeline identifiers ───────────────────────────────────

    def test_k_timeline_identifiers(self):
        booking = self._booking()
        job = self._job(booking)
        stop = job.stop_ids[0]
        item = job.item_ids[0]
        visit = self.env["prema.dispatch.route.visit"].create({
            "operating_date": datetime.date(2026, 9, 1),
            "vehicle_id": self.vehicle.id,
            "saved_location_id": stop.saved_location_id.id,
            "address": stop.address,
        })
        job._post_timeline(
            job, "evidence_uploaded", notes="with identifiers",
            stop=stop, visit=visit, pallet=item)
        ev = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job.id), ("notes", "=", "with identifiers")])
        self.assertEqual(len(ev), 1)
        row = ev[0]
        self.assertEqual(row.stop_id.id, stop.id)
        self.assertEqual(row.visit_id.id, visit.id)
        self.assertEqual(row.pallet_id.id, item.id)
        # Related identifiers resolve from the job — the Progress page's
        # customer/booking columns come straight from here.
        self.assertEqual(row.customer_id.id, self.partner.id)
        self.assertEqual(row.booking_id.id, booking.id)

    # ── §10: shared visit evidence stays per job/stop ───────────────

    def test_l_shared_visit_evidence_never_combined(self):
        shared_loc = self._location("P9 Shared Facility 2")
        b1 = self._booking(pickup_loc=self._location("P9 C Pickup"),
                           delivery_loc=shared_loc)
        b2 = self._booking(pickup_loc=self._location("P9 D Pickup"),
                           delivery_loc=shared_loc, partner=self.partner2)
        job1 = self._job(b1)
        job2 = self._job(b2)
        Visit = self.env["prema.dispatch.route.visit"]
        visits = Visit.ensure_for_stops(job1.stop_ids + job2.stop_ids)
        self.assertEqual(len(visits), 1)
        d1 = job1.stop_ids.filtered(lambda s: s.stop_type == "dropoff")[:1]
        d2 = job2.stop_ids.filtered(lambda s: s.stop_type == "dropoff")[:1]

        job1.driver_add_evidence(
            d1.id, "pod", self._png("podA"), "POD_A.png",
            {"captured_at": "2026-09-01T11:00:00"})
        job2.driver_add_evidence(
            d2.id, "pod", self._png("podB"), "POD_B.png",
            {"captured_at": "2026-09-01T11:05:00"})

        Evidence = self.env["prema.dispatch.evidence"]
        ev1 = Evidence.search([("stop_id", "=", d1.id)])
        ev2 = Evidence.search([("stop_id", "=", d2.id)])
        self.assertEqual(len(ev1), 1)
        self.assertEqual(len(ev2), 1)
        # The same physical visit, but the UI never merges the two PODs:
        # attachment buckets, evidence records and timeline events all
        # stay on their own logical stop/job.
        self.assertEqual(d1.pod_attachment_ids, ev1.attachment_id)
        self.assertEqual(d2.pod_attachment_ids, ev2.attachment_id)
        self.assertEqual(ev1.job_id.id, job1.id)
        self.assertEqual(ev2.job_id.id, job2.id)
        tl1 = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job1.id), ("event_type", "=", "pod_uploaded")])
        tl2 = self.env["prema.dispatch.timeline.event"].search([
            ("job_id", "=", job2.id), ("event_type", "=", "pod_uploaded")])
        self.assertEqual(len(tl1), 1)
        self.assertEqual(len(tl2), 1)
        self.assertEqual(tl1.evidence_id.id, ev1.id)
        self.assertEqual(tl2.evidence_id.id, ev2.id)
        self.assertNotEqual(ev1.id, ev2.id)

    # ── §10: progress computed fields ───────────────────────────────

    def test_m_progress_computed_fields(self):
        booking = self._booking(pallets=1)
        job = self._job(booking)
        pickup = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        delivery = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")[:1]
        item = job.item_ids[0]

        # Stop evidence text: empty when nothing required/attached.
        self.assertEqual(pickup.evidence_status_text, "—")
        job.driver_add_evidence(
            pickup.id, "pop", self._png("m1"), "POP_M.png")
        self.assertIn("POP 1 ✓", pickup.evidence_status_text)
        self.assertEqual(pickup.pop_count, 1)

        # Missing required proof on a completed stop is flagged (the
        # requirement is raised AFTER completion — completing a stop whose
        # booking already REQUIRES proof would block by design).
        delivery.action_mark_arrived()
        delivery.action_mark_completed()
        delivery.write({"pod_required": True})
        self.assertIn("POD 0 ⚠", delivery.evidence_status_text)

        # Actual service duration = departure − arrival.
        delivery.write({
            "actual_arrival_time": datetime.datetime(2026, 9, 1, 10, 0),
            "actual_departure_time": datetime.datetime(2026, 9, 1, 10, 25),
        })
        self.assertEqual(delivery.actual_service_minutes, 25)

        # Pallet POPP/POD status.
        self.assertEqual(item.popp_status_text, "None")
        job.driver_add_evidence(
            pickup.id, "popp", self._png("m2"), "POPP_M.png",
            {"pallet_id": item.id})
        self.assertIn("1 photo", item.popp_status_text)
        job.driver_add_evidence(
            delivery.id, "pod", self._png("m3"), "POD_M.png")
        self.assertEqual(item.pod_status_text, "POD ✓")

        # Physical route summary names the facilities in order.
        route = job.physical_route_text
        self.assertIn("P9 Pickup", route)
        self.assertIn("P9 Delivery", route)

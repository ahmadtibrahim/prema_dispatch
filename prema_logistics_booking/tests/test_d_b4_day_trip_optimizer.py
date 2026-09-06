# -*- coding: utf-8 -*-
"""Work package D-B4 — §15 daily multi-booking trip optimizer + §16.6
board drag guard.

Acceptance coverage of the master requirements (synthetic fixtures; the
proposal path writes NOTHING until an explicit dispatcher Apply):

(a) several accepted loads on one truck/day produce a FEASIBLE proposed
    stop sequence that respects pickup-before-delivery per load and keeps
    onboard pallets within the truck's positions at every stop;
(b) creating a proposal mutates nothing (jobs, stops, order, windows and
    loads are byte-identical after generate);
(c) Apply writes the proposed order exactly once — a second apply is
    refused (state/version guard);
(d) a changed window/load after generation marks the proposal STALE and
    apply refuses it (never silently re-applied over a changed day);
(e) the §16.6 drag-equivalent call: an invalid reorder (delivery before
    its pickup) is refused with a UserError and mutates NOTHING, the
    refusal is audit-logged, and only an explicit, validated, audited
    reorder (the dispatcher's confirmed change) may rewrite the day.

Run: --test-tags /prema_logistics_booking/tests/test_d_b4_day_trip_optimizer
"""
import datetime
import json

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

# Synthetic test addresses (streets that do not exist) — never real
# customer facilities.
DAY = datetime.date(2026, 9, 23)
D8 = datetime.datetime(2026, 9, 23, 8, 0)
D12 = datetime.datetime(2026, 9, 23, 12, 0)
DISPT = "prema_dispatch.group_dispatcher"


def _order_of(proposal, stop):
    """Optimized position (1-based) of a stop inside a proposal."""
    line = proposal.line_ids.filtered(lambda l: l.stop_id.id == stop.id)
    return line.optimized_order if line else -1


class TestDB4DayTripOptimizer(TransactionCase):
    """D-B4 §15/§16.6 — day-route proposal lifecycle + drag guard."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        group = env.ref(DISPT)
        if not env.user.has_group(DISPT):
            env.user.write({"groups_id": [(4, group.id)]})
        env["ir.config_parameter"].sudo().set_param("deepseek.api_key", "")

        stage = (env.ref("prema_dispatch.stage_assigned",
                         raise_if_not_found=False)
                 or env["prema.dispatch.stage"].sudo().search(
                     [("stage_type", "=", "draft")], limit=1))
        cls.stage = stage

        model = env["fleet.vehicle.model"].create({
            "name": "D-B4 Box Truck",
        })
        cls.truck = env["fleet.vehicle"].create({
            "model_id": model.id,
            "license_plate": "TEST-B4-TRUCK",
            "odometer_unit": "kilometers",
            "power_unit": "power",
        })
        # Explicit canonical layout: 40 pallet positions, default (without
        # it the capacity service falls back to the 12/13/14 defaults).
        env["fleet.vehicle.pallet.layout"].create({
            "vehicle_id": cls.truck.id,
            "name": "D-B4 40-ft Standard",
            "code": "db4-40",
            "layout_type": "standard",
            "max_pallets": 40,
            "is_default": True,
            "sequence": 10,
        })

        cls.dispatcher = env["res.users"].create({
            "name": "D-B4 Dispatcher", "login": "db4disp@test.local",
            "tz": "UTC",
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id, group.id])],
        })
        cls.plain = env["res.users"].create({
            "name": "D-B4 Plain User", "login": "db4plain@test.local",
            "tz": "UTC",
            "groups_id": [(6, 0, [env.ref("base.group_user").id])],
        })

    # ── Fixture helpers ─────────────────────────────────────────────

    def _mk_load(self, pallets, tag):
        """One accepted synthetic load on the D-B4 truck/day: a booking
        with a pickup + delivery stop converted into a dispatch job (the
        same legacy no-legs bridge the P14/P20 suites exercise)."""
        env = self.env
        partner = env["res.partner"].create({
            "name": "D-B4 Customer %s" % tag,
            "is_company": True,
            "email": "db4-%s@example.test" % tag,
        })
        booking = env["logistics.booking"].create({
            "partner_id": partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": pallets, "physical_pallets": pallets,
            "weight_lbs": 2400.0,
            "pickup_date": DAY,
            "estimated_delivery_date": DAY,
            "price_snapshot": [{"line": "D-B4 test"}],
            "booking_number":
                env["logistics.booking"]._generate_booking_number(),
        })
        env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10,
             "stop_type": "pickup",
             "city": "D-B4 %s Pickup" % tag, "pallet_count": 0},
            {"booking_id": booking.id, "sequence": 20,
             "stop_type": "delivery",
             "city": "D-B4 %s Delivery" % tag, "pallet_count": pallets},
        ])
        job = booking._create_dispatch_job()
        job = job[0] if isinstance(job, (list, tuple)) else job
        job.write({
            "vehicle_id": self.truck.id,
            "scheduled_pickup": D8,
        })
        if self.stage:
            job.write({"stage_id": self.stage.id})
        for stop in job.stop_ids.sorted(lambda s: (s.sequence or 0, s.id)):
            stop.write({"status": "pending",
                        "service_time_minutes": 15})
        pickup = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")
        delivery = job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff")
        if pickup and delivery:
            pickup.write({"pallets_in": pallets, "scheduled_time": D8})
            delivery.write({"pallets_out": pallets,
                            "scheduled_time": D12})
        return job

    def _three_loads(self):
        """Three accepted loads on the truck/day: 5 + 7 + 11 = 23 ≤ 40."""
        a = self._mk_load(5, "A")
        b = self._mk_load(7, "B")
        c = self._mk_load(11, "C")
        return a, b, c

    def _day_snapshot(self, jobs):
        """Tuple fingerprint of everything a proposal must never touch:
        per-stop sequence/status/windows/load + job stage/vehicle."""
        out = []
        for job in jobs:
            out.append((job.id, job.stage_id.id, job.vehicle_id.id))
            for s in job.stop_ids.sorted(lambda x: (x.sequence or 0, x.id)):
                out.append((s.id, s.sequence, s.status,
                            s.time_window_type, s.pallets_in,
                            s.pallets_out, s.weight_in_lbs,
                            s.weight_out_lbs,
                            s.scheduled_time.isoformat()
                            if s.scheduled_time else None,
                            s.service_time_minutes))
        return out

    # ── (a) feasible sequence: precedence + capacity ────────────────

    def test_a_feasible_sequence_respects_precedence_and_capacity(self):
        a, b, c = self._three_loads()
        proposal = self.env["prema.dispatch.day.route.proposal"].generate(
            self.truck.id, DAY)
        self.assertTrue(proposal.feasible, proposal.reason)
        # Every load: its pickup is proposed BEFORE its delivery.
        for job in (a, b, c):
            pickups = job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup")
            dropoffs = job.stop_ids.filtered(
                lambda s: s.stop_type == "dropoff")
            self.assertTrue(pickups and dropoffs, "job %s stops" % job.id)
            self.assertLess(
                _order_of(proposal, pickups[0]),
                _order_of(proposal, dropoffs[0]),
                "delivery proposed before its pickup on job %s" % job.id)
        # Capacity: onboard never negative and never above 40 positions.
        timeline = json.loads(proposal.capacity_timeline_json or "[]")
        self.assertTrue(timeline)
        for tick in timeline:
            self.assertGreaterEqual(tick["before"], 0)
            self.assertGreaterEqual(tick["after"], 0)
            self.assertLessEqual(tick["after"], 40)
            self.assertLessEqual(tick["before"], 40)
        self.assertLessEqual(proposal.peak_onboard, 40)
        self.assertEqual(proposal.onboard_at_start, 0)

    # ── (b) generation mutates nothing ──────────────────────────────

    def test_b_proposal_creation_mutates_nothing(self):
        a, b, c = self._three_loads()
        jobs = (a, b, c)
        before = self._day_snapshot(jobs)
        proposal = self.env["prema.dispatch.day.route.proposal"].generate(
            self.truck.id, DAY)
        self.assertEqual(self._day_snapshot(jobs), before,
                         "generate() wrote to jobs/stops")
        # And the proposal is a self-consistent picture of that day.
        from odoo.addons.prema_logistics_booking.services.day_route_service import (
            DayRouteService)
        fp = DayRouteService(self.env).compute_fingerprint(
            self.truck.id, DAY)
        self.assertEqual(proposal.fingerprint, fp)
        scope_ids = (set(proposal.line_ids.mapped("stop_id.id")))
        self.assertEqual(len(scope_ids), 6)
        self.assertEqual(proposal.state, "proposed")
        self.assertFalse(proposal.is_stale)

    # ── (c) apply exactly once ──────────────────────────────────────

    def test_c_apply_exactly_once(self):
        a, b, c = self._three_loads()
        proposal = self.env["prema.dispatch.day.route.proposal"].generate(
            self.truck.id, DAY)
        result = proposal.apply()
        self.assertTrue(result["success"])
        self.assertEqual(proposal.state, "applied")
        self.assertEqual(proposal.version, 2)
        self.assertTrue(proposal.applied_at)
        sequences = sorted(
            s.sequence for job in (a, b, c)
            for s in job.stop_ids)
        self.assertEqual(sequences, [10, 20, 30, 40, 50, 60])
        # A second apply is refused — the change happened exactly once.
        with self.assertRaises(UserError):
            proposal.apply()
        sequences_after = sorted(
            s.sequence for job in (a, b, c)
            for s in job.stop_ids)
        self.assertEqual(sequences_after, [10, 20, 30, 40, 50, 60])

    # ── (d) a changed day stales the proposal; apply refuses ────────

    def test_d_changed_window_marks_stale_and_apply_refused(self):
        a, b, c = self._three_loads()
        proposal = self.env["prema.dispatch.day.route.proposal"].generate(
            self.truck.id, DAY)
        delivery = b.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff")
        # The dispatcher edits the load/window after generation…
        delivery.write({"pallets_out": 9})
        self.assertTrue(proposal.is_stale, "proposal not marked stale")
        self.assertIn("pallets_out", proposal.stale_reason or "")
        # …and the stale proposal is NEVER silently re-applied.
        with self.assertRaises(UserError) as caught:
            proposal.apply()
        self.assertIn("STALE", str(caught.exception))
        # A fresh proposal for the changed day applies normally.
        fresh = self.env["prema.dispatch.day.route.proposal"].generate(
            self.truck.id, DAY)
        self.assertFalse(fresh.is_stale)
        self.assertTrue(fresh.apply()["success"])

    # ── (e) drag-equivalent reorder: refused without confirm ────────

    def test_e_invalid_drag_does_not_mutate_and_is_audited(self):
        a, b, c = self._three_loads()
        jobs = (a, b, c)
        job_model = self.env["prema.dispatch.job"]
        before = self._day_snapshot(jobs)
        # Delivery of load A dragged BEFORE its pickup: the board payload
        # is a flat full-day list, exactly like dispatch_board.js sends.
        all_stops = [s for job in jobs
                     for s in job.stop_ids.sorted(
                         lambda x: (x.sequence or 0, x.id))]
        # all_stops = [P_A, D_A, P_B, D_B, P_C, D_C]
        stop_order = [all_stops[1].id] + [s.id for s in all_stops
                                          if s.id != all_stops[1].id]
        # Unauthorized user: refused before anything is touched.
        with self.assertRaises(UserError):
            job_model.with_user(self.plain).driver_reorder_stops_for_truck(
                stop_order)
        # Dispatcher: the same invalid drop is refused with validation.
        with self.assertRaises(UserError) as caught:
            job_model.with_user(
                self.dispatcher).driver_reorder_stops_for_truck(stop_order)
        self.assertIn("Reordering refused", str(caught.exception))
        # Nothing mutated and the refusal is on the audit trail.
        self.assertEqual(self._day_snapshot(jobs), before)
        refused = self.env["prema.dispatch.day.route.event"].sudo().search([
            ("vehicle_id", "=", self.truck.id),
            ("operating_date", "=", DAY),
            ("event_type", "=", "reorder_refused"),
        ])
        self.assertTrue(refused)
        # No reorder was applied either way.
        self.assertFalse(self.env[
            "prema.dispatch.day.route.event"].sudo().search([
                ("vehicle_id", "=", self.truck.id),
                ("event_type", "=", "manual_reorder")]))

    def test_f_validated_drag_applies_and_is_audited(self):
        """An explicit dispatcher reorder IS a confirmed change: same
        validation, then written and audit-logged (manual_reorder)."""
        a, b, c = self._three_loads()
        job_model = self.env["prema.dispatch.job"]
        ordered = [s for job in (a, b, c)
                   for s in job.stop_ids.sorted(
                       lambda x: (x.sequence or 0, x.id))]
        # Legal: every pickup still precedes its own delivery.
        # ordered = [P_A, D_A, P_B, D_B, P_C, D_C] →
        # payload = [P_A, P_B, P_C, D_C, D_B, D_A]
        stop_order = [ordered[0].id, ordered[2].id, ordered[4].id,
                      ordered[5].id, ordered[3].id, ordered[1].id]
        res = job_model.with_user(
            self.dispatcher).driver_reorder_stops_for_truck(stop_order)
        self.assertTrue(res.get("success"))
        by_id = {s.id: s for s in ordered}
        seqs = [by_id[sid].sequence for sid in stop_order]
        self.assertEqual(seqs, [10, 20, 30, 40, 50, 60])
        audit = self.env["prema.dispatch.day.route.event"].sudo().search([
            ("vehicle_id", "=", self.truck.id),
            ("operating_date", "=", DAY),
            ("event_type", "=", "manual_reorder"),
        ])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit.changed_by.id, self.dispatcher.id)
        # The no-mutate RPC agrees with the accepted order.
        check = job_model.with_user(
            self.dispatcher).validate_stop_order_rpc(stop_order)
        self.assertTrue(check["valid"], check["errors"])

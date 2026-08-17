"""Milk-run operations — per-stop pickup actuals, POP/POD enforcement,
proof override, order-dependent capacity, load plan summary, driver
payload enrichment."""
import datetime

from odoo import exceptions
from odoo.tests import TransactionCase


class TestMilkRunOperations(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].search([], limit=1)
        cls.stage_draft = cls.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1)

    def _make_job(self):
        return self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "scheduled_pickup": datetime.datetime(2026, 8, 19, 12, 0),
        })

    def _canonical_job(self):
        """UD pickup (4 → Ottawa), TF pickup (3 → Belleville)."""
        job = self._make_job()
        ud = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 10, "stop_type": "pickup",
            "address": "United Dairy", "pallets_in": 4,
            "pop_required": True,
        })
        tf = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 20, "stop_type": "pickup",
            "address": "TerraFreska", "pallets_in": 3,
            "pop_required": True,
        })
        blv = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 30, "stop_type": "dropoff",
            "address": "Belleville Depot", "pallets_out": 3,
            "pod_required": True,
        })
        ott = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 40, "stop_type": "dropoff",
            "address": "Ottawa DC", "pallets_out": 4,
            "pod_required": True,
        })
        for i in range(4):
            self.env["prema.dispatch.item"].create({
                "job_id": job.id, "name": "U-%02d" % (i + 1),
                "weight_lbs": 500.0,
                "pickup_stop_id": ud.id, "delivery_stop_id": ott.id,
                "available_after_stop_id": ud.id,
            })
        for i in range(3):
            self.env["prema.dispatch.item"].create({
                "job_id": job.id, "name": "TF-%02d" % (i + 1),
                "weight_lbs": 400.0,
                "pickup_stop_id": tf.id, "delivery_stop_id": blv.id,
                "available_after_stop_id": tf.id,
            })
        return job, ud, tf, blv, ott

    def _attachment(self):
        return self.env["ir.attachment"].create({
            "name": "proof.jpg",
            "datas": "aGVsbG8=",  # base64 "hello"
            "res_model": "prema.dispatch.stop",
        })

    # ── Per-stop pickup actuals ─────────────────────────────────────

    def test_01_pickup_variance_isolated_per_stop(self):
        """TerraFreska expected 3, actual 2 — UD actuals untouched,
        Belleville expectation recomputed, one TF item cancelled."""
        job, ud, tf, blv, ott = self._canonical_job()
        tf.confirm_pickup_actuals(2, 800.0, notes="One pallet not ready.")
        self.assertEqual(tf.actual_pallets_in, 2)
        self.assertEqual(tf.actual_weight_in_lbs, 800.0)
        self.assertTrue(tf.pickup_actuals_confirmed_at)
        self.assertEqual(tf.pickup_actuals_confirmed_by, self.env.user)
        self.assertEqual(tf.variance_notes, "One pallet not ready.")
        # UD actuals are NOT altered by the TF variance.
        self.assertFalse(ud.actual_pallets_in)
        self.assertFalse(ud.pickup_actuals_confirmed_at)
        # Downstream recompute: Belleville 3 → 2, Ottawa unchanged.
        self.assertEqual(blv.pallets_out, 2)
        self.assertAlmostEqual(blv.weight_out_lbs, 800.0, places=1)
        self.assertEqual(ott.pallets_out, 4)
        # Exactly one TF item cancelled (the one beyond actual count).
        cancelled = job.item_ids.filtered(lambda i: i.status == "cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(cancelled.name.startswith("TF-"))

    def test_02_full_pickup_no_variance_keeps_expectations(self):
        job, ud, tf, blv, ott = self._canonical_job()
        ud.confirm_pickup_actuals(4, 2000.0)
        self.assertEqual(ud.actual_pallets_in, 4)
        self.assertEqual(blv.pallets_out, 3)  # nothing cancelled
        self.assertFalse(job.item_ids.filtered(lambda i: i.status == "cancelled"))

    # ── POP/POD enforcement ─────────────────────────────────────────

    def test_03_pop_required_blocks_pickup_completion(self):
        job, ud, tf, blv, ott = self._canonical_job()
        with self.assertRaises(exceptions.UserError):
            ud.action_mark_completed()
        self.assertEqual(ud.status, "pending")

    def test_04_pop_attachment_allows_completion(self):
        job, ud, tf, blv, ott = self._canonical_job()
        att = self._attachment()
        ud.write({"pop_attachment_ids": [(4, att.id)]})
        ud.action_mark_completed()
        self.assertEqual(ud.status, "completed")

    def test_05_pod_required_blocks_delivery_completion(self):
        job, ud, tf, blv, ott = self._canonical_job()
        with self.assertRaises(exceptions.UserError):
            blv.action_mark_completed()
        self.assertEqual(blv.status, "pending")

    def test_06_proof_override_wizard_records_audit(self):
        job, ud, tf, blv, ott = self._canonical_job()
        wizard = self.env["prema.dispatch.proof.override.wizard"].create({
            "stop_id": blv.id,
            "reason": "Receiver refused photo; signed paper POD kept.",
        })
        wizard.apply_override()
        self.assertEqual(blv.proof_override_by, self.env.user)
        self.assertTrue(blv.proof_override_at)
        self.assertIn("signed paper", blv.proof_override_reason)
        # Override lets the stop complete without attachments.
        blv.action_mark_completed()
        self.assertEqual(blv.status, "completed")

    # ── Order-dependent capacity ────────────────────────────────────

    def _vehicle(self, max_pallets):
        model = self.env["fleet.vehicle.model"].search([], limit=1)
        if not model:
            model = self.env["fleet.vehicle.model"].create({"name": "Van"})
        return self.env["fleet.vehicle"].create({
            "name": "Truck %s" % max_pallets,
            "model_id": model.id,
            "x_max_pallets": max_pallets,
        })

    def test_07_capacity_check_is_order_dependent(self):
        """UD→TF→BLV→OTT peaks at 7 (blocked on a 6-position truck);
        UD→OTT→TF→BLV peaks at 4 (fits the same truck). Total handled
        is 7 either way — capacity is MAXIMUM SIMULTANEOUS ONBOARD."""
        job, ud, tf, blv, ott = self._canonical_job()
        job.write({"vehicle_id": self._vehicle(6).id})
        check = job.route_capacity_check()
        self.assertEqual(check["peak"], 7)
        self.assertEqual(check["vehicle_max"], 6)
        self.assertFalse(check["ok"])
        # Re-sequence: UD → OTT → TF → BLV.
        ud.write({"sequence": 10})
        ott.write({"sequence": 20})
        tf.write({"sequence": 30})
        blv.write({"sequence": 40})
        check = job.route_capacity_check()
        self.assertEqual(check["peak"], 4)
        self.assertTrue(check["ok"])

    # ── Capacity: total handled > capacity, peak fits ───────────────

    def test_07b_total_handled_exceeds_capacity_peak_fits(self):
        """+8, −5, +8: 16 pallets handled, peak 11 — acceptable on a
        configured 13-position vehicle. Capacity is MAXIMUM SIMULTANEOUS
        ONBOARD, never total pallets handled."""
        job = self._make_job()
        pa = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 10, "stop_type": "pickup",
            "address": "Pickup A", "pallets_in": 8})
        da = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "Drop A", "pallets_out": 5})
        pb = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 30, "stop_type": "pickup",
            "address": "Pickup B", "pallets_in": 8})
        db = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 40, "stop_type": "dropoff",
            "address": "Drop B", "pallets_out": 11})
        for i in range(8):
            self.env["prema.dispatch.item"].create({
                "job_id": job.id, "name": "A-%02d" % (i + 1),
                "weight_lbs": 500.0,
                "pickup_stop_id": pa.id,
                "delivery_stop_id": (da.id if i < 5 else db.id),
                "available_after_stop_id": pa.id,
            })
        for i in range(8):
            self.env["prema.dispatch.item"].create({
                "job_id": job.id, "name": "B-%02d" % (i + 1),
                "weight_lbs": 500.0,
                "pickup_stop_id": pb.id,
                "delivery_stop_id": db.id,
                "available_after_stop_id": pb.id,
            })
        check = job.route_capacity_check()
        self.assertEqual(check["peak"], 11)  # 8 → 3 → 11 → 0
        self.assertEqual(len(job.item_ids), 16)  # total handled 16
        # On a 13-position truck the SAME route is fine.
        job.write({"vehicle_id": self._vehicle(13).id})
        check = job.route_capacity_check()
        self.assertEqual(check["peak"], 11)
        self.assertTrue(check["ok"])
        # But not on an 8-position truck.
        job.write({"vehicle_id": self._vehicle(8).id})
        check = job.route_capacity_check()
        self.assertFalse(check["ok"])

    # ── Load plan summary (future pickups, same item identity) ──────

    def test_08_load_plan_summary_future_pickups(self):
        """Before any pickup all 7 items are future/planned; after TF
        pickup the SAME 3 TF item rows become onboard — never duplicated."""
        job, ud, tf, blv, ott = self._canonical_job()
        summary = job.load_plan_summary()
        self.assertEqual(summary["onboard_items"], 0)
        self.assertEqual(summary["future_pickup_items"], 7)
        self.assertEqual(summary["planned_peak"], 7)
        # TF pickup executes: its 3 items become onboard (identity
        # preserved — no re-creation at pickup).
        tf_items = job.item_ids.filtered(
            lambda i: i.pickup_stop_id.id == tf.id)
        self.assertEqual(len(tf_items), 3)
        att = self._attachment()
        tf.write({"pop_attachment_ids": [(4, att.id)]})
        tf.action_mark_completed()
        summary = job.load_plan_summary()
        self.assertEqual(summary["onboard_items"], 3)
        self.assertEqual(summary["future_pickup_items"], 4)
        # The exact same physical item rows moved from planned to onboard.
        self.assertEqual(
            set(job.item_ids.filtered(lambda i: not i.pending_future_pickup
                                      and i.status != "cancelled").ids),
            set(tf_items.ids),
        )

    # ── Driver payload enrichment ───────────────────────────────────

    def test_09_driver_stop_dict_milk_run_fields(self):
        job, ud, tf, blv, ott = self._canonical_job()
        ud.write({
            "requires_liftgate": True,
            "appointment_required": True,
            "operating_hours_snapshot": {str(d): [6.0, 16.0] for d in range(7)},
            "dispatcher_notes": "Dock 3",
        })
        payload = job._driver_stop_dict(ud)
        self.assertTrue(payload["pop_required"])
        self.assertTrue(payload["liftgate_required"])
        self.assertTrue(payload["appointment_required"])
        self.assertEqual(payload["expected_pallets_in"], 4)
        self.assertEqual(payload["instructions"], "Dock 3")
        self.assertTrue(payload["facility_hours"])  # e.g. "06:00 AM – 04:00 PM"
        ott_payload = job._driver_stop_dict(ott)
        self.assertTrue(ott_payload["pod_required"])
        self.assertEqual(ott_payload["expected_pallets_out"], 4)

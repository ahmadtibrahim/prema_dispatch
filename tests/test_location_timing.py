"""Phase 6 — Historical stop timing and dwell estimates (spec PHASE 6).

Every completed stop at a saved location archives a raw visit sample; the
location then exposes exact median dwell, last-10 dwell average and
per-type loading/unloading averages, and new stops created at a known
location start with its learned service time.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLocationTiming(TransactionCase):

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create(
            {"name": "Timing Customer", "is_company": True})
        stage = self.env["prema.dispatch.stage"].search(
            [("is_booking_phase", "=", True)], limit=1) or \
            self.env["prema.dispatch.stage"].create({
                "name": "Timing Booking", "is_booking_phase": True})
        self.job = self.env["prema.dispatch.job"].create({
            "name": "TIMING-0001",
            "partner_id": self.customer.id,
            "stage_id": stage.id,
            "scheduled_pickup": "2026-08-18 09:00:00",
        })
        self.loc = self.env["prema.dispatch.location"].create({
            "name": "Timing Warehouse",
            "business_name": "Timing Warehouse",
            "address": "100 Timing Rd, Toronto, ON",
        })

    # ── helpers ────────────────────────────────────────────────

    def _stop(self, stop_type="dropoff", seq=10, service=15):
        return self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": stop_type,
            "sequence": seq,
            "name": f"Timing Stop {seq}",
            "saved_location_id": self.loc.id,
            "service_time_minutes": service,
        })

    def _complete(self, stop, dwell_minutes):
        """Arrive `dwell` minutes ago, then complete — mirrors the driver
        flow (arrival recorded, action_mark_completed records departure)."""
        stop.write({
            "actual_arrival_time": fields.Datetime.now() -
                                   timedelta(minutes=dwell_minutes),
        })
        stop.action_mark_completed()
        self.assertEqual(stop.status, "completed")

    # ── §6a raw samples + median/last-10 ───────────────────────

    def test_01_samples_and_median_stats(self):
        self._complete(self._stop(seq=10, service=15), dwell_minutes=20)
        self._complete(self._stop(seq=20, service=30), dwell_minutes=40)
        self._complete(self._stop(seq=30, service=20), dwell_minutes=30)
        self.assertEqual(self.loc.use_count, 3)
        self.assertEqual(len(self.loc.visit_sample_ids), 3)
        self.assertEqual(self.loc.median_dwell_minutes, 30.0)
        self.assertEqual(self.loc.avg_last10_dwell_minutes, 30.0)
        self.assertAlmostEqual(self.loc.avg_unloading_minutes, 21.67, places=1)
        self.assertFalse(self.loc.avg_loading_minutes)  # no pickup samples yet

    def test_02_loading_breakdown(self):
        self._complete(self._stop(stop_type="pickup", seq=10, service=50), dwell_minutes=60)
        self._complete(self._stop(seq=20, service=20), dwell_minutes=35)
        self.assertEqual(self.loc.avg_loading_minutes, 50.0)
        self.assertEqual(self.loc.avg_unloading_minutes, 20.0)
        self.assertEqual(self.loc.median_dwell_minutes, 47.5)

    def test_03_last10_window(self):
        for i in range(12):
            self._complete(self._stop(seq=(i + 1) * 10, service=5),
                           dwell_minutes=(i + 1) * 10)
        # dwells 10..120 → median 65, last-10 = mean(30..120) = 75
        self.assertEqual(self.loc.median_dwell_minutes, 65.0)
        self.assertEqual(self.loc.avg_last10_dwell_minutes, 75.0)
        self.assertEqual(len(self.loc.visit_sample_ids), 12)

    # ── §6b learned service time wired into new stops ──────────

    def test_04_recommended_time_wires_into_stop_values(self):
        self.loc.recommended_service_time_minutes = 45
        vals = self.env["prema.dispatch.stop"]._saved_location_values(self.loc)
        self.assertEqual(vals["service_time_minutes"], 45)
        # a fresh stop linked to the location picks up the learned time
        fresh = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id, "stop_type": "dropoff", "sequence": 10,
            "name": "Fresh Stop"})
        fresh._apply_saved_location(self.loc)
        self.assertEqual(fresh.service_time_minutes, 45)
        # ...but an explicitly-set service time is never clobbered
        kept = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id, "stop_type": "dropoff", "sequence": 20,
            "name": "Kept Stop", "service_time_minutes": 20})
        kept._apply_saved_location(self.loc)
        self.assertEqual(kept.service_time_minutes, 20)

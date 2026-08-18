"""Phase 5 — Live sync (spec §32-§34).

The Booking Board's FEASIBILITY column is replaced by LIVE PROGRESS
(computed live from stop states + driver actions), every driver action
propagates to the job timeline (which the customer tracking portal and
the board both read), and the driver app's Skip is now a real
server-side action instead of a client-side status flip.
"""
import base64
import io

from odoo import fields
from odoo.tests import TransactionCase, tagged

try:
    from PIL import Image
except ImportError:
    Image = None


def _jpeg_b64(color=(120, 60, 200), size=(48, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


@tagged("post_install", "-at_install")
class TestLiveSync(TransactionCase):

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create(
            {"name": "LiveSync Customer", "is_company": True})
        self.driver_partner = self.env["res.partner"].create({
            "name": "LiveSync Driver Partner",
            # message_post as the driver needs a real sender address
            "email": "livesync.driver@example.com",
        })
        self.driver_user = self.env["res.users"].create({
            "name": "LiveSync Driver",
            "login": "livesyncdriver",
            "partner_id": self.driver_partner.id,
            "groups_id": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("prema_dispatch.group_dispatch_driver").id),
            ],
        })
        self.driver_env = self.env(user=self.driver_user.id)
        stage = self.env["prema.dispatch.stage"].search(
            [("is_booking_phase", "=", True)], limit=1) or \
            self.env["prema.dispatch.stage"].create({
                "name": "LiveSync Booking", "is_booking_phase": True})
        self.job = self.env["prema.dispatch.job"].create({
            "name": "LIVESYNC-0001",
            "partner_id": self.customer.id,
            "stage_id": stage.id,
            "driver_id": self.driver_partner.id,
            "scheduled_pickup": "2026-08-18 09:00:00",
            "expected_pallet_count": 2,
            "approximate_skids": 2,
        })
        self.stop = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": "pickup",
            "sequence": 10,
            "name": "LiveSync Warehouse",
            "scheduled_time": "2026-08-18 09:00:00",
        })
        self.drops = self.env["prema.dispatch.stop"].create([
            {"job_id": self.job.id, "stop_type": "dropoff", "sequence": 20,
             "name": "Drop A", "scheduled_time": "2026-08-18 12:00:00"},
            {"job_id": self.job.id, "stop_type": "dropoff", "sequence": 30,
             "name": "Drop B", "scheduled_time": "2026-08-18 15:00:00"},
        ])

    # ── helpers ────────────────────────────────────────────────

    def _progress(self):
        return self.job._board_live_progress()

    def _events(self, ev_type):
        return self.env["prema.dispatch.timeline.event"].search(
            [("job_id", "=", self.job.id),
             ("event_type", "=", ev_type)])

    def _confirm_actuals(self, actual=2, notes=""):
        return self.driver_env["prema.dispatch.job"].driver_confirm_pickup_actuals(
            self.stop.id, {
                "job_id": self.job.id,
                "actual_received_pallet_count": actual,
                "variance_notes": notes,
                "route_sheet_received": False,
            })

    def _items(self):
        return self.job.item_ids.filtered(
            lambda i: i.consumes_floor_position and i.status != "cancelled")

    def _allocate_all(self):
        for item in self._items():
            self.env["prema.dispatch.pallet.stop.allocation"].create({
                "dispatch_item_id": item.id,
                "stop_id": self.drops[0].id,
            })

    def _popp_all(self):
        for item in self._items():
            self.driver_env["prema.dispatch.job"].driver_add_evidence(
                self.stop.id, "popp", _jpeg_b64(), "popp.jpg",
                extra={"pallet_id": item.id,
                       "captured_at": "2026-08-18 09:30:00"})

    def _start_route(self):
        return self.driver_env["prema.dispatch.job"].driver_start_route(
            self.job.id)

    def _set_status(self, stop, status):
        stop.write({"status": status})

    # ── §33 LIVE PROGRESS states ───────────────────────────────

    def test_01_progress_planned_and_completed(self):
        self.assertEqual(self._progress()["key"], "planned")
        for s in self.job.stop_ids:
            self._set_status(s, "completed")
        self.assertEqual(self._progress()["key"], "completed")

    def test_02_progress_pickup_phases(self):
        self._start_route()
        self.assertEqual(self._progress()["key"], "driver_started")
        self._set_status(self.stop, "en_route")
        self.assertEqual(self._progress()["key"], "en_route_pickup")
        self._set_status(self.stop, "arrived")
        self.assertEqual(self._progress()["key"], "arrived_pickup")
        self.job.pickup_actuals_confirmed_at = fields.Datetime.now()
        self.assertEqual(self._progress()["key"], "loading")

    def test_03_progress_delivery_phases(self):
        self._start_route()
        self._set_status(self.stop, "completed")
        self.assertEqual(self._progress()["key"], "pickup_complete")
        self._set_status(self.drops[0], "en_route")
        p = self._progress()
        self.assertEqual(p["key"], "en_route_delivery")
        self.assertEqual(p["label"], "EN ROUTE TO DELIVERY 1/2")
        self._set_status(self.drops[0], "arrived")
        self.assertEqual(self._progress()["key"], "arrived_delivery")
        self._set_status(self.drops[0], "completed")
        self.assertEqual(self._progress()["key"], "delivering")
        self._set_status(self.drops[1], "completed")
        self.assertEqual(self._progress()["key"], "completed")

    # ── §32 Booking Board payload ──────────────────────────────

    def test_04_board_data_has_live_progress_no_feasibility(self):
        self._start_route()
        data = self.env["prema.dispatch.job"].get_booking_status_board_data()
        row = next(r for r in data["rows"] if r["job_id"] == self.job.id)
        self.assertIn("live_progress", row)
        self.assertIn("live_progress_label", row)
        self.assertEqual(row["live_progress"], "driver_started")
        self.assertNotIn("feasibility", row)
        self.assertNotIn("feasibility_reason", row)

    # ── §34 timeline propagation ───────────────────────────────

    def test_05_route_started_event(self):
        self._start_route()
        evs = self._events("route_started")
        self.assertEqual(len(evs), 1)
        self.assertIn("LiveSync Driver", evs.notes)

    def test_06_skipped_action_is_server_side(self):
        r = self.driver_env["prema.dispatch.job"].driver_update_stop(
            self.drops[0].id, "skipped")
        self.assertTrue(r.get("success"), r)
        self.assertEqual(self.drops[0].status, "skipped")
        self.assertEqual(len(self._events("stop_skipped")), 1)
        # idempotent guard: closed stops are rejected
        r2 = self.driver_env["prema.dispatch.job"].driver_update_stop(
            self.drops[0].id, "skipped")
        self.assertFalse(r2.get("success"))
        self.assertEqual(len(self._events("stop_skipped")), 1)

    def test_07_issue_reported_event(self):
        r = self.driver_env["prema.dispatch.job"].driver_update_stop(
            self.drops[0].id, "issue", {"delay_reason": "Breakdown on 401"})
        self.assertTrue(r.get("success"), r)
        evs = self._events("issue_reported")
        self.assertEqual(len(evs), 1)
        self.assertIn("Breakdown on 401", evs.notes)

    def test_08_pickup_confirmed_event_after_gate(self):
        # Gate blocks until pallets are assigned + POPP'd (no variance
        # here: actual == expected).
        first = self._confirm_actuals(actual=2)
        self.assertFalse(first.get("success"))
        self.assertEqual(first.get("code"), "pickup_gate_blocked")
        self._allocate_all()
        self._popp_all()
        self.assertEqual(len(self._events("pallet_assigned")), len(self._items()))
        second = self._confirm_actuals(actual=2)
        self.assertTrue(second.get("success"), second)
        evs = self._events("pickup_confirmed")
        self.assertEqual(len(evs), 1)
        self.assertIn("Actual pallets 2 confirmed", evs.notes)

    def test_09_evidence_events(self):
        self.driver_env["prema.dispatch.job"].driver_add_evidence(
            self.stop.id, "pod", _jpeg_b64(color=(10, 20, 30)), "pod.jpg",
            extra={"captured_at": "2026-08-18 10:00:00"})
        evs = self._events("pod_uploaded")
        self.assertEqual(len(evs), 1)
        self.assertIn("POD photo", evs.notes)

    def test_10_document_scanned_event(self):
        for idx in (0, 1):
            self.driver_env["prema.dispatch.job"].driver_add_evidence(
                self.stop.id, "scan", _jpeg_b64(color=(idx * 40 + 5, 20, 30)),
                f"page{idx}.jpg",
                extra={"scan_session": "SESS-1", "scan_page_index": idx,
                       "captured_at": "2026-08-18 10:05:00"})
        r = self.driver_env["prema.dispatch.job"].driver_complete_scan(
            self.stop.id, "pod", "SESS-1")
        self.assertTrue(r.get("success"), r)
        self.assertEqual(r.get("pages"), 2)
        evs = self._events("document_scanned")
        self.assertEqual(len(evs), 1)
        self.assertIn("2 page(s) merged", evs.notes)

    def test_11_day_ended_event(self):
        Workday = self.env["prema.dispatch.driver.workday"]
        wd = Workday._get_or_create_for(
            self.driver_partner.id, fields.Date.today())
        for s in self.job.stop_ids:
            self._set_status(s, "completed")
        r = self.driver_env["prema.dispatch.driver.workday"].browse(
            wd.id).action_end_day()
        self.assertTrue(r.get("success"), r)
        evs = self._events("day_ended")
        self.assertEqual(len(evs), 1)
        self.assertIn("Workday ended", evs.notes)
        self.assertEqual(wd.state, "completed")

# -*- coding: utf-8 -*-
"""18-section work order §13: Evidence upload pipeline.

Server-side failed-state persistence + retry by idempotency_key:

- every capture carries a client idempotency_key; a retry with the same
  key REPLAYS the existing record instead of duplicating (§57)
- when the server-side flow throws after the row committed, the row is
  marked upload_state='failed' — the dispatch panel surfaces it and a
  keyed retry RESUMES it
- dispatch evidence search: the mislabeled "No GPS" filter (named
  f_pending) is replaced with honest Pending/Failed, Superseded and
  No GPS filters; the list/form surface the §12 envelope
- customers see completed-only: tracking-page photos use
  _customer_visible_domain (uploaded, never superseded)

Run: --test-tags /prema_logistics_booking/tests/test_phase13_upload_pipeline
"""
import datetime

from unittest.mock import patch

from odoo.tests import TransactionCase

from .test_phase12_evidence_relationships import (
    PHOTO_A, PHOTO_B, _real_jpeg_b64)

PHOTO_C = _real_jpeg_b64((60, 30, 120))


class TestPhase13UploadPipeline(TransactionCase):
    """§13 idempotency + failed-state upload pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P13 Customer"})
        cls.driver = env["res.partner"].create({"name": "P13 Driver"})
        cls.driver_user = env["res.users"].create({
            "name": "p13driver", "login": "p13driver@test.local",
            "partner_id": cls.driver.id, "tz": "UTC",
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id,
                env.ref("prema_dispatch.group_dispatch_driver").id,
            ])],
        })

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"404 P13 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _job(self):
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 3),
            "estimated_delivery_date": datetime.date(2026, 9, 3),
            "price_snapshot": [{
                "line": "P13 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P13 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P13 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        job.write({"driver_id": self.driver.id})
        return job

    def _upload(self, job, ev_type, photo=PHOTO_A, extra=None):
        return job.with_user(self.driver_user).driver_add_evidence(
            job.stop_ids[0].id, ev_type, photo, "p13.jpg",
            extra=extra or {})

    def _rows_for_key(self, key):
        return self.env["prema.dispatch.evidence"].sudo().search(
            [("idempotency_key", "=", key)])

    def test_a_replay_same_key_never_duplicates(self):
        """Two uploads sharing one idempotency_key yield ONE evidence row;
        the retry replays the first record (duplicate=True) — even with
        different bytes, because the replay precedes checksum dedup."""
        job = self._job()
        r1 = self._upload(job, "pop", extra={"idempotency_key": "k13-a"})
        self.assertTrue(r1.get("success"))
        r2 = self._upload(job, "pop", photo=PHOTO_B,
                          extra={"idempotency_key": "k13-a"})
        self.assertTrue(r2.get("success"))
        self.assertTrue(r2.get("duplicate"))
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r2["id"], r1["id"])
        rows = self._rows_for_key("k13-a")
        self.assertEqual(len(rows), 1)

    def test_b_failed_row_resumed_by_keyed_retry(self):
        """A row persisted in the failed state (upload_state='failed') is
        RESUMED by a retry carrying the same key: marked uploaded, the
        record replayed, no second row."""
        job = self._job()
        r1 = self._upload(job, "pop", extra={"idempotency_key": "k13-b"})
        self.assertTrue(r1.get("success"))
        row = self._rows_for_key("k13-b")
        row.write({"upload_state": "failed"})  # simulates the §57 mark
        r2 = self._upload(job, "pop", photo=PHOTO_B,
                          extra={"idempotency_key": "k13-b"})
        self.assertTrue(r2.get("success"))
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r2["id"], r1["id"])
        self.assertEqual(len(self._rows_for_key("k13-b")), 1)
        self.assertEqual(
            self._rows_for_key("k13-b").upload_state, "uploaded")

    def test_c_server_exception_marks_failed_then_retry_resumes(self):
        """Real exception path: the flow throws AFTER the row committed
        (mocked feed emit). The keyed row lands in the failed state and a
        later retry with the same key resumes it to uploaded."""
        job = self._job()
        with patch.object(type(job), "_emit_feed",
                          side_effect=RuntimeError("feed down")):
            r1 = self._upload(job, "pop",
                              extra={"idempotency_key": "k13-c"})
            self.assertFalse(r1.get("success"))
            self.assertEqual(r1.get("code"), "upload_failed")
        row = self._rows_for_key("k13-c")
        self.assertEqual(len(row), 1, "row persisted despite the failure")
        self.assertEqual(row.upload_state, "failed")
        self.assertTrue(row.attachment_id.exists())
        # Feed restored → the driver's Retry with the same key resumes.
        r2 = self._upload(job, "pop", photo=PHOTO_B,
                          extra={"idempotency_key": "k13-c"})
        self.assertTrue(r2.get("success"))
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(len(self._rows_for_key("k13-c")), 1)
        self.assertEqual(
            self._rows_for_key("k13-c").upload_state, "uploaded")

    def test_d_keyless_uploads_untouched(self):
        """Uploads without a key keep working exactly as before — no
        replay, no failed-marking, plain duplicate-by-checksum."""
        job = self._job()
        r1 = self._upload(job, "pop")
        r2 = self._upload(job, "pop")  # same bytes → checksum duplicate
        self.assertTrue(r2.get("success"))
        self.assertTrue(r2.get("duplicate"))
        self.assertFalse(r2.get("replayed"))
        self.assertEqual(r2["id"], r1["id"])
        self.assertEqual(len(job.stop_ids[0].pop_attachment_ids), 1)

    def test_e_customer_sees_completed_only(self):
        """_customer_visible_domain returns ONLY live, uploaded proof —
        failed rows and superseded (retaken) photos never reach the
        customer's tracking page."""
        job = self._job()
        item = job.item_ids[0]
        live = self._upload(job, "popp", extra={
            "pallet_id": item.id, "idempotency_key": "k13-e1"})
        self.assertTrue(live.get("success"))
        retake = self._upload(job, "popp", photo=PHOTO_B, extra={
            "pallet_id": item.id, "idempotency_key": "k13-e2",
            "supersedes_att_id": live["id"]})
        self.assertTrue(retake.get("success"))
        failed = self._upload(job, "popp", photo=PHOTO_C, extra={
            "pallet_id": item.id, "idempotency_key": "k13-e3"})
        self.env["prema.dispatch.evidence"].sudo().browse(
            failed["evidence_id"]).write({"upload_state": "failed"})

        Evidence = self.env["prema.dispatch.evidence"]
        visible = Evidence.sudo().search(
            Evidence._customer_visible_domain(
                job=job, evidence_type="popp", pallet_id=item),
            order="captured_at asc, id asc")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible.id, retake["evidence_id"],
                         "only the live retake photo is customer-visible")

    def test_f_search_filters_fixed(self):
        """The mislabeled 'No GPS' f_pending filter is replaced by honest
        Pending/Failed, Superseded and No GPS filters in the search view."""
        view = self.env.ref("prema_dispatch.view_dispatch_evidence_search")
        arch = view.arch
        self.assertIn('name="f_failed"', arch)
        self.assertIn("Pending / Failed Uploads", arch)
        self.assertIn('name="f_superseded"', arch)
        self.assertIn("Superseded (Retaken)", arch)
        self.assertIn('name="f_no_gps"', arch)
        self.assertIn("No GPS", arch)
        self.assertNotIn('name="f_pending"', arch,
                         "the mislabeled f_pending filter must be gone")

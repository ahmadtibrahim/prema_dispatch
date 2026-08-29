# -*- coding: utf-8 -*-
"""18-section work order §12: Evidence relationships.

The canonical prema.dispatch.evidence row gains the §12 envelope —
load_plan_id, upload_state, idempotency_key, original_filename,
uploaded_at, gps_accuracy_m, captured_tz — and the spec §55 retake flow
finally WRITES superseded_by_id: retaking a photo uploads the replacement
first, the server points the old record at it and keeps BOTH for audit
(old attachment leaves the driver-visible buckets + invoice copy, the row
survives so live-map/dispatch count it as superseded, never as live
proof).

Run: --test-tags /prema_logistics_booking/tests/test_phase12_evidence_relationships
"""
import base64
import datetime
import io

from odoo.tests import TransactionCase

# decode_and_validate verifies JPEG content with Pillow (not just magic
# bytes) — a real tiny JPEG must be generated, not faked.
from PIL import Image as _PILImage


def _real_jpeg_b64(color=(120, 60, 30)):
    buf = io.BytesIO()
    _PILImage.new("RGB", (4, 4), color).save(buf, format="JPEG")
    # Mirror the real app: JSON delivers the payload as a str.
    return base64.b64encode(buf.getvalue()).decode("ascii")


# A retake is a NEW photo (new bytes): a re-upload of the SAME file is a
# duplicate by design (find_duplicate short-circuits before the supersede
# wiring). Tests must use distinct captures per retake step.
PHOTO_A = _real_jpeg_b64((120, 60, 30))
PHOTO_B = _real_jpeg_b64((30, 120, 60))
PHOTO_C = _real_jpeg_b64((60, 30, 120))
PHOTO_D = _real_jpeg_b64((200, 200, 20))


class TestPhase12EvidenceRelationships(TransactionCase):
    """§12 evidence relationships + retake supersession."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P12 Customer"})
        cls.driver = env["res.partner"].create({"name": "P12 Driver"})
        cls.driver_user = env["res.users"].create({
            "name": "p12driver", "login": "p12driver@test.local",
            "partner_id": cls.driver.id, "tz": "UTC",
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id,
                env.ref("prema_dispatch.group_dispatch_driver").id,
            ])],
        })
        brand = env["fleet.vehicle.model.brand"].create({"name": "P12 Brand"})
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P12 Truck Model", "brand_id": brand.id})
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P12-TRUCK-01", "license_plate": "P12-0001",
            "odometer_unit": "kilometers", "power_unit": "power",
            "model_id": vehicle_model.id,
        })
        cls.layout = env["prema.dispatch.vehicle.layout.template"].create({
            "name": "P12 Layout", "layout_type": "straight",
        })

    @classmethod
    def _location(cls, name):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        return cls.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"303 P12 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _booking(self):
        return self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 2),
            "estimated_delivery_date": datetime.date(2026, 9, 2),
            "price_snapshot": [{
                "line": "P12 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })

    def _job(self):
        """Assigned driver job with a pickup + delivery stop and freight."""
        booking = self._booking()
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P12 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P12 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        job.write({"driver_id": self.driver.id,
                   "vehicle_id": self.vehicle.id})
        return job

    def _upload(self, job, ev_type, photo=PHOTO_A, filename="p12.jpg",
                extra=None, user=None):
        """Driver upload through the real driver_add_evidence path."""
        r = job.with_user(user or self.driver_user).driver_add_evidence(
            job.stop_ids[0].id, ev_type, photo, filename, extra=extra or {})
        if not r.get("success"):
            print("UPLOAD_FAILED", r)
        return r

    def _evidence_of(self, attachment_id):
        return self.env["prema.dispatch.evidence"].sudo().search(
            [("attachment_id", "=", attachment_id)], limit=1)

    def test_a_meta_envelope_persisted(self):
        """The §12 envelope lands on the canonical row: idempotency key,
        original filename, upload time, GPS accuracy, capture TZ — and a
        failed-state row can be persisted with the same key (spec §57)."""
        job = self._job()
        r = self._upload(job, "pop", extra={
            "captured_at": "2026-09-02T13:05:00.000Z",
            "idempotency_key": "k-abc-001",
            "original_filename": "IMG_001.JPG",
            "uploaded_at": "2026-09-02T13:05:03.000Z",
            "gps_accuracy_m": 12.5,
            "captured_tz": "America/Toronto",
            "lat": 43.65, "lng": -79.38,
        })
        self.assertTrue(r.get("success"))
        ev = self._evidence_of(r["id"])
        self.assertEqual(ev.upload_state, "uploaded")
        self.assertEqual(ev.idempotency_key, "k-abc-001")
        self.assertEqual(ev.original_filename, "IMG_001.JPG")
        self.assertEqual(ev.gps_accuracy_m, 12.5)
        self.assertEqual(ev.captured_tz, "America/Toronto")
        self.assertEqual(ev.captured_at, datetime.datetime(2026, 9, 2, 13, 5))
        self.assertEqual(ev.uploaded_at, datetime.datetime(2026, 9, 2, 13, 5, 3))
        # §57: a server-persisted FAILED row keeps the key for retry dedupe.
        failed = self.env["prema.dispatch.evidence"]._create_evidence(
            self.env["ir.attachment"].create({
                "name": "lost.jpg", "type": "binary",
                "datas": PHOTO_A,
                "res_model": "prema.dispatch.stop", "res_id": job.stop_ids[0].id,
            }), job.stop_ids[0], "pop_general", {
                "idempotency_key": "k-abc-001",
                "upload_state": "failed",
                "original_filename": "IMG_001.JPG",
            })
        self.assertEqual(failed.upload_state, "failed")
        self.assertEqual(failed.idempotency_key, "k-abc-001")

    def test_b_load_plan_derived(self):
        """Evidence captured under a load plan links to it automatically
        (via the job's load-plan membership); a plan-less job stays
        unlinked."""
        job = self._job()
        plan = self.env["prema.dispatch.load.plan"].create({
            "operating_date": datetime.date(2026, 9, 2),
            "vehicle_id": self.vehicle.id,
            "layout_template_id": self.layout.id,
        })
        self.env["prema.dispatch.load.plan.job"].create({
            "load_plan_id": plan.id, "job_id": job.id,
        })
        r = self._upload(job, "pop")
        self.assertEqual(self._evidence_of(r["id"]).load_plan_id.id, plan.id)
        job2 = self._job()
        r2 = self._upload(job2, "pop")
        self.assertFalse(self._evidence_of(r2["id"]).load_plan_id)

    def test_c_retake_supersedes_pop(self):
        """Spec §55: retake keeps the old record + attachment for audit,
        points old.superseded_by_id at the replacement, and drops the old
        file from the stop's driver-visible bucket."""
        job = self._job()
        pickup = job.stop_ids[0]
        r_a = self._upload(job, "pop")
        self.assertIn(r_a["id"], pickup.pop_attachment_ids.ids)
        # The driver retakes: replacement upload names the old attachment.
        r_b = self._upload(job, "pop", photo=PHOTO_B, extra={
            "supersedes_att_id": r_a["id"]})
        self.assertTrue(r_b.get("success"))
        self.assertEqual(r_b.get("supersedes_attachment_id"), r_a["id"])
        ev_a = self._evidence_of(r_a["id"])
        ev_b = self._evidence_of(r_b["id"])
        self.assertTrue(ev_a.exists(), "old record kept for audit")
        self.assertEqual(ev_a.superseded_by_id.id, ev_b.id)
        self.assertFalse(ev_b.superseded_by_id)
        self.assertTrue(ev_a.attachment_id.exists(),
                        "old attachment file kept for audit")
        self.assertNotIn(r_a["id"], pickup.pop_attachment_ids.ids,
                         "old file leaves the driver-visible bucket")
        self.assertIn(r_b["id"], pickup.pop_attachment_ids.ids)

    def test_d_retake_supersedes_popp(self):
        """Same contract for per-pallet POPP photos: old row superseded,
        old attachment leaves the pallet bucket, new photo lives on."""
        job = self._job()
        item = job.item_ids[0]
        extra_popp = {"pallet_id": item.id}
        r_a = self._upload(job, "popp", extra=extra_popp)
        self.assertIn(r_a["id"], item.popp_attachment_ids.ids)
        r_b = self._upload(job, "popp", photo=PHOTO_B, extra={
            "pallet_id": item.id, "supersedes_att_id": r_a["id"]})
        self.assertTrue(r_b.get("success"))
        ev_a = self._evidence_of(r_a["id"])
        ev_b = self._evidence_of(r_b["id"])
        self.assertEqual(ev_a.superseded_by_id.id, ev_b.id)
        self.assertNotIn(r_a["id"], item.popp_attachment_ids.ids)
        self.assertIn(r_b["id"], item.popp_attachment_ids.ids)
        self.assertTrue(ev_a.attachment_id.exists())

    def test_e_foreign_or_missing_supersede_ignored(self):
        """A supersedes_att_id naming another job's attachment — or a
        nonexistent id — degrades to a plain upload: success, nothing
        superseded."""
        job1 = self._job()
        job2 = self._job()
        r_a = self._upload(job1, "pop")
        r_b = self._upload(job2, "pop", extra={
            "supersedes_att_id": r_a["id"]})  # belongs to job1
        self.assertTrue(r_b.get("success"))
        self.assertFalse(r_b.get("supersedes_attachment_id"))
        self.assertFalse(self._evidence_of(r_a["id"]).superseded_by_id,
                         "foreign attachment never superseded")
        r_c = self._upload(job2, "pop", extra={"supersedes_att_id": 999999})
        self.assertTrue(r_c.get("success"))
        self.assertFalse(self._evidence_of(r_c["id"]).superseded_by_id)

    def test_f_supersede_chain_is_stable(self):
        """Retake-of-retake builds a one-way audit chain; every old row
        points at exactly its replacement and nothing re-points."""
        job = self._job()
        r_a = self._upload(job, "pop")
        r_b = self._upload(job, "pop", photo=PHOTO_B, extra={
            "supersedes_att_id": r_a["id"]})
        r_c = self._upload(job, "pop", extra={
            "supersedes_att_id": r_b["id"]})
        ev_a, ev_b, ev_c = (self._evidence_of(i) for i in
                            (r_a["id"], r_b["id"], r_c["id"]))
        self.assertEqual(ev_a.superseded_by_id.id, ev_b.id)
        self.assertEqual(ev_b.superseded_by_id.id, ev_c.id)
        self.assertFalse(ev_c.superseded_by_id)
        # Re-uploading the FIRST file is now a duplicate of itself (still
        # in the bucket? no — it left; the checksum matches nothing live,
        # so it uploads fresh and must NOT touch the chain).
        r_d = self._upload(job, "pop", extra={
            "supersedes_att_id": ev_a.attachment_id.id})
        self.assertTrue(r_d.get("success"))
        self.assertFalse(self._evidence_of(r_d["id"]).superseded_by_id)
        # ev_a already superseded: its link never changes, its attachment
        # never re-enters the bucket.
        ev_a = self._evidence_of(r_a["id"])
        self.assertEqual(ev_a.superseded_by_id.id, ev_b.id)

    def test_g_payload_exposes_relationship_fields(self):
        """_payload exposes the §12 envelope so pending/failed panels and
        dispatcher views render relationship data without re-reading."""
        job = self._job()
        r = self._upload(job, "pop", extra={
            "idempotency_key": "k-abc-009",
            "original_filename": "IMG_009.JPG",
            "gps_accuracy_m": 8.0,
            "captured_tz": "America/Toronto",
        })
        payload = self._evidence_of(r["id"])._payload()
        self.assertEqual(payload["idempotency_key"], "k-abc-009")
        self.assertEqual(payload["original_filename"], "IMG_009.JPG")
        self.assertEqual(payload["gps_accuracy_m"], 8.0)
        self.assertEqual(payload["captured_tz"], "America/Toronto")
        self.assertEqual(payload["upload_state"], "uploaded")
        self.assertIn("superseded_by_id", payload)
        self.assertIn("load_plan_id", payload)
        self.assertIn("invoice_id", payload)

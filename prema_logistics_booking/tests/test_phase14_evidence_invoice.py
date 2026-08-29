# -*- coding: utf-8 -*-
"""18-section work order §14: Evidence + deferred invoicing.

prema.dispatch.evidence.invoice_id is finally WRITTEN:

- upload-time: driver_add_evidence / driver_complete_scan → the same-
  customer DRAFT gate that produces the invoice copy also links the
  canonical row (idempotent — never rewrites an existing link)
- completion-time: _attach_documents_to_invoice fills invoice_id for live
  evidence that predates the deferred invoice (uploaded, not superseded,
  not failed), resolving stop-level invoices first with the same
  same-customer DRAFT gate per row (consolidated multi-invoice jobs)
- cross-customer stop invoices never receive another customer's proof —
  the stop-level invoice is the resolution target (shadowing the job
  invoice), and when it fails the same-customer gate the evidence is
  linked to NOTHING, at upload and at completion
- repeated completions never re-link, never duplicate copies

Run: --test-tags /prema_logistics_booking/tests/test_phase14_evidence_invoice
"""
import base64
import datetime
import io

from odoo.tests import TransactionCase

from PIL import Image as _PILImage

from .test_phase12_evidence_relationships import PHOTO_A, PHOTO_B


def _jpeg_b64(color=(90, 90, 90)):
    buf = io.BytesIO()
    _PILImage.new("RGB", (4, 4), color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


PHOTO_C = _jpeg_b64((40, 40, 40))


class TestPhase14EvidenceInvoice(TransactionCase):
    """§14 evidence ↔ deferred invoice linking."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner = env["res.partner"].create({"name": "P14 Customer"})
        cls.other = env["res.partner"].create({"name": "P14 Other Customer"})
        cls.driver = env["res.partner"].create({"name": "P14 Driver"})
        cls.driver_user = env["res.users"].create({
            "name": "p14driver", "login": "p14driver@test.local",
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
            "address": f"505 P14 Ave #{cls._loc_n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _invoice(self, partner, name="P14-INV"):
        return self.env["account.move"].sudo().create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_date": datetime.date(2026, 9, 4),
            "ref": name,
            "invoice_line_ids": [(0, 0, {
                "name": "Freight P14",
                "quantity": 1,
                "price_unit": 100.0,
            })],
        })

    def _job(self, partner=None):
        booking = self.env["logistics.booking"].create({
            "partner_id": (partner or self.partner).id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 4),
            "estimated_delivery_date": datetime.date(2026, 9, 4),
            "price_snapshot": [{
                "line": "P14 test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        })
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": self._location("P14 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": self._location("P14 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        job = booking._create_dispatch_job()
        job.write({"driver_id": self.driver.id})
        return job

    def _upload(self, job, ev_type, photo=PHOTO_A, extra=None):
        return job.with_user(self.driver_user).driver_add_evidence(
            job.stop_ids[0].id, ev_type, photo, "p14.jpg",
            extra=extra or {})

    def _ev(self, r):
        return self.env["prema.dispatch.evidence"].sudo().search(
            [("attachment_id", "=", r["id"])], limit=1)

    def _invoice_copies(self, inv, att):
        return self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", inv.id),
            ("description", "=", f"__evidence_source:{att.id}__"),
        ])

    def test_a_upload_time_link_when_invoice_exists(self):
        """Upload under an existing same-customer draft invoice links the
        canonical row at upload time."""
        job = self._job()
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        r = self._upload(job, "pop")
        ev = self._ev(r)
        self.assertEqual(ev.invoice_id.id, inv.id)
        self.assertEqual(len(self._invoice_copies(inv, ev.attachment_id)), 1)

    def test_b_deferred_upload_has_no_link(self):
        """Before the deferred invoice exists the row stays unlinked;
        completion-time fill links it."""
        job = self._job()
        r = self._upload(job, "pop")
        ev = self._ev(r)
        self.assertFalse(ev.invoice_id)
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        job._attach_documents_to_invoice()
        self.assertEqual(ev.invoice_id.id, inv.id)

    def test_c_completion_fills_only_live_evidence(self):
        """Completion links uploaded-but-unlinked LIVE rows only —
        failed and superseded (retaken) proof is never linked."""
        job = self._job()
        item = job.item_ids[0]
        r_live = self._upload(job, "pop")
        r_popp = self._upload(job, "popp", extra={"pallet_id": item.id})
        r_retaken = self._upload(job, "pop", photo=PHOTO_B, extra={
            "supersedes_att_id": r_live["id"]})
        # Force a failed-state row (simulating §57 persistence).
        r_failed = self._upload(job, "pop", photo=PHOTO_C)
        self.env["prema.dispatch.evidence"].sudo().browse(
            r_failed["evidence_id"]).write({"upload_state": "failed"})

        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        job._attach_documents_to_invoice()

        ev_retaken = self._ev(r_retaken)
        ev_popp = self._ev(r_popp)
        ev_failed = self._ev(r_failed)
        self.assertEqual(ev_retaken.invoice_id.id, inv.id,
                         "the live retake photo IS linked")
        self.assertEqual(ev_popp.invoice_id.id, inv.id,
                         "POPP proof is linked too (it was copied at upload)")
        self.assertEqual(self._ev(r_retaken).invoice_id.id, inv.id)
        self.assertFalse(ev_failed.invoice_id,
                         "failed evidence is never linked")
        # The superseded row (the old photo) must NOT be linked — it was
        # replaced, and its invoice copy was removed at retake.
        old = self.env["prema.dispatch.evidence"].sudo().search(
            [("attachment_id", "=", r_live["id"])], limit=1)
        self.assertFalse(old.invoice_id)

    def test_d_cross_customer_stop_invoice_never_linked(self):
        """A consolidated stop whose invoice belongs to ANOTHER customer
        never receives the evidence link nor an attachment copy — the
        same-customer DRAFT gate holds at upload AND completion.

        The stop-level invoice is the resolution target (mirroring the
        upload-time `stop.invoice_id or job.invoice_id` rule); because it
        fails the gate the evidence is linked to NOTHING — a cross-customer
        stop invoice shadows the job invoice, so no fallback link happens
        and the job's own invoice never receives this evidence either."""
        job = self._job()
        inv_other = self._invoice(self.other, "P14-OTHER-INV")
        job.stop_ids[0].write({"invoice_id": inv_other.id})
        r = self._upload(job, "pop")
        ev = self._ev(r)
        self.assertFalse(ev.invoice_id,
                         "cross-customer invoice must not be linked")
        self.assertEqual(len(self._invoice_copies(inv_other, ev.attachment_id)),
                         0, "no copy may land on the other customer's invoice")
        # Completion-time pass respects the same gate AND the same
        # stop-first resolution: the job's same-customer invoice is NOT
        # used as a fallback while the stop names its own invoice.
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        job._attach_documents_to_invoice()
        self.assertFalse(self._ev(r).invoice_id,
                         "stop-invoice shadows the job invoice — still no "
                         "link, even though a same-customer job invoice "
                         "exists")
        self.assertEqual(len(self._invoice_copies(inv, ev.attachment_id)), 0,
                         "no tagged copy on the job invoice either")

    def test_e_idempotent_completion(self):
        """Repeated completion never re-links, never duplicates copies and
        never creates a second invoice link.

        Completion-time copies are NAME-based (the _link_attachment
        scheme, `{inv_ref}_{job_ref}_STOP{n}_PICKUP_PROOF.{ext}`) — the
        description tag is the upload-time _copy_evidence_to_invoice
        scheme — so the duplicate check counts invoice attachments."""
        job = self._job()
        r = self._upload(job, "pop")
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        first = job._attach_documents_to_invoice()
        self.assertEqual(first, 1)
        ev = self._ev(r)
        linked_inv = ev.invoice_id.id
        self.assertEqual(linked_inv, inv.id,
                         "completion linked the deferred evidence")
        second = job._attach_documents_to_invoice()
        self.assertEqual(second, 0, "no duplicate copies on repeat")
        self.assertEqual(ev.invoice_id.id, linked_inv,
                         "link never rewritten")
        inv_atts = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", inv.id),
        ])
        self.assertEqual(len(inv_atts), 1,
                         "exactly one proof copy on the invoice")

    def test_f_repeat_upload_same_key_keeps_single_link(self):
        """A keyed retry (replay) after the first attempt committed keeps
        exactly one link — the replayed row is the same row."""
        job = self._job()
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        r1 = self._upload(job, "pop", extra={"idempotency_key": "k14-f"})
        r2 = self._upload(job, "pop", photo=PHOTO_B,
                          extra={"idempotency_key": "k14-f"})
        self.assertTrue(r2.get("replayed"))
        rows = self.env["prema.dispatch.evidence"].sudo().search(
            [("idempotency_key", "=", "k14-f")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.invoice_id.id, inv.id)
        self.assertEqual(
            len(self._invoice_copies(inv, rows.attachment_id)), 1,
            "one copy — the retry never duplicated the invoice copy")

    def test_g_scanned_merge_links_invoice(self):
        """The merged scan PDF (driver_complete_scan) is linked to the
        same-customer draft invoice like any other upload."""
        job = self._job()
        inv = self._invoice(self.partner)
        job.write({"invoice_id": inv.id})
        stop = job.stop_ids[0]
        # Two scan pages (real JPEGs) in one session.
        session = "p14-scan-session"
        Evidence = self.env["prema.dispatch.evidence"]
        for idx, color in enumerate([(10, 20, 30), (30, 40, 50)]):
            att = self.env["ir.attachment"].sudo().create({
                "name": f"page{idx}.jpg", "type": "binary",
                "datas": _jpeg_b64(color),
                "res_model": "prema.dispatch.stop", "res_id": stop.id,
            })
            Evidence.sudo()._create_evidence(att, stop, "scan_page", {
                "scan_session": session, "scan_page_index": idx,
            })
        r = job.with_user(self.driver_user).driver_complete_scan(
            stop.id, "pop", session)
        self.assertTrue(r.get("success"), r)
        merged = Evidence.sudo().search([
            ("stop_id", "=", stop.id),
            ("evidence_type", "in", ("scanned_pop", "scanned_pod")),
        ], limit=1)
        self.assertTrue(merged.exists())
        self.assertEqual(merged.invoice_id.id, inv.id)
        self.assertEqual(len(self._invoice_copies(inv, merged.attachment_id)), 1)

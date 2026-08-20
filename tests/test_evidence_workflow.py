"""Phase 3 — Evidence workflow (spec §15-17, §35-37, §55-57).

Canonical evidence records, camera-metadata capture, scanner page
handling (held separately until the session is merged into ONE PDF),
delete/retake supersession, and invoice-copy synchronization.

The stamp itself is client-side (canvas burn-in) and covered by manual
UAT; these tests cover the server contract it relies on.
"""
import base64
import io
import re

from odoo.tests import TransactionCase, tagged

try:
    from PIL import Image
except ImportError:
    Image = None


def _jpeg_b64(color=(200, 120, 40), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


@tagged("post_install", "-at_install")
class TestEvidenceWorkflow(TransactionCase):

    def setUp(self):
        super().setUp()
        customer = self.env["res.partner"].create({
            "name": "Evidence Test Customer",
            "is_company": True,
        })
        self.customer = customer
        driver_partner = self.env["res.partner"].create({
            "name": "Ev Driver Partner",
        })
        self.driver_partner = driver_partner
        self.driver_user = self.env["res.users"].create({
            "name": "Ev Driver",
            "login": f"evdriver{self.env['ir.sequence'].next_by_code('test') or '1'}",
            "partner_id": driver_partner.id,
            "groups_id": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("prema_dispatch.group_dispatch_driver").id),
            ],
        })
        self.driver_env = self.env(user=self.driver_user.id)
        stage = self.env["prema.dispatch.stage"].search([("is_booking_phase", "=", True)], limit=1)
        if not stage:
            stage = self.env["prema.dispatch.stage"].create({
                "name": "Ev Booking",
                "is_booking_phase": True,
            })
        self.stage = stage
        job = self.env["prema.dispatch.job"].create({
            "name": "EVJOB-0001",
            "partner_id": customer.id,
            "stage_id": stage.id,
            "driver_id": driver_partner.id,
            "scheduled_pickup": "2026-08-18 14:00:00",
            "approximate_skids": 2,
        })
        self.job = job
        self.stop = self.env["prema.dispatch.stop"].create({
            "job_id": job.id,
            "stop_type": "pickup",
            "sequence": 10,
            "name": "Terra Freska Produce",
            "pop_required": True,
            "scheduled_time": "2026-08-18 14:00:00",
        })

    # ── helpers ────────────────────────────────────────────────

    def _add(self, ev_type, b64, filename="photo.jpg", extra=None, user=None):
        env = user or self.driver_env
        return env["prema.dispatch.job"].driver_add_evidence(
            self.stop.id, ev_type, b64, filename, extra=extra)

    def _draft_invoice(self):
        return self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.customer.id,
            "invoice_date": "2026-08-18",
        })

    # ── §16 metadata + canonical record ────────────────────────

    def test_01_pop_upload_creates_canonical_evidence_record(self):
        r = self._add("pop", _jpeg_b64(), "pop1.jpg", extra={
            "captured_at": "2026-08-18 14:05:11",
            "lat": 43.768431, "lng": -79.619128,
            "device": "test-driver-app",
        })
        self.assertTrue(r.get("success"), r)
        ev = self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])], limit=1)
        self.assertTrue(ev)
        self.assertEqual(ev.evidence_type, "pop_general")
        self.assertEqual(ev.stop_id, self.stop)
        self.assertEqual(ev.job_id, self.job)
        self.assertEqual(ev.driver_id, self.driver_user)
        self.assertEqual(ev.lat, 43.768431)
        self.assertEqual(ev.lng, -79.619128)
        self.assertIn("14:05:11", ev.captured_at.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(ev.device, "test-driver-app")
        self.assertTrue(ev.checksum_sha256)
        # canonical record links the same attachment the stop m2m holds
        self.assertIn(r["id"], self.stop.pop_attachment_ids.ids)

    def test_01b_iso8601_captured_at_accepted(self):
        # The app sends captured_at via new Date().toISOString()
        # ("2026-08-20T00:53:35.722Z") — the ORM Datetime field only
        # accepts "YYYY-MM-DD HH:MM:SS". Before the normalization in
        # _create_evidence every upload raised ValueError, the generic
        # catch returned success=False (while the row still committed),
        # and the client never attached the photo — the guided pickup
        # gate could not advance. Found in v7 browser UAT.
        r = self._add("pop", _jpeg_b64(), "iso.jpg", extra={
            "captured_at": "2026-08-20T00:53:35.722Z",
        })
        self.assertTrue(r.get("success"), r)
        ev = self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])], limit=1)
        self.assertTrue(ev)
        self.assertEqual(ev.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
                         "2026-08-20 00:53:35")

    def test_02_scan_page_held_apart_from_proof(self):
        r = self._add("scan", _jpeg_b64(), "page1.jpg", extra={
            "scan_session": "s_123", "scan_page_index": 0,
            "captured_at": "2026-08-18 14:06:00",
        })
        self.assertTrue(r.get("success") and r.get("page"), r)
        ev = self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])], limit=1)
        self.assertEqual(ev.evidence_type, "scan_page")
        self.assertEqual(ev.scan_session, "s_123")
        self.assertEqual(ev.scan_page_index, 0)
        # A page must NOT satisfy pop proof and must NOT appear in the m2m
        self.assertNotIn(r["id"], self.stop.pop_attachment_ids.ids)

    def test_03_scan_complete_merges_pages_into_one_pdf(self):
        self._add("scan", _jpeg_b64((200, 0, 0)), "p1.jpg",
                  extra={"scan_session": "sessA", "scan_page_index": 0})
        self._add("scan", _jpeg_b64((0, 0, 200)), "p2.jpg",
                  extra={"scan_session": "sessA", "scan_page_index": 1})
        # an unrelated session must not be swept in
        self._add("scan", _jpeg_b64((0, 200, 0)), "other.jpg",
                  extra={"scan_session": "sessB", "scan_page_index": 0})
        r = self.driver_env["prema.dispatch.job"].driver_complete_scan(
            self.stop.id, "pop", "sessA")
        self.assertTrue(r.get("success"), r)
        self.assertEqual(r["pages"], 2)
        # one PDF in the proof bucket with the spec filename shape
        self.assertEqual(len(self.stop.pop_attachment_ids), 1)
        pdf = self.stop.pop_attachment_ids[0]
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertRegex(pdf.name, r"^POP-.*-\d{8}-\d{4}\.pdf$")
        self.assertTrue(base64.b64decode(pdf.datas).startswith(b"%PDF"))
        # page attachments removed; sessB page untouched
        self.assertFalse(self.env["prema.dispatch.evidence"].search(
            [("scan_session", "=", "sessA"), ("evidence_type", "=", "scan_page")]))
        self.assertTrue(self.env["prema.dispatch.evidence"].search(
            [("scan_session", "=", "sessB")]))
        # canonical record says scanned_pop and links the merged attachment
        ev = self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", pdf.id)], limit=1)
        self.assertEqual(ev.evidence_type, "scanned_pop")
        self.assertEqual(ev.stop_id, self.stop)

    def test_04_scan_complete_copies_to_draft_invoice(self):
        inv = self._draft_invoice()
        self.job.invoice_id = inv.id
        self._add("scan", _jpeg_b64(), "p1.jpg",
                  extra={"scan_session": "sessC", "scan_page_index": 0})
        r = self.driver_env["prema.dispatch.job"].driver_complete_scan(
            self.stop.id, "pop", "sessC")
        self.assertTrue(r.get("success"), r)
        copies = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", inv.id),
            ("description", "=", f"__evidence_source:{r['id']}__"),
        ])
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].name, f"Pickup proof - Stop {self.stop.id}.pdf")

    def test_05_scan_cancel_discards_pages(self):
        self._add("scan", _jpeg_b64(), "p1.jpg",
                  extra={"scan_session": "sessD", "scan_page_index": 0})
        r = self.driver_env["prema.dispatch.job"].driver_cancel_scan(
            self.stop.id, "sessD")
        self.assertTrue(r.get("success"))
        self.assertFalse(self.env["prema.dispatch.evidence"].search(
            [("scan_session", "=", "sessD")]))
        self.assertFalse(self.stop.pop_attachment_ids)

    def test_06_scan_pages_do_not_satisfy_required_proof(self):
        self._add("scan", _jpeg_b64(), "p1.jpg",
                  extra={"scan_session": "sessE", "scan_page_index": 0})
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.stop.action_mark_completed()
        # after the merge the same stop completes
        r = self.driver_env["prema.dispatch.job"].driver_complete_scan(
            self.stop.id, "pop", "sessE")
        self.assertTrue(r.get("success"), r)
        self.stop.action_mark_completed()
        self.assertEqual(self.stop.status, "completed")

    def test_07_unsupported_ev_type_rejected(self):
        r = self._add("selfie", _jpeg_b64())
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "unsupported_type")

    def test_08_duplicate_scan_pages_allowed(self):
        # Dedup is per-proof-bucket; scan pages are held separately so the
        # same visual page can be uploaded twice and merged.
        same = _jpeg_b64()
        r1 = self._add("scan", same, "p1.jpg",
                       extra={"scan_session": "sessF", "scan_page_index": 0})
        r2 = self._add("scan", same, "p2.jpg",
                       extra={"scan_session": "sessF", "scan_page_index": 1})
        self.assertTrue(r1.get("success"))
        self.assertTrue(r2.get("success"))
        self.assertNotEqual(r1["id"], r2["id"])

    # ── §55 delete / retake ─────────────────────────────────────

    def test_09_remove_evidence_deletes_row_and_invoice_copy(self):
        inv = self._draft_invoice()
        self.job.invoice_id = inv.id
        r = self._add("pod", _jpeg_b64(), "pod1.jpg")
        self.assertTrue(r.get("success"), r)
        self.assertIn(r["id"], self.stop.pod_attachment_ids.ids)
        self.assertTrue(self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])]))
        rr = self.driver_env["prema.dispatch.job"].driver_remove_evidence(
            self.stop.id, "pod", r["id"])
        self.assertTrue(rr.get("success"))
        self.assertNotIn(r["id"], self.stop.pod_attachment_ids.ids)
        self.assertFalse(self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])]))
        self.assertFalse(self.env["ir.attachment"].search([
            ("description", "=", f"__evidence_source:{r['id']}__")]))

    def test_10_remove_scan_page_by_attachment(self):
        r = self._add("scan", _jpeg_b64(), "p1.jpg",
                      extra={"scan_session": "sessG", "scan_page_index": 0})
        rr = self.driver_env["prema.dispatch.job"].driver_remove_evidence(
            self.stop.id, "scan", r["id"])
        self.assertTrue(rr.get("success"))
        self.assertFalse(self.env["prema.dispatch.evidence"].search(
            [("scan_session", "=", "sessG")]))

    # ── §36/§37 invoice evidence sync ──────────────────────────

    def test_11_completion_bulk_copy_includes_pop(self):
        inv = self._draft_invoice()
        self.job.invoice_id = inv.id
        self._add("pop", _jpeg_b64(), "pop1.jpg")
        attached = self.job._attach_documents_to_invoice()
        self.assertGreater(attached, 0)
        names = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", inv.id),
        ]).mapped("name")
        self.assertTrue(any("PICKUP_PROOF" in n for n in names), names)
        self.assertFalse(any("POD" in n for n in names))  # base.automation 54 guard

    def test_12_pod_copy_only_to_draft_same_customer(self):
        inv = self._draft_invoice()
        self.job.invoice_id = inv.id
        inv.state = "posted"  # posted invoices never receive evidence
        r = self._add("pod", _jpeg_b64(), "pod1.jpg")
        self.assertTrue(r.get("success"), r)
        self.assertFalse(self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", inv.id),
        ]))

    # ── §57 end-day interplay (server-side gate) ───────────────

    def test_13_driver_without_stop_access_cannot_upload(self):
        # driver B (different partner) must be refused
        other_partner = self.env["res.partner"].create({"name": "Ev Other Driver"})
        other_user = self.env["res.users"].create({
            "name": "Ev Other",
            "login": f"evother{self.env['ir.sequence'].next_by_code('test') or '2'}",
            "partner_id": other_partner.id,
            "groups_id": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("prema_dispatch.group_dispatch_driver").id),
            ],
        })
        r = self.env(user=other_user.id)["prema.dispatch.job"].driver_add_evidence(
            self.stop.id, "pop", _jpeg_b64(), "x.jpg")
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "unauthorized")


@tagged("post_install", "-at_install")
class TestEvidenceControllerForwardsExtra(TransactionCase):
    """Static contract pinning the HTTP layer to the model contract.

    The model's driver_add_evidence() requires extra['pallet_id'] for POPP
    (spec §20) and spec §16 metadata (captured_at/lat/lng/device) for every
    upload — but the /dispatch/driver/evidence/add controller dropped the
    extra param into **kwargs, so every pallet-photo upload was rejected
    with "pallet_not_found" before reaching the model. Found in v7 browser
    UAT; all pre-existing evidence tests called the model directly and
    never exercised the controller. This pins the forwarding so the route
    cannot silently regress again.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.modules.module import get_module_path
        from pathlib import Path
        root = Path(get_module_path("prema_dispatch"))
        cls.ctrl = (root / "controllers" / "driver_app.py").read_text(encoding="utf-8")

    def test_route_signature_accepts_extra(self):
        match = re.search(r"def add_evidence\(self, stop_id, ev_type, data_b64, filename=.photo.jpg., extra=None, \*\*kwargs\)", self.ctrl)
        self.assertTrue(match, "add_evidence must accept extra=None explicitly")

    def test_route_forwards_extra_to_model(self):
        self.assertIn("driver_add_evidence(\n            stop_id, ev_type, data_b64, filename, extra=extra)", self.ctrl)

    def test_model_contract_requires_popp_pallet_id(self):
        # The popp branch of driver_add_evidence resolves the pallet from
        # extra['pallet_id'] and rejects a missing/foreign pallet — the
        # forwarding pinned above is what makes the route satisfy this.
        from odoo.modules.module import get_module_path
        from pathlib import Path
        root = Path(get_module_path("prema_dispatch"))
        model = (root / "models" / "dispatch_job.py").read_text(encoding="utf-8")
        self.assertIn("pallet_id", model)
        self.assertIn("This pallet does not belong to this pickup.", model)

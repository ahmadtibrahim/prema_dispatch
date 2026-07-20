"""
Phase 1C — upload validation, authorization, and duplicate-prevention
regression tests. Covers both the pure validation helper
(services/dispatch_upload.py) and the end-to-end driver_add_evidence /
driver_remove_evidence flow on prema.dispatch.job.

Run with:
  ./odoo-bin -c odoo18.conf -d <test-db> --test-enable \
      --test-tags /prema_dispatch:TestUploadValidation,/prema_dispatch:TestEvidenceUploadIntegration \
      -u prema_dispatch
"""
import base64
import io

from odoo.tests.common import TransactionCase

from odoo.addons.prema_dispatch.services import dispatch_upload as du

try:
    from PIL import Image
except ImportError:
    Image = None


def _real_jpeg(color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buf, format="JPEG")
    return buf.getvalue()


def _real_png(color=(0, 0, 255)):
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buf, format="PNG")
    return buf.getvalue()


def _real_pdf():
    return b"%PDF-1.4\n%fake but correctly signed for signature-only validation\n%%EOF"


def _heic_like():
    # Minimal ISO-BMFF box: size(4) + 'ftyp' + brand 'heic' + padding.
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 24


def _b64(data):
    return base64.b64encode(data).decode()


class TestUploadValidation(TransactionCase):
    """Pure validation-helper tests — no DB records needed."""

    # 1-4: valid formats accepted
    def test_01_valid_jpeg_accepted(self):
        r = du.decode_and_validate(_b64(_real_jpeg()), "photo.jpg", category="pod")
        self.assertEqual(r["mimetype"], "image/jpeg")
        self.assertTrue(r["preview_available"])

    def test_02_valid_png_accepted(self):
        r = du.decode_and_validate(_b64(_real_png()), "photo.png", category="pod")
        self.assertEqual(r["mimetype"], "image/png")
        self.assertTrue(r["preview_available"])

    def test_03_valid_pdf_accepted(self):
        r = du.decode_and_validate(_b64(_real_pdf()), "doc.pdf", category="pod")
        self.assertEqual(r["mimetype"], "application/pdf")
        self.assertFalse(r["preview_available"])

    def test_04_heic_like_accepted_no_preview(self):
        r = du.decode_and_validate(_b64(_heic_like()), "photo.heic", category="pod")
        self.assertEqual(r["mimetype"], "image/heic")
        self.assertFalse(r["preview_available"], "HEIC must not claim inline preview — no HEIF decode plugin on this server")

    # 5-11: rejections
    def test_05_empty_file_rejected(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate("", "x.jpg", category="pod")
        self.assertEqual(cm.exception.code, "empty_file")

    def test_06_invalid_base64_rejected(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate("not-valid-base64!!!", "x.jpg", category="pod")
        self.assertEqual(cm.exception.code, "invalid_base64")

    def test_07_oversized_file_rejected(self):
        big = b"\xff\xd8\xff" + b"0" * (du.MAX_UPLOAD_BYTES + 1024)
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(big), "big.jpg", category="pod")
        self.assertEqual(cm.exception.code, "file_too_large")

    def test_08_fake_jpeg_extension_rejected(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(b"this is plain text, not an image"), "x.jpg", category="pod")
        self.assertEqual(cm.exception.code, "unsupported_type")

    def test_09_fake_png_rejected(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(b"this is plain text, not an image"), "x.png", category="pod")
        self.assertEqual(cm.exception.code, "unsupported_type")

    def test_10_fake_pdf_rejected(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(b"this is plain text, not a pdf"), "x.pdf", category="pod")
        self.assertEqual(cm.exception.code, "unsupported_type")

    def test_11_unsupported_mime_rejected(self):
        # ZIP signature ("PK\x03\x04") — must never be accepted regardless of extension.
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(b"PK\x03\x04fakezipcontent"), "archive.zip", category="pod")
        self.assertEqual(cm.exception.code, "unsupported_type")

    # 12-13: filename sanitization
    def test_12_unsafe_filename_sanitized(self):
        name = du.sanitize_filename("../../etc/passwd.jpg")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertEqual(name, "passwd.jpg")

    def test_13_extremely_long_filename_handled(self):
        name = du.sanitize_filename("a" * 500 + ".jpg")
        self.assertLessEqual(len(name), 150)

    # 22: structured error code — mime/extension mismatch
    def test_22_structured_error_code_mime_mismatch(self):
        with self.assertRaises(du.UploadError) as cm:
            du.decode_and_validate(_b64(_real_png()), "photo.jpg", category="pod")
        self.assertEqual(cm.exception.code, "mime_mismatch")
        self.assertTrue(cm.exception.message)


class TestEvidenceUploadIntegration(TransactionCase):
    """End-to-end tests against driver_add_evidence/driver_remove_evidence,
    covering authorization, duplicate detection, and the invoice-copy
    behavior that already existed before this phase."""

    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.stage_draft = self.env["prema.dispatch.stage"].search([("stage_type", "=", "draft")], limit=1)
        self.customer = self.env["res.partner"].create({"name": "Upload Test Customer"})

        self.driver_a_partner = self.env["res.partner"].create({"name": "Upload Driver A"})
        self.driver_b_partner = self.env["res.partner"].create({"name": "Upload Driver B"})

        self.invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.customer.id,
        })
        self.job_a = self.Job.create({
            "partner_id": self.customer.id, "stage_id": self.stage_draft.id,
            "driver_id": self.driver_a_partner.id, "invoice_id": self.invoice.id,
        })
        self.stop_a = self.Stop.create({
            "job_id": self.job_a.id, "sequence": 10, "stop_type": "dropoff",
            "address": "1 Upload Test St, Toronto, ON",
        })
        self.stop_a2 = self.Stop.create({
            "job_id": self.job_a.id, "sequence": 20, "stop_type": "dropoff",
            "address": "2 Upload Test St, Toronto, ON",
        })
        self.job_b = self.Job.create({
            "partner_id": self.customer.id, "stage_id": self.stage_draft.id,
            "driver_id": self.driver_b_partner.id,
        })
        self.stop_b = self.Stop.create({
            "job_id": self.job_b.id, "sequence": 10, "stop_type": "dropoff",
            "address": "999 Foreign St, Toronto, ON",
        })

        self.driver_a_user = self._make_user(
            "upload_driver_a@example.com", self.driver_a_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        self.dispatcher_user = self._make_user(
            "upload_dispatcher@example.com",
            self.env["res.partner"].create({"name": "Upload Dispatcher"}),
            "base.group_user", "prema_dispatch.group_dispatcher",
        )

    def _make_user(self, login, partner, *group_xmlids):
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": partner.name, "login": login, "partner_id": partner.id,
            "groups_id": [(6, 0, [self.env.ref(g).id for g in group_xmlids])],
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
        })

    def _add_evidence(self, user, stop_id, ev_type, data, filename):
        return self.Job.with_user(user).driver_add_evidence(stop_id, ev_type, _b64(data), filename)

    # 14-16: duplicate detection scope
    def test_14_duplicate_checksum_no_second_attachment(self):
        jpeg = _real_jpeg()
        r1 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", jpeg, "photo.jpg")
        self.assertTrue(r1["success"]); self.assertFalse(r1.get("duplicate"))
        r2 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", jpeg, "photo.jpg")
        self.assertTrue(r2["success"]); self.assertTrue(r2.get("duplicate"))
        self.assertEqual(r2["existing_attachment_id"], r1["id"])
        self.stop_a.invalidate_recordset()
        self.assertEqual(len(self.stop_a.pod_attachment_ids), 1)

    def test_15_same_filename_different_content_new_attachment(self):
        r1 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg((255, 0, 0)), "photo.jpg")
        r2 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg((0, 255, 0)), "photo.jpg")
        self.assertTrue(r1["success"]); self.assertTrue(r2["success"])
        self.assertFalse(r2.get("duplicate"))
        self.assertNotEqual(r1["id"], r2["id"])
        self.stop_a.invalidate_recordset()
        self.assertEqual(len(self.stop_a.pod_attachment_ids), 2)

    def test_16_same_content_different_stop_allowed(self):
        jpeg = _real_jpeg()
        r1 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", jpeg, "photo.jpg")
        r2 = self._add_evidence(self.driver_a_user, self.stop_a2.id, "pod", jpeg, "photo.jpg")
        self.assertTrue(r1["success"]); self.assertTrue(r2["success"])
        self.assertFalse(r2.get("duplicate"), "duplicate scope is per-record, not global")

    # 17-19: authorization
    def test_17_driver_a_upload_to_driver_b_stop_rejected(self):
        r = self._add_evidence(self.driver_a_user, self.stop_b.id, "pod", _real_jpeg(), "photo.jpg")
        self.assertFalse(r["success"])
        self.assertEqual(r["code"], "unauthorized")

    def test_18_driver_a_upload_to_nonexistent_stop_rejected(self):
        fake_id = self.stop_b.id + 999999
        r = self._add_evidence(self.driver_a_user, fake_id, "pod", _real_jpeg(), "photo.jpg")
        self.assertFalse(r["success"])
        self.assertEqual(r["code"], "record_not_found")

    def test_19_dispatcher_upload_remains_allowed(self):
        r = self._add_evidence(self.dispatcher_user, self.stop_b.id, "pod", _real_jpeg(), "photo.jpg")
        self.assertTrue(r["success"])

    # 20-21: invoice-copy behavior (pre-existing, must survive this phase)
    def test_20_invoice_evidence_copy_behavior_preserved(self):
        r = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg(), "photo.jpg")
        self.assertTrue(r["success"])
        tag = f"__evidence_source:{r['id']}__"
        copies = self.env["ir.attachment"].search([("description", "=", tag)])
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies.res_model, "account.move")
        self.assertEqual(copies.res_id, self.invoice.id)

    def test_21_remove_evidence_removes_only_intended_copy(self):
        r1 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg((255, 0, 0)), "a.jpg")
        r2 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg((0, 255, 0)), "b.jpg")
        self.Job.with_user(self.driver_a_user).driver_remove_evidence(self.stop_a.id, "pod", r1["id"])
        self.stop_a.invalidate_recordset()
        remaining_ids = self.stop_a.pod_attachment_ids.ids
        self.assertNotIn(r1["id"], remaining_ids)
        self.assertIn(r2["id"], remaining_ids)
        tag1 = f"__evidence_source:{r1['id']}__"
        tag2 = f"__evidence_source:{r2['id']}__"
        self.assertFalse(self.env["ir.attachment"].search([("description", "=", tag1)]))
        self.assertTrue(self.env["ir.attachment"].search([("description", "=", tag2)]))

    # 23: success metadata
    def test_23_successful_upload_returns_metadata(self):
        r = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", _real_jpeg(), "photo.jpg")
        for key in ("id", "name", "url", "mimetype", "checksum_sha256", "preview_available"):
            self.assertIn(key, r)
        self.assertEqual(r["mimetype"], "image/jpeg")

    # 24: idempotent retry
    def test_24_repeated_identical_request_is_idempotent(self):
        jpeg = _real_jpeg()
        r1 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", jpeg, "photo.jpg")
        r2 = self._add_evidence(self.driver_a_user, self.stop_a.id, "pod", jpeg, "photo.jpg")
        self.assertTrue(r1["success"]); self.assertTrue(r2["success"])
        self.assertTrue(r2.get("duplicate"))
        self.stop_a.invalidate_recordset()
        self.assertEqual(len(self.stop_a.pod_attachment_ids), 1, "a retried identical request must not create a second attachment")

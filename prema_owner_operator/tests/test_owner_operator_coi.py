# -*- coding: utf-8 -*-
"""COI version storage, deterministic verification and reviewer actions."""
import uuid

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)
from odoo.addons.prema_owner_operator.tests.test_owner_operator_workflow import (
    _pdf_bytes)


@tagged("post_install", "-at_install")
class TestCOIVerification(OwnerOperatorCase):

    def setUp(self):
        super().setUp()
        self.authority = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)
        self.verified_stage = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Verified"),
             ("job_ids", "in", self.job.id)], limit=1)

    def _app(self, email=None, **overrides):
        app = self.Application._create_from_public(self._public_vals(
            email=email or "coi-%s@test.invalid" % uuid.uuid4().hex[:8],
            **overrides))
        app._ensure_checklist()
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": self.authority.id})
        return app

    def _config(self, min_auto=2000000, min_cargo=100000,
                min_conf=0.7, trusted="insurer.test"):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("prema_owner_operator.coi_min_auto_liability", min_auto)
        icp.set_param("prema_owner_operator.coi_min_cargo", min_cargo)
        icp.set_param("prema_owner_operator.coi_min_ai_confidence", min_conf)
        icp.set_param("prema_owner_operator.trusted_insurer_domains", trusted)

    def _version(self, app, payload=None, source="insurer", is_direct=True,
                 sender="broker@insurer.test"):
        payload = payload or _pdf_bytes() or b"%PDF-1.4 test"
        version, duplicate = self.env[
            "prema.owner.operator.coi.version"].sudo()._create_version(
                app, payload, "coi.pdf", "application/pdf", sender,
                fields.Datetime.now(), "<%s@test.invalid>" % uuid.uuid4().hex,
                source=source, is_direct=is_direct)
        return version, duplicate

    def _valid(self, app):
        today = fields.Date.context_today(app)
        return {
            "is_coi": {"value": True, "confidence": 0.95, "page": 1,
                       "evidence": "Certificate of Insurance"},
            "named_insured": {"value": app.business_name, "confidence": 0.95,
                              "page": 1, "evidence": app.business_name},
            "certificate_holder_name": {
                "value": "PremaFirm Inc.", "confidence": 0.95, "page": 1,
                "evidence": "PremaFirm Inc."},
            "certificate_holder_address": {
                "value": "PremaFirm Inc., 994 Westport Cres, Unit 7A, "
                         "Mississauga, ON L5T 1G1, Canada",
                "confidence": 0.95, "page": 1,
                "evidence": "994 Westport Cres"},
            "auto_liability_limit": {"value": 2000000, "confidence": 0.95,
                                     "page": 1, "evidence": "$2,000,000"},
            "cargo_limit": {"value": 100000, "confidence": 0.95, "page": 1,
                            "evidence": "$100,000"},
            "policy_numbers": {"value": ["POL-001"], "confidence": 0.95,
                               "page": 1, "evidence": "POL-001"},
            "effective_date": {
                "value": fields.Date.subtract(today, days=30).isoformat(),
                "confidence": 0.95, "page": 1, "evidence": "Effective"},
            "expiry_date": {
                "value": fields.Date.add(today, days=300).isoformat(),
                "confidence": 0.95, "page": 1, "evidence": "Expiry"},
            "contradictions": {"value": [], "confidence": 0.95, "page": 1,
                               "evidence": ""},
        }

    def _apply(self, version, data):
        version.write({
            "extraction_state": "done",
            "extraction_result": data,
            "extraction_method": "test",
            "extraction_confidence": 0.95,
            "extraction_error": False,
        })
        version._apply_extracted_fields(data)
        version._evaluate_verification()
        return version

    def test_valid_direct_coi_verifies_once_without_contract_or_fleet(self):
        self._config()
        app = self._app(business_name="Acme Transport Ltd")
        version, duplicate = self._version(app)
        self.assertFalse(duplicate)
        self.assertTrue(version.is_current)
        self.assertFalse(version.attachment_id.public)
        self.assertTrue(version.sha256)
        agreement_mails = self.env["mail.mail"].search_count(
            [("subject", "ilike", "Agreement for signature")])
        self._apply(version, self._valid(app))
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "verified")
        self.assertEqual(app.hr_applicant_id.stage_id, self.verified_stage)
        self.assertEqual(app.verification_method, "ai")
        self.assertTrue(app.verification_date)
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertEqual(doc.status, "verified")
        version.invalidate_recordset()
        self.assertEqual(version.verification_status, "verified")
        self.assertEqual(
            self.env["prema.owner.operator"].search_count([]), 0)
        self.assertEqual(
            self.env["fleet.vehicle"].search_count(
                [("operator_id", "!=", False)]), 0)
        self.assertEqual(
            self.env["mail.mail"].search_count(
                [("subject", "ilike", "Agreement for signature")]),
            agreement_mails)
        messages = len(app.message_ids)
        self._apply(version, self._valid(app))
        self.assertEqual(len(app.message_ids), messages,
                         "a second decision must not duplicate the audit note")
        self.assertEqual(app.hr_applicant_id.stage_id, self.verified_stage)

    def test_low_confidence_produces_review(self):
        self._config(min_conf=0.9)
        app = self._app()
        version, _ = self._version(app)
        data = self._valid(app)
        data["named_insured"]["confidence"] = 0.4
        self._apply(version, data)
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "review")
        self.assertIn("Low-confidence", app.verification_reasons or "")
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)
        self.assertEqual(len(app.activity_ids), 1)

    def test_expired_insufficient_and_mismatched_coverage_review(self):
        self._config()
        app = self._app(business_name="Acme Transport Ltd")
        version, _ = self._version(app)
        data = self._valid(app)
        data["named_insured"]["value"] = "Some Other Company"
        data["auto_liability_limit"]["value"] = 1000
        data["expiry_date"]["value"] = (
            fields.Date.subtract(
                fields.Date.context_today(app), days=5).isoformat())
        self._apply(version, data)
        reasons = app.verification_reasons or ""
        self.assertEqual(app.verification_status, "review")
        self.assertIn("Named insured does not match", reasons)
        self.assertIn("below the configured minimum", reasons)
        self.assertIn("expired", reasons)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)

    def test_extraction_failure_produces_review(self):
        self._config()
        app = self._app()
        version, _ = self._version(app)
        version.write({
            "extraction_state": "failed",
            "extraction_error": "Unreadable document.",
        })
        version._evaluate_verification()
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "review")
        self.assertIn("Unreadable", app.verification_reasons or "")
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)

    def test_non_direct_version_is_review_only(self):
        self._config()
        app = self._app()
        version, _ = self._version(
            app, source="unknown", is_direct=False,
            sender="unknown@unknown.test")
        self.assertFalse(version.is_current)
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "review")
        self.assertIn("Unknown or untrusted", app.verification_reasons or "")

    def test_renewed_coi_keeps_every_version(self):
        self._config()
        app = self._app()
        first, _ = self._version(app, payload=b"%PDF-1.4 first")
        second, _ = self._version(app, payload=b"%PDF-1.4 second")
        self.assertEqual(app.coi_version_ids, first | second)
        self.assertFalse(first.is_current)
        self.assertTrue(second.is_current)
        self.assertTrue(first.attachment_id.exists())
        self.assertTrue(second.attachment_id.exists())
        self.assertNotEqual(first.attachment_id, second.attachment_id)
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertEqual(doc.attachment_id, second.attachment_id)

    def test_duplicate_checksum_is_not_a_new_version(self):
        self._config()
        app = self._app()
        first, dup1 = self._version(app, payload=b"%PDF-1.4 same")
        second, dup2 = self._version(app, payload=b"%PDF-1.4 same")
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertEqual(first, second)
        self.assertEqual(len(app.coi_version_ids), 1)

    def test_manual_approve_reject_and_request(self):
        self._config()
        app = self._app()
        version, _ = self._version(app)
        app.write({
            "verification_status": "review",
            "verification_reasons": "Needs review",
            "verification_review_reason": "Checked with the insurer by phone",
        })
        app.action_approve_verification()
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "verified")
        self.assertEqual(app.verification_method, "manual")
        self.assertEqual(app.verification_reviewer_id, self.env.user)
        self.assertTrue(app.verification_reviewed_on)
        self.assertEqual(app.verification_review_reason,
                         "Checked with the insurer by phone")
        self.assertEqual(app.hr_applicant_id.stage_id, self.verified_stage)
        self.assertTrue(version.attachment_id.exists())

        app2 = self._app()
        version2, _ = self._version(app2)
        app2.write({
            "verification_status": "review",
            "verification_reasons": "Needs review",
            "verification_review_reason": "Certificate is unreadable",
        })
        app2.action_reject_coi()
        app2.invalidate_recordset()
        self.assertEqual(app2.verification_status, "review")
        self.assertEqual(app2.hr_applicant_id.stage_id, self.authority)
        self.assertTrue(version2.attachment_id.exists(),
                        "rejection must preserve the document")
        self.assertIn("rejected", (app2.verification_reasons or "").lower())

        app3 = self._app()
        self._version(app3)
        app3.write({
            "verification_status": "review",
            "verification_review_reason": "Need a current certificate",
        })
        before = self.env["mail.mail"].search_count(
            [("subject", "ilike", "Action Required: Certificate of Insurance")])
        app3.action_request_updated_coi()
        after = self.env["mail.mail"].search_count(
            [("subject", "ilike", "Action Required: Certificate of Insurance")])
        self.assertEqual(after, before + 1)
        self.assertEqual(app3.verification_status, "review")
        self.assertEqual(app3.hr_applicant_id.stage_id, self.authority)

    def test_review_requires_a_reason_for_manual_actions(self):
        self._config()
        app = self._app()
        self._version(app)
        app.write({"verification_status": "review"})
        with self.assertRaises(UserError):
            app.action_approve_verification()

    def test_verified_and_review_are_single_values(self):
        app = self._app()
        app.write({"verification_status": "verified"})
        app.write({"verification_status": "review"})
        self.assertEqual(app.verification_status, "review")
        app.write({"verification_status": "verified"})
        self.assertEqual(app.verification_status, "verified")

    def test_kanban_card_declares_verified_and_review_badges(self):
        view = self.env.ref(
            "prema_owner_operator.view_hr_applicant_kanban_owner_operator")
        arch = view.arch_db or ""
        self.assertIn("oo_verification_status", arch)
        self.assertIn("oo_verification_reasons", arch)
        self.assertIn("border-danger", arch)
        self.assertIn("decoration-success", arch)
        self.assertIn("decoration-danger", arch)
        app = self._app()
        app.hr_applicant_id.invalidate_recordset()
        self.assertEqual(app.hr_applicant_id.oo_verification_status,
                         "pending")
        app.verification_status = "verified"
        app.hr_applicant_id.invalidate_recordset()
        self.assertEqual(app.hr_applicant_id.oo_verification_status,
                         "verified")
        app.verification_status = "review"
        app.hr_applicant_id.invalidate_recordset()
        self.assertEqual(app.hr_applicant_id.oo_verification_status, "review")

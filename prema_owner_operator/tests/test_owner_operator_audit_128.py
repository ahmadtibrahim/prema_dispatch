# -*- coding: utf-8 -*-
"""1.2.8 audit proofs: real COI chain, negative COI, units, permissions."""
import base64
import os
import uuid
from email.message import EmailMessage

from odoo import fields
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tests import HttpCase, tagged

from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)
from odoo.addons.prema_owner_operator.tests.test_owner_operator_workflow import (
    _pdf_bytes)


@tagged("post_install", "-at_install")
class TestCOIApprovalChain(OwnerOperatorCase):

    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("prema_owner_operator.coi_min_auto_liability", 2000000)
        icp.set_param("prema_owner_operator.coi_min_cargo", 100000)
        icp.set_param("prema_owner_operator.coi_min_ai_confidence", 0.7)
        icp.set_param("prema_owner_operator.trusted_insurer_domains",
                      "insurer.test")
        self.authority = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)

    def _app(self):
        app = self.Application._create_from_public(self._public_vals(
            email="chain-%s@test.invalid" % uuid.uuid4().hex[:8]))
        app._ensure_checklist()
        app.write({
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=2),
            "home_province_code": "ON",
            "home_province": "Ontario",
        })
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": self.authority.id})
        return app

    def _verify_docs(self, app, skip=("coi_insurer", "signed_agreement")):
        for doc in app.document_ids:
            if doc.document_type in skip:
                continue
            att = self.env["ir.attachment"].create({
                "name": "%s.pdf" % doc.document_type,
                "raw": _pdf_bytes() or b"%PDF-1.4 test",
                "res_model": "prema.owner.operator.document",
                "res_id": doc.id,
            })
            doc.write({"status": "verified", "attachment_id": att.id})

    def _noncompliant_operator(self, app):
        op = self.env["prema.owner.operator"].sudo().create({
            "partner_id": app.partner_id.id,
            "home_city": "Mississauga", "home_province": "Ontario",
            "authority_state": "pending_manual",
            "coi_verified_direct": False,
            "insurance_expiry": fields.Date.add(
                fields.Date.context_today(app), years=1),
        })
        app.write({"operator_id": op.id})
        return op

    def _sign(self, app):
        pdf = _pdf_bytes()
        if not pdf:
            self.skipTest("reportlab is unavailable")
        att = self.env["ir.attachment"].create({
            "name": "agreement.pdf", "raw": pdf,
            "mimetype": "application/pdf"})
        tmpl = self.env["sign.template"].create({"attachment_id": att.id})
        role = self.env.ref("sign.sign_item_role_default")
        req = self.env["sign.request"].with_context(no_sign_mail=True).create({
            "template_id": tmpl.id, "reference": app.name,
            "subject": "Owner-Operator Agreement — %s" % app.name,
            "request_item_ids": [(0, 0, {
                "partner_id": app.partner_id.id, "role_id": role.id})],
        })
        req.write({"state": "signed"})
        req.request_item_ids.write({
            "state": "completed", "signing_date": fields.Date.today()})
        req.write({"completed_document": base64.b64encode(pdf)})
        app.write({"sign_request_id": req.id, "sign_template_id": tmpl.id,
                   "contract_sent_on": fields.Datetime.now()})
        app._cron_sync_signatures()
        signed = app.document_ids.filtered(
            lambda d: d.document_type == "signed_agreement")[:1]
        signed.write({"status": "verified"})
        return signed

    def _raw(self, app, sender="Broker <broker@insurer.test>",
             certificate=True):
        msg = EmailMessage()
        msg["Subject"] = "[%s] — Certificate of Insurance — %s" % (
            app.name, app.business_name)
        msg["From"] = sender
        msg["To"] = "verification@premafirm.com"
        msg["Message-ID"] = "<%s@test.invalid>" % uuid.uuid4().hex
        msg.set_content("Please find the certificate attached.")
        if certificate:
            msg.add_attachment(
                _pdf_bytes() or b"%PDF-1.4 test", maintype="application",
                subtype="pdf", filename="coi.pdf")
        return msg.as_bytes()

    def _process(self, raw):
        thread_id = self.env["mail.thread"].message_process(
            "prema.owner.operator.verification", raw)
        return self.env["prema.owner.operator.verification"].browse(
            thread_id)

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
                "confidence": 0.95, "page": 1, "evidence": "994 Westport Cres"},
            "auto_liability_limit": {"value": 2000000, "confidence": 0.95,
                                     "page": 1, "evidence": "$2,000,000"},
            "cargo_limit": {"value": 100000, "confidence": 0.95, "page": 1,
                            "evidence": "$100,000"},
            "policy_numbers": {"value": ["POL-1"], "confidence": 0.95,
                               "page": 1, "evidence": "POL-1"},
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

    def test_real_inbound_coi_verifies_then_reaches_driver_and_vehicle(self):
        app = self._app()
        self._verify_docs(app)
        raw = self._raw(app)
        queue = self._process(raw)
        self.assertEqual(queue.state, "matched")
        self.assertEqual(queue.matched_application_id, app)
        version = app.coi_current_version_id
        self.assertTrue(version)
        self.assertTrue(version.is_current)
        self.assertTrue(version.is_direct)
        self.assertEqual(version.verification_id, queue)
        self.assertIn(version.message_id, queue.message_id)
        self.assertEqual(len(version.sha256), 64)
        self.assertTrue(version.attachment_id.checksum)
        self.assertFalse(version.attachment_id.public)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority,
                         "receipt alone must not move the stage")

        self._apply(version, self._valid(app))
        app.invalidate_recordset()
        self.assertEqual(app.verification_status, "verified")
        self.assertEqual(app.hr_applicant_id.stage_id.name, "Verified")
        coi_doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1]
        self.assertEqual(coi_doc.status, "verified")
        self.assertEqual(coi_doc.attachment_id.checksum,
                         version.attachment_id.checksum)

        op = self._noncompliant_operator(app)
        op.write({
            "coi_document_id": coi_doc.id,
            "coi_verified_direct": True,
            "insurance_expiry": fields.Date.add(
                fields.Date.context_today(app), years=1),
        })
        op.action_mark_authority_verified()
        signed = self._sign(app)
        app.invalidate_recordset()
        self.assertTrue(app.can_approve, (
            app.missing_verification, app.equipment_missing,
            app.licence_current, app.operator_id.compliance_ok))
        vehicle = app.action_approve()
        self.assertTrue(vehicle)

        vehicle_coi = self.env["documents.document"].search([
            ("oo_application_id", "=", app.id),
            ("oo_role", "=", "vehicle"),
            ("oo_coi_version_id", "=", version.id),
        ])
        self.assertEqual(len(vehicle_coi), 1)
        self.assertEqual(vehicle_coi.oo_source_checksum,
                         version.attachment_id.checksum)
        driver_signed = self.env["documents.document"].search([
            ("oo_application_id", "=", app.id),
            ("oo_role", "=", "driver"),
            ("oo_source_document_id", "=", signed.id),
        ])
        self.assertEqual(len(driver_signed), 1)
        self.assertEqual(driver_signed.oo_source_checksum,
                         signed.attachment_id.checksum)

        docs_count = self.env["documents.document"].search_count(
            [("oo_application_id", "=", app.id)])
        repeat = app.action_approve()
        self.assertEqual(repeat, vehicle)
        self.assertEqual(self.env["documents.document"].search_count(
            [("oo_application_id", "=", app.id)]), docs_count)

    def test_applicant_untrusted_missing_conflicting_expired_block_approval(self):
        scenarios = []

        # applicant-forwarded certificate (applicant copy only)
        app = self._app()
        self._verify_docs(app)
        self._noncompliant_operator(app)
        queue = self._process(self._raw(app, sender=app.email))
        app.invalidate_recordset()
        self.assertEqual(queue.state, "applicant_forwarded")
        self.assertFalse(app.coi_current_version_id.is_direct)
        scenarios.append(("applicant", app))

        # untrusted sender
        app = self._app()
        self._verify_docs(app)
        self._noncompliant_operator(app)
        queue = self._process(self._raw(
            app, sender="Unknown <unknown@unknown.test>"))
        app.invalidate_recordset()
        self.assertEqual(queue.state, "matched")
        self.assertFalse(app.coi_current_version_id.is_direct)
        scenarios.append(("untrusted", app))

        # missing certificate
        app = self._app()
        self._verify_docs(app)
        self._noncompliant_operator(app)
        queue = self._process(self._raw(app, certificate=False))
        app.invalidate_recordset()
        self.assertEqual(queue.state, "no_certificate")
        scenarios.append(("missing", app))

        # conflicting + expired coverage
        app = self._app()
        self._verify_docs(app)
        self._noncompliant_operator(app)
        queue = self._process(self._raw(app))
        version = app.coi_current_version_id
        data = self._valid(app)
        data["named_insured"]["value"] = "Some Other Company"
        data["expiry_date"]["value"] = fields.Date.subtract(
            fields.Date.context_today(app), days=5).isoformat()
        data["auto_liability_limit"]["value"] = 1000
        self._apply(version, data)
        app.invalidate_recordset()
        scenarios.append(("conflicting", app))

        for label, app in scenarios:
            app.invalidate_recordset()
            self.assertEqual(app.verification_status, "review", label)
            self.assertEqual(app.hr_applicant_id.stage_id, self.authority,
                             label)
            self.assertFalse(app.can_approve, label)
            with self.assertRaises(UserError, msg=label):
                app.action_approve()
            self.assertFalse(app.fleet_vehicle_id, label)
            self.assertFalse(self.env["fleet.vehicle"].search(
                [("application_id", "=", app.id)]), label)

    def test_hr_manager_can_move_applicant_without_owner_group(self):
        app = self.Application._create_from_public(self._public_vals(
            email="hrmgr-%s@test.invalid" % uuid.uuid4().hex[:8]))
        app._ensure_checklist()
        app.write({
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=2),
            "home_province_code": "ON", "home_province": "Ontario",
        })
        manager = self.env["res.users"].create({
            "name": "HR Manager",
            "login": "hrmgr-%s" % uuid.uuid4().hex[:8],
            "email": "hrmgr-%s@test.invalid" % uuid.uuid4().hex[:8],
            "password": "HrManager#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "hr_recruitment.group_hr_recruitment_manager").id,
            ])],
        })
        self.assertFalse(manager.has_group(
            "prema_owner_operator.group_owner_operator"))
        app.hr_applicant_id.with_user(manager).write(
            {"stage_id": self.authority.id})
        app.invalidate_recordset()
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)
        self.assertTrue(app.coi_request_sent_at)
        self.assertTrue(app.message_ids)


@tagged("post_install", "-at_install")
class TestCargoHeightContract(OwnerOperatorCase):

    def test_96_inches_is_8_feet_and_never_96_feet(self):
        app = self.Application._create_from_public(self._public_vals(
            email="height-%s@test.invalid" % uuid.uuid4().hex[:8]))
        self._complete(app, sign=True)
        app.write({"cargo_height_in": 96.0, "box_height_ft": 8.0})
        vehicle = app.action_approve()
        self.assertEqual(vehicle.oo_cargo_height_in, 96.0)
        self.assertEqual(vehicle.x_box_interior_height_ft, 8.0)
        self.assertNotEqual(vehicle.x_box_interior_height_ft, 96.0)

    def test_migration_converts_only_operator_vehicles(self):
        app = self.Application._create_from_public(self._public_vals(
            email="mig-%s@test.invalid" % uuid.uuid4().hex[:8]))
        self._complete(app, sign=True)
        app.write({"cargo_height_in": 96.0})
        vehicle = app.action_approve()
        vehicle.write({"x_box_interior_height_ft": 96.0,
                       "oo_cargo_height_in": 0.0})
        company = self.env["fleet.vehicle"].create({
            "name": "Company Height Test",
            "model_id": self._vehicle_model().id,
            "x_box_interior_height_ft": 8.5,
        })
        path = os.path.join(
            os.path.dirname(__file__), "..", "migrations",
            "18.0.1.2.8", "post-migrate.py")
        namespace = {}
        with open(path) as handle:
            exec(handle.read(), namespace)
        namespace["migrate"](self.env.cr, "18.0.1.2.7")
        vehicle.invalidate_recordset()
        company.invalidate_recordset()
        self.assertEqual(vehicle.oo_cargo_height_in, 96.0)
        self.assertEqual(vehicle.x_box_interior_height_ft, 8.0)
        self.assertEqual(company.x_box_interior_height_ft, 8.5)
        self.assertFalse(company.operator_id)


@tagged("post_install", "-at_install")
class TestDocumentPermissionsAudit(HttpCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Doc Applicant"})
        self.app = self.env["prema.owner.operator.application"].create({
            "partner_id": self.partner.id,
            "contact_name": "Doc Applicant",
            "email": "doc-%s@test.invalid" % uuid.uuid4().hex[:8],
            "business_name": "Doc Transport",
            "company_id": self.env.company.id,
        })
        attachment = self.env["ir.attachment"].create({
            "name": "licence.pdf", "raw": b"%PDF-1.4 licence",
            "mimetype": "application/pdf", "public": False,
        })
        self.doc = self.env["documents.document"].create({
            "name": "licence.pdf", "type": "binary",
            "attachment_id": attachment.id,
            "oo_application_id": self.app.id,
            "oo_role": "driver",
            "access_internal": "view",
            "access_via_link": "none",
        })
        unrelated_attachment = self.env["ir.attachment"].create({
            "name": "unrelated.pdf", "raw": b"%PDF-1.4 unrelated",
            "mimetype": "application/pdf", "public": False,
        })
        self.unrelated = self.env["documents.document"].create({
            "name": "unrelated.pdf", "type": "binary",
            "attachment_id": unrelated_attachment.id,
            "access_internal": "view", "access_via_link": "none",
        })
        self.reviewer = self.env["res.users"].create({
            "name": "Onboarding Reviewer",
            "login": "onboarding-%s" % uuid.uuid4().hex[:8],
            "password": "Reviewer#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "prema_owner_operator.group_owner_operator").id,
            ])],
        })
        self.unrelated_user = self.env["res.users"].create({
            "name": "Unrelated Internal",
            "login": "internal-%s" % uuid.uuid4().hex[:8],
            "password": "Internal#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("fleet.fleet_group_user").id,
            ])],
        })
        self.fleet_manager = self.env["res.users"].create({
            "name": "Fleet Manager",
            "login": "fleet-%s" % uuid.uuid4().hex[:8],
            "password": "Fleet#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("fleet.fleet_group_manager").id,
            ])],
        })

    def test_onboarding_reviewer_has_scoped_documents_only(self):
        self.assertFalse(self.reviewer.has_group(
            "documents.group_documents_user"))
        self.assertFalse(self.reviewer.has_group(
            "documents.group_documents_manager"))
        self.assertFalse(self.reviewer.has_group(
            "prema_logistics_booking.group_logistics_pricing_manager"))
        self.doc.with_user(self.reviewer).check_access("read")
        # A Fleet manager keeps their existing company-wide document access
        # AND can read the operator-linked copies.
        self.assertFalse(self.fleet_manager.has_group(
            "documents.group_documents_user"))
        self.doc.with_user(self.fleet_manager).check_access("read")
        self.unrelated.with_user(self.fleet_manager).check_access("read")
        for record, user, label in (
                (self.unrelated, self.reviewer, "reviewer/unrelated"),
                (self.doc, self.unrelated_user, "unrelated/user-doc")):
            try:
                record.with_user(user).check_access("read")
                self.fail("%s could read %s" % (label, record.name))
            except (AccessError, MissingError):
                pass

        self.authenticate(self.reviewer.login, "Reviewer#123")
        ok = self.url_open("/owner_operator/documents/%d" % self.doc.id)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers.get("Content-Type"), "application/pdf")

        self.authenticate(self.unrelated_user.login, "Internal#123")
        denied = self.url_open("/owner_operator/documents/%d" % self.doc.id)
        self.assertIn(denied.status_code, (403, 404))

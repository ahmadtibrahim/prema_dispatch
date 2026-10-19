# -*- coding: utf-8 -*-
"""Revised owner-operator workflow assertions.

Covers the job-5 pipeline, the insurer-direct COI request, inbound
verification matching, genuine Sign completion, load-notification consent and
the dispatch-equipment gate. No test sends email to a real recipient: the
staging SMTP is disabled and every template is queued with force_send=False.
"""
import base64
import email
import uuid
from datetime import datetime
from io import BytesIO

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    normalise_public)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)


def _pdf_bytes():
    try:
        from reportlab.pdfgen import canvas
    except ImportError:  # pragma: no cover
        return False
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(100, 700, "Owner-operator agreement")
    pdf.save()
    return buffer.getvalue()


@tagged("post_install", "-at_install")
class TestPipeline(OwnerOperatorCase):

    def test_job5_has_only_the_revised_pipeline(self):
        stages = self.env["hr.recruitment.stage"].search(
            [("job_ids", "in", self.job.id)], order="sequence,id")
        self.assertEqual(
            stages.mapped("name"),
            ["New", "Authority & Insurance Review", "Contract Proposal",
             "Contract Signed", "Approved — Ready for Dispatch"])
        self.assertEqual([s.sequence for s in stages], [0, 1, 2, 3, 4])
        removed = self.env["hr.recruitment.stage"].search(
            [("name", "in", ["Equipment Verification",
                             "Initial Qualification", "Final Review",
                             "First Interview", "Second Interview"])])
        for stage in removed:
            self.assertNotIn(self.job, stage.job_ids)

    def test_final_review_is_detached_and_history_returns_to_authority(self):
        from odoo.addons.prema_owner_operator.hooks import ensure_pipeline
        app = self._applicant("finalreview-migration@test.invalid") \
            .owner_operator_application_id
        final = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Final Review")], limit=1)
        final.write({"job_ids": [(4, self.job.id)]})
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": final.id})
        mail_before = self.env["mail.mail"].search_count([])
        ensure_pipeline(self.env)
        app.invalidate_recordset()
        self.assertNotIn(self.job, final.job_ids)
        self.assertEqual(app.hr_applicant_id.stage_id.name,
                         "Authority & Insurance Review")
        self.assertEqual(self.env["mail.mail"].search_count([]), mail_before,
                         "migration must not send mail")

    def test_duplicate_stage_name_is_collapsed(self):
        from odoo.addons.prema_owner_operator.hooks import ensure_pipeline
        Stage = self.env["hr.recruitment.stage"]
        original = Stage.search(
            [("name", "=", "Contract Proposal"),
             ("job_ids", "in", self.job.id)], limit=1)
        duplicate = Stage.create({
            "name": "Contract Proposal", "sequence": 99})
        duplicate.write({"job_ids": [(4, self.job.id)]})
        ensure_pipeline(self.env)
        linked = Stage.search(
            [("name", "=", "Contract Proposal"),
             ("job_ids", "in", self.job.id)])
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked, original)
        self.assertNotIn(self.job, duplicate.job_ids)

    def test_other_jobs_keep_their_stage_links(self):
        other = self.env["hr.job"].create({
            "name": "Other pipeline %s" % uuid.uuid4().hex[:6],
            "is_published": True,
        })
        stage = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Equipment Verification")], limit=1)
        stage.write({"job_ids": [(4, other.id)]})
        from odoo.addons.prema_owner_operator.hooks import ensure_pipeline
        ensure_pipeline(self.env)
        self.assertIn(other, stage.job_ids,
                      "another job's stage link must be preserved")
        self.assertNotIn(self.job, stage.job_ids)


@tagged("post_install", "-at_install")
class TestCOIRequestEmail(OwnerOperatorCase):

    def _move_to_authority(self, app):
        stage = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)
        stage.write({"template_id": False})
        app.hr_applicant_id.write({"stage_id": stage.id})
        return stage

    def _coi_mails(self, app):
        return self.env["mail.mail"].search([
            ("subject", "ilike", "Insurance verification required"),
            ("subject", "ilike", app.name),
        ])

    def test_one_correct_email_on_entry(self):
        app = self.Application._create_from_public(self._public_vals(
            business_name="Acme Transport Ltd"))
        before = len(self._coi_mails(app))
        self._move_to_authority(app)
        mails = self._coi_mails(app)
        self.assertEqual(len(mails), before + 1)
        mail = mails[:1]
        body = mail.body_html or ""
        self.assertIn("verification@premafirm.com", body)
        self.assertIn("PremaFirm Inc.", body)
        self.assertIn("994 Westport Cres, Unit 7A", body)
        self.assertIn("Mississauga, ON L5T 1G1", body)
        self.assertIn("Canada", body)
        self.assertIn("Acme Transport Ltd", body)
        self.assertIn(app.name, body)
        self.assertIn("Acme Transport Ltd", mail.subject)
        self.assertIn(app.name, mail.subject)
        self.assertIn(app.partner_id, mail.recipient_ids)
        self.assertEqual(app.partner_id.email, app.email)
        self.assertNotIn("additional insured", body.lower())
        app.invalidate_recordset()
        self.assertTrue(app.coi_request_sent_at)
        self.assertEqual(app.coi_request_count, 1)
        self.assertTrue(app.coi_request_message_id)
        self.assertEqual(app.coi_request_sent_by, self.env.user)

    def test_ordinary_edits_do_not_resend(self):
        app = self.Application._create_from_public(self._public_vals())
        self._move_to_authority(app)
        count = len(self._coi_mails(app))
        app.write({"home_city": "Mississauga",
                   "recent_driving_work": "Changed"})
        self.assertEqual(len(self._coi_mails(app)), count)

    def test_deliberate_resend_is_logged(self):
        app = self.Application._create_from_public(self._public_vals())
        self._move_to_authority(app)
        count = len(self._coi_mails(app))
        app.action_resend_coi_request()
        self.assertEqual(len(self._coi_mails(app)), count + 1)
        app.invalidate_recordset()
        self.assertEqual(app.coi_request_count, 2)
        self.assertTrue(any("re-sent" in (m.body or "")
                            for m in app.message_ids))

    def test_migration_is_silent_and_does_not_advance(self):
        from odoo.addons.prema_owner_operator.hooks import ensure_pipeline
        app = self._applicant("migrate@test.invalid") \
            .owner_operator_application_id
        removed = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Equipment Verification")], limit=1)
        removed.write({"job_ids": [(4, self.job.id)]})
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": removed.id})
        before = len(self._coi_mails(app))
        ensure_pipeline(self.env)
        app.invalidate_recordset()
        self.assertEqual(len(self._coi_mails(app)), before)
        self.assertEqual(app.hr_applicant_id.stage_id.name, "New")


@tagged("post_install", "-at_install")
class TestInboundVerification(OwnerOperatorCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.trusted_insurer_domains",
            "insurer.test,broker.test")
        self.authority = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)

    def _app(self, suffix="insurer", **overrides):
        app = self.Application._create_from_public(self._public_vals(
            email="%s-%s@test.invalid" % (suffix, uuid.uuid4().hex[:6]),
            **overrides))
        app._ensure_checklist()
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": self.authority.id})
        return app

    def _raw(self, app, sender="Broker <broker@insurer.test>",
             subject=None, message_id=None, references=None,
             certificate=True, body="Please find the certificate attached."):
        msg = email.message.EmailMessage()
        msg["Subject"] = subject or (
            "Insurance verification required — %s — %s"
            % (app.name, app.business_name))
        msg["From"] = sender
        msg["To"] = "verification@premafirm.com"
        msg["Message-ID"] = message_id or "<%s@test.invalid>" % uuid.uuid4().hex
        if references:
            msg["References"] = references
            msg["In-Reply-To"] = references
        msg.set_content(body)
        if certificate:
            pdf = _pdf_bytes()
            if not pdf:
                self.skipTest("reportlab is unavailable to build a test PDF")
            msg.add_attachment(pdf, maintype="application", subtype="pdf",
                               filename="coi.pdf")
        return msg.as_bytes()

    def _process(self, raw):
        applicants_before = self.env["hr.applicant"].search_count([])
        thread_id = self.env["mail.thread"].message_process(
            "prema.owner.operator.verification", raw)
        queue = self.env["prema.owner.operator.verification"].browse(
            thread_id)
        self.assertEqual(self.env["hr.applicant"].search_count([]),
                         applicants_before,
                         "verification mail must never create an applicant")
        return queue

    def test_alias_targets_the_queue(self):
        alias = self.env["mail.alias"].search([
            ("alias_name", "=", "verification"),
            ("alias_domain_id.name", "=", "premafirm.com"),
        ], limit=1)
        self.assertTrue(alias)
        self.assertEqual(alias.alias_model_id.model,
                         "prema.owner.operator.verification")

    def test_trusted_match_stays_in_authority_and_creates_one_activity(self):
        app = self._app()
        raw = self._raw(app)
        queue = self._process(raw)
        self.assertEqual(queue.state, "matched")
        self.assertEqual(queue.matched_application_id, app)
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertEqual(doc.status, "received")
        self.assertTrue(doc.attachment_id)
        self.assertFalse(doc.verified_on)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority,
                         "COI receipt must not move the applicant")
        self.assertEqual(len(app.activity_ids), 1,
                         "exactly one reviewer activity is required")
        self.assertTrue(queue.message_ids)
        # Repeat-safe: the duplicate Message-ID is ignored.
        activity_count = len(app.activity_ids)
        queue_count = self.env[
            "prema.owner.operator.verification"].search_count([])
        self.assertFalse(self.env["mail.thread"].message_process(
            "prema.owner.operator.verification", raw))
        self.assertEqual(
            self.env["prema.owner.operator.verification"].search_count([]),
            queue_count)
        self.assertEqual(len(app.activity_ids), activity_count)

    def test_untrusted_match_stays_in_manual_review(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.trusted_insurer_domains", "")
        app = self._app()
        queue = self._process(self._raw(
            app, sender="Broker <broker@unknown.test>"))
        self.assertEqual(queue.state, "matched")
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertFalse(doc.attachment_id)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)
        self.assertTrue(app.activity_ids)

    def test_applicant_forwarded_is_not_insurer_direct(self):
        app = self._app("forwarded")
        queue = self._process(self._raw(
            app, sender="Applicant <%s>" % app.email))
        self.assertEqual(queue.state, "applicant_forwarded")
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertFalse(doc.attachment_id)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)

    def test_missing_certificate_does_not_advance(self):
        app = self._app("missing")
        queue = self._process(self._raw(app, certificate=False))
        self.assertEqual(queue.state, "no_certificate")
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertFalse(doc.attachment_id)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority)

    def test_ambiguous_reference_goes_to_queue(self):
        app1 = self._app("amb1")
        app2 = self._app("amb2")
        queue = self._process(self._raw(
            app1, subject="Certificates for %s and %s"
            % (app1.name, app2.name)))
        self.assertEqual(queue.state, "ambiguous")
        self.assertFalse(queue.matched_application_id)
        self.assertFalse(app1.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer").attachment_id)
        self.assertFalse(app2.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer").attachment_id)

    def test_conflicting_reference_goes_to_queue(self):
        app1 = self._app("conf1")
        app2 = self._app("conf2")
        msg = app2.message_post(body="Prior thread")
        queue = self._process(self._raw(
            app1, subject="Certificate for %s" % app1.name,
            references=msg.message_id))
        self.assertEqual(queue.state, "conflict")
        self.assertFalse(app1.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer").attachment_id)

    def test_duplicate_message_is_ignored(self):
        app = self._app("dupe")
        raw = self._raw(app)
        first = self._process(raw)
        count = self.env["prema.owner.operator.verification"].search_count([])
        second = self.env["mail.thread"].message_process(
            "prema.owner.operator.verification", raw)
        self.assertFalse(second)
        self.assertEqual(
            self.env["prema.owner.operator.verification"].search_count([]),
            count)
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertEqual(doc.attachment_id, first.matched_application_id
                         .document_ids.filtered(
                             lambda d: d.document_type == "coi_insurer")
                         .attachment_id)

    def test_unmatched_reference_stays_in_queue(self):
        before = self.env["prema.owner.operator.verification"].search_count([])
        queue = self._process(self._raw(
            type("X", (), {"name": "OO-999999",
                           "business_name": "Nobody",
                           "email": "x@test.invalid"})()))
        self.assertEqual(queue.state, "unmatched")
        self.assertFalse(queue.matched_application_id)
        self.assertEqual(
            self.env["prema.owner.operator.verification"].search_count([]),
            before + 1)

    def test_outsider_cannot_read_the_queue(self):
        outsider = self.env["res.users"].create({
            "login": "vq.outsider.%s" % uuid.uuid4().hex[:6],
            "partner_id": self.env["res.partner"].create(
                {"name": "VQ Outsider"}).id,
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            self.env["prema.owner.operator.verification"].with_user(
                outsider).search([])

    def test_manual_attach_after_review(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.trusted_insurer_domains", "")
        app = self._app("manual")
        queue = self._process(self._raw(
            app, sender="Broker <broker@unknown.test>"))
        self.assertEqual(queue.state, "matched")
        queue.write({"matched_application_id": app.id})
        queue.action_manual_attach()
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        self.assertEqual(doc.status, "received")
        self.assertTrue(doc.attachment_id)
        self.assertEqual(app.hr_applicant_id.stage_id, self.authority,
                         "manual attach must not move the applicant")


@tagged("post_install", "-at_install")
class TestSigningCompletion(OwnerOperatorCase):

    def _completed_request(self, app):
        pdf = _pdf_bytes()
        if not pdf:
            self.skipTest("reportlab is unavailable to build a test PDF")
        attachment = self.env["ir.attachment"].create({
            "name": "agreement.pdf", "raw": pdf,
            "mimetype": "application/pdf",
        })
        template = self.env["sign.template"].create({
            "attachment_id": attachment.id,
        })
        role = self.env.ref("sign.sign_item_role_default")
        request = self.env["sign.request"].with_context(
            no_sign_mail=True).create({
                "template_id": template.id,
                "reference": app.name,
                "subject": "Owner-Operator Agreement — %s" % app.name,
                "request_item_ids": [(0, 0, {
                    "partner_id": app.partner_id.id,
                    "role_id": role.id,
                })],
            })
        request.write({"state": "signed"})
        request.request_item_ids.write({
            "state": "completed",
            "signing_date": fields.Date.today(),
        })
        request.write({
            "completed_document": base64.b64encode(pdf),
        })
        app.write({
            "sign_request_id": request.id,
            "sign_template_id": template.id,
            "contract_sent_on": fields.Datetime.now(),
        })
        return request, attachment

    def test_genuine_completion_advances_once_tags_and_emails_once(self):
        app = self.Application._create_from_public(self._public_vals(
            home_city="Mississauga", home_province="ON"))
        self._complete(app, sign=False)
        app.write({"home_city": "Mississauga", "home_province": "ON"})
        existing_tag = self.env["hr.applicant.category"].search(
            [("name", "=", "Reserve")], limit=1) \
            or self.env["hr.applicant.category"].create({"name": "Reserve"})
        app.hr_applicant_id.write({"categ_ids": [(4, existing_tag.id)]})
        self._completed_request(app)
        before = self.env["mail.mail"].search_count([
            ("subject", "ilike", "Owner-operator account setup"),
        ])
        app._cron_sync_signatures()
        app.invalidate_recordset()
        self.assertTrue(app.contract_signed_on)
        self.assertTrue(app.contract_completed_on)
        self.assertEqual(app.hr_applicant_id.stage_id.name,
                         "Contract Signed")
        names = app.hr_applicant_id.categ_ids.mapped("name")
        self.assertIn("Reserve", names)
        self.assertIn("Mississauga", names)
        self.assertIn("ON", names)
        self.assertTrue(app.setup_email_sent_at)
        setup_mails = self.env["mail.mail"].search([
            ("subject", "ilike", "Owner-operator account setup"),
        ])
        self.assertEqual(len(setup_mails), before + 1)
        body = setup_mails[:1].body_html or ""
        self.assertIn("We have received your signed documents", body)
        self.assertNotIn("immediately", body.lower())
        signed_doc = app.document_ids.filtered(
            lambda d: d.document_type == "signed_agreement")
        self.assertEqual(signed_doc.status, "received")
        self.assertTrue(signed_doc.attachment_id)
        # duplicate completion events do nothing
        app._cron_sync_signatures()
        self.assertEqual(len(self.env["mail.mail"].search([
            ("subject", "ilike", "Owner-operator account setup"),
        ])), before + 1)
        self.assertEqual(
            app.hr_applicant_id.stage_id.name, "Contract Signed")

    def test_incomplete_sign_request_does_not_advance(self):
        app = self.Application._create_from_public(self._public_vals())
        self._complete(app, sign=False)
        request, _attachment = self._completed_request(app)
        request.write({"state": "sent"})
        request.request_item_ids.write({"state": "sent"})
        app._cron_sync_signatures()
        app.invalidate_recordset()
        self.assertFalse(app.contract_signed_on)
        self.assertNotEqual(app.hr_applicant_id.stage_id.name,
                            "Contract Signed")

    def test_missing_location_flags_instead_of_inventing(self):
        app = self.Application._create_from_public(self._public_vals())
        self._complete(app, sign=False)
        app.write({"home_city": False, "home_province": False})
        self._completed_request(app)
        app._cron_sync_signatures()
        app.invalidate_recordset()
        self.assertTrue(app.home_base_tag_issue)
        self.assertFalse(app.hr_applicant_id.categ_ids.filtered(
            lambda c: c.name in ("Mississauga", "ON")))


@tagged("post_install", "-at_install")
class TestNotificationAndMultiSelect(OwnerOperatorCase):

    def _raw(self, **overrides):
        raw = {
            "partner_name": "Notify Driver",
            "email_from": "notify@test.invalid",
            "partner_phone": "+1 416 555 0199",
            "LegalBusinessName": "Notify Transport",
            "BusinessStructure": "sole",
            "HomeCity": "Toronto",
            "HomeProvince": "ON",
            "CommercialDrivingYears": "6–10 years",
            "DriverLicenceClass": "AZ",
            "LicenceJurisdiction": "Ontario",
            "OwnerOperatorYears": "3–5 years",
            "LicenceSuspension": "No",
            "RecentCollisions": "None",
            "RecentConvictions": "None",
            "VehicleType": "straight",
            "TruckMakeModel": "Freightliner M2",
            "TruckYear": "2013",
            "VehicleOwnership": "Own",
            "Transmission": "Automatic",
            "OdometerKm": "100000",
            "TruckPlateJurisdiction": "N 1 / Ontario",
            "CargoBodyType": "dry",
            "LegalPayloadLb": "10000",
            "PalletCapacity": "12",
            "CargoLengthFt": "26",
            "CargoWidthIn": "100",
            "CargoHeightIn": "96",
            "DoorWidthIn": "96",
            "DoorHeightIn": "87",
            "Liftgate": "no",
            "DockHeightCapable": "yes",
            "AirRide": "yes",
            "PreferredAreaTorontoGTA": "1",
            "PreferredAreaQuebec": "1",
            "WorkPreferenceLocal": "1",
            "WorkPreferenceRegional": "1",
            "LoadNotificationChannel": "whatsapp",
            "WhatsAppConsentGranted": "1",
            "WhatsAppNumber": "+1 416 555 0100",
            "ApplicationDeclaration": "Confirmed",
        }
        raw.update(overrides)
        return raw

    def test_whatsapp_requires_number_and_consent(self):
        vals, errors = normalise_public(self._raw(
            WhatsAppNumber="", WhatsAppConsentGranted=""), strict=True)
        self.assertTrue(any("WhatsApp number" in e for e in errors))
        self.assertTrue(any("agree to WhatsApp" in e for e in errors))
        vals, errors = normalise_public(self._raw(), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["whatsapp_number"], "+14165550100")
        self.assertTrue(vals["whatsapp_consent_granted"])
        self.assertTrue(vals["whatsapp_consent_text"].startswith(
            "I agree to receive load quote requests"))
        self.assertEqual(vals["load_notification_channel"], "whatsapp")

    def test_use_contact_phone_and_use_application_email(self):
        vals, errors = normalise_public(self._raw(
            WhatsAppNumber="", UseContactPhoneForWhatsApp="1"), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["whatsapp_number"], "+14165550199")
        vals, errors = normalise_public(self._raw(
            LoadNotificationChannel="email",
            UseApplicationEmailForNotifications="1"), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["load_notification_email"],
                         "notify@test.invalid")
        self.assertFalse(vals.get("whatsapp_consent_granted"))

    def test_multi_select_answers_survive(self):
        vals, errors = normalise_public(self._raw(), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(
            set(app.preferred_service_area_ids.mapped("code")),
            {"toronto_gta", "quebec"})
        self.assertEqual(
            set(app.work_preference_ids.mapped("code")),
            {"local", "regional"})
        self.assertEqual(app.load_notification_channel, "whatsapp")
        self.assertEqual(app.whatsapp_number, "+14165550100")
        self.assertTrue(app.whatsapp_consent_granted)
        self.assertTrue(app.whatsapp_consent_at)

    def test_preference_change_is_audited(self):
        vals, _errors = normalise_public(self._raw(), strict=True)
        app = self.Application._create_from_public(vals)
        log_count = len(app.notification_preference_log_ids)
        app.write({
            "load_notification_channel": "email",
            "load_notification_email": "loads@test.invalid",
            "load_notification_opt_out": True,
            "load_notification_opt_out_at": fields.Datetime.now(),
            "whatsapp_consent_granted": False,
        })
        self.assertGreater(len(app.notification_preference_log_ids),
                           log_count)
        app.invalidate_recordset()
        last = self.env["prema.owner.operator.notification.log"].search(
            [("application_id", "=", app.id)],
            order="id desc", limit=1)
        self.assertEqual(last.channel, "email")
        self.assertTrue(last.opt_out)


@tagged("post_install", "-at_install")
class TestComplianceReviewOperability(OwnerOperatorCase):

    def test_prepare_profile_verify_coi_and_approve(self):
        app = self._applicant("compliance-review@test.invalid") \
            .owner_operator_application_id
        temporary_operator = self._complete(app, sign=True)
        app.write({"operator_id": False})
        temporary_operator.unlink()

        action = app.action_prepare_operator()
        app.invalidate_recordset()
        self.assertTrue(app.operator_id)
        self.assertFalse(app.fleet_vehicle_id,
                         "preparing a profile must not approve dispatch")
        self.assertNotEqual(app.hr_applicant_id.stage_id.name,
                            "Approved — Ready for Dispatch")
        self.assertEqual(action["res_model"], "prema.owner.operator")

        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        doc.write({
            "status": "received",
            "source": "insurer",
            "expiry_date": fields.Date.add(
                fields.Date.context_today(app), years=1),
        })
        app.operator_id.write({"coi_document_id": doc.id})
        app.operator_id.action_verify_coi_with_insurer()
        app.operator_id.action_mark_authority_verified()
        app.operator_id.invalidate_recordset()
        self.assertTrue(app.operator_id.coi_verified_direct)
        self.assertTrue(app.operator_id.compliance_ok)
        doc.action_verify()
        app.invalidate_recordset()
        self.assertTrue(app.can_approve, app.missing_verification)
        vehicle = app.action_approve()
        self.assertTrue(vehicle)
        self.assertEqual(app.hr_applicant_id.stage_id.name,
                         "Approved — Ready for Dispatch")

    def test_contract_proposal_requires_completed_compliance_review(self):
        app = self._applicant("contract-gate@test.invalid") \
            .owner_operator_application_id
        self._complete(app, sign=False)
        contract = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Contract Proposal"),
             ("job_ids", "in", self.job.id)], limit=1)
        app.operator_id.write({"coi_verified_direct": False})
        app.operator_id.invalidate_recordset()
        with self.assertRaises(UserError) as cm:
            app._guard_transition(contract)
        self.assertIn("compliance review", str(cm.exception).lower())
        app.operator_id.write({"coi_verified_direct": True})
        app.operator_id.invalidate_recordset()
        app._guard_transition(contract)  # must not raise


@tagged("post_install", "-at_install")
class TestEquipmentGate(OwnerOperatorCase):

    def test_incomplete_equipment_blocks_approval(self):
        app = self._applicant("gearmissing@test.invalid") \
            .owner_operator_application_id
        self._complete(app, sign=False)
        app.write({
            "vehicle_type": "straight",
            "cargo_body_type": False,
            "payload_lbs": 0,
            "box_length_ft": 0,
            "cargo_width_in": 0,
            "cargo_height_in": 0,
            "door_width_in": 0,
            "door_clearance_in": 0,
        })
        self.assertFalse(app.equipment_complete)
        self.assertTrue(app.equipment_missing)
        with self.assertRaises(UserError):
            app.action_approve()
        with self.assertRaises(UserError) as cm:
            app._guard_transition(self.env["hr.recruitment.stage"].search(
                [("name", "=", "Approved — Ready for Dispatch"),
                 ("job_ids", "in", self.job.id)], limit=1))
        self.assertIn("equipment", str(cm.exception).lower())

    def test_power_only_tractor_needs_no_cargo_capacity(self):
        app = self._applicant("poweronly@test.invalid") \
            .owner_operator_application_id
        self._complete(app, sign=False)
        app.write({
            "vehicle_type": "tractor",
            "trailer_ownership": "power_only",
            "cargo_body_type": False,
            "payload_lbs": 0,
            "pallet_capacity": 0,
            "box_length_ft": 0,
            "cargo_width_in": 0,
            "cargo_height_in": 0,
            "door_width_in": 0,
            "door_clearance_in": 0,
            "liftgate": "no",
            "dock_height_capable": "no",
        })
        self.assertTrue(app.equipment_complete, app.equipment_missing)

    def test_zero_is_distinct_from_not_provided(self):
        app = self._applicant("zerovalue@test.invalid") \
            .owner_operator_application_id
        self._complete(app, sign=False)
        app.write({
            "vehicle_type": "straight",
            "liftgate": "no",
            "payload_lbs": 0,
            "equipment_provided": {"payload_lbs": True},
        })
        self.assertNotIn("Legal payload", app.equipment_missing or "")
        app.write({"equipment_provided": {}})
        self.assertIn("Legal payload", app.equipment_missing or "")

    def test_tractor_approval_does_not_copy_cargo_to_fleet(self):
        app = self._applicant("trailerfleet@test.invalid") \
            .owner_operator_application_id
        self._complete(app, sign=True)
        app.write({
            "vehicle_type": "tractor",
            "trailer_ownership": "own",
            "trailer_type": "dry",
            "trailer_length_ft": 53,
            "cargo_body_type": "dry",
            "payload_lbs": 45000,
            "pallet_capacity": 30,
            "box_length_ft": 53,
            "cargo_width_in": 100,
            "cargo_height_in": 110,
            "door_width_in": 100,
            "door_clearance_in": 110,
            "liftgate": "no",
            "dock_height_capable": "yes",
        })
        vehicle = app.action_approve()
        self.assertFalse(vehicle.x_max_payload_lbs)
        self.assertFalse(vehicle.x_max_pallets)
        self.assertFalse(vehicle.x_cargo_box_length_ft)
        self.assertFalse(vehicle.x_box_interior_height_ft)
        self.assertFalse(vehicle.x_door_clearance_in)

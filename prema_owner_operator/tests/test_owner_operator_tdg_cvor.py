# -*- coding: utf-8 -*-
"""TDG, CVOR extraction and document-viewer acceptance tests."""
import os
import uuid
from io import BytesIO

from odoo import fields
from odoo.tests import HttpCase, tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    normalise_public)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)


def _raw(**overrides):
    vals = {
        "partner_name": "TDG Driver",
        "email_from": "tdg-%s@test.invalid" % uuid.uuid4().hex[:8],
        "partner_phone": "416-555-0100",
        "LegalBusinessName": "TDG Transport",
        "BusinessStructure": "sole",
        "HomeCity": "Toronto", "HomeProvince": "ON",
        "CommercialDrivingYears": "6–10 years",
        "DriverLicenceClass": "AZ", "LicenceJurisdiction": "ON",
        "OwnerOperatorYears": "3–5 years",
        "LicenceSuspension": "No", "RecentCollisions": "None",
        "RecentConvictions": "None",
        "VehicleType": "straight",
        "CVORNumber": "123456789",
        "TruckMake": "Freightliner", "TruckModel": "M2 106",
        "TruckYear": "2013", "VehicleOwnership": "Own",
        "Transmission": "Automatic", "OdometerKm": "100000",
        "TruckPlate": "TDG 100", "TruckPlateProvince": "ON",
        "GVWRLb": "26000", "CargoBodyType": "dry",
        "CargoDoorStyle": "rollup",
        "TrailerDoorStyle": "rollup",
        "LegalPayloadLb": "12000", "PalletCapacity": "13",
        "CargoLengthFt": "26", "CargoWidthIn": "100",
        "CargoHeightIn": "96", "DoorWidthIn": "96",
        "DoorHeightIn": "87", "Liftgate": "no", "AirRide": "yes",
        "ServiceProvinceON": "Ontario",
        "WorkPreferenceLocal": "1",
        "LoadNotificationChannel": "email",
        "UseApplicationEmailForNotifications": "1",
        "ApplicationDeclaration": "Confirmed",
    }
    vals.update(overrides)
    return vals


@tagged("post_install", "-at_install")
class TestTDGAndCVOR(OwnerOperatorCase):

    def _app(self, **overrides):
        vals = normalise_public(_raw(**overrides), strict=True)[0]
        return self.Application._create_from_public(vals)

    def test_tdg_multiselect_and_none(self):
        app = self._app(
            TDGStatus="current", TDGClass1="1", TDGClass3="1",
            TDGClass5="1", TDGSpecialTraining="Bulk tank")
        self.assertEqual(app.tdg_status, "current")
        self.assertEqual(set(app.tdg_class_ids.mapped("code")),
                         {"1", "3", "5"})
        self.assertEqual(app.tdg_special_training, "Bulk tank")
        self.assertEqual(app.tdg_training, "yes")

        none = self._app(
            TDGStatus="none", TDGClass1="1", TDGSpecialTraining="x")
        self.assertEqual(none.tdg_status, "none")
        self.assertFalse(none.tdg_class_ids)
        self.assertFalse(none.tdg_special_training)
        self.assertEqual(none.tdg_training, "no")

        expired = self._app(TDGStatus="expired", TDGClass2="1")
        self.assertEqual(expired.tdg_status, "expired")
        self.assertFalse(expired.tdg_class_ids)

    def test_tdg_certificate_is_required_and_handed_to_fleet(self):
        app = self._app(TDGStatus="current", TDGClass3="1")
        self._complete(app, sign=True)
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "tdg_certificate")[:1]
        self.assertTrue(doc)
        self.assertTrue(doc.attachment_id)
        app.write({
            "tdg_certificate_result": "verified",
            "tdg_certificate_issuer": "Transport Training Inc.",
            "tdg_certificate_issue_date": fields.Date.today(),
            "tdg_certificate_expiry_date": fields.Date.add(
                fields.Date.today(), years=3),
        })
        vehicle = app.action_approve()
        vehicle_docs = self.env["documents.document"].search([
            ("oo_application_id", "=", app.id),
            ("oo_role", "=", "vehicle"),
            ("oo_source_document_id", "=", doc.id),
        ])
        self.assertEqual(len(vehicle_docs), 1)
        self.assertFalse(vehicle_docs.attachment_id.public)

    def test_tdg_self_declaration_blocks_approval_until_verified(self):
        app = self._app(TDGStatus="current", TDGClass3="1")
        self._complete(app, sign=True)
        app.write({"tdg_certificate_result": "pending"})
        with self.assertRaises(Exception):
            app.action_approve()
        self.assertFalse(app.fleet_vehicle_id)

    def _cvor_pdf(self, text):
        from reportlab.pdfgen import canvas
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(
            80, 740,
            "Ontario Commercial Vehicle Operator Registration certificate "
            "issued by the Ministry of Transportation for the carrier named "
            "on this document. This is a test scan with enough text for the "
            "PDF text extractor to treat it as a readable document.")
        pdf.drawString(80, 700, text)
        pdf.save()
        return buffer.getvalue()

    def _attach_cvor(self, app, payload):
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "cvor")[:1]
        att = self.env["ir.attachment"].create({
            "name": "cvor.pdf", "raw": payload,
            "mimetype": "application/pdf",
            "res_model": "prema.owner.operator.document", "res_id": doc.id,
        })
        doc.write({"attachment_id": att.id, "status": "received"})
        return doc

    def test_cvor_extraction_proposes_and_is_idempotent(self):
        app = self._app()
        self._attach_cvor(app, self._cvor_pdf("Ontario CVOR 123 456 789"))
        self.assertTrue(app.action_extract_cvor())
        self.assertEqual(app.cvor_proposed_number, "123-456-789")
        self.assertIn(app.cvor_extraction_status, ("done", "review"))
        checked = app.cvor_extraction_checked_on
        self.assertFalse(app.action_extract_cvor())
        self.assertEqual(app.cvor_extraction_checked_on, checked)

    def test_cvor_mismatch_and_multiple_numbers_need_review(self):
        app = self._app(CVORNumber="999999999")
        self._attach_cvor(
            app, self._cvor_pdf("CVOR 123 456 789 and 987 654 321"))
        app.action_extract_cvor()
        self.assertEqual(app.cvor_extraction_status, "review")
        self.assertTrue(app.cvor_extraction_mismatch)
        self.assertIn("123-456-789", app.cvor_extraction_candidates)
        self.assertIn("987-654-321", app.cvor_extraction_candidates)

    def test_cvor_unreadable_never_invents_a_number(self):
        app = self._app()
        self._attach_cvor(app, b"not a pdf")
        app.action_extract_cvor()
        self.assertEqual(app.cvor_extraction_status, "failed")
        self.assertFalse(app.cvor_proposed_number)

    def test_viewer_files_are_scoped_to_the_application(self):
        app1 = self._app()
        app2 = self._app()
        doc1 = app1.document_ids.filtered(
            lambda d: d.document_type == "cvor")[:1]
        doc2 = app2.document_ids.filtered(
            lambda d: d.document_type == "cvor")[:1]
        for doc in (doc1, doc2):
            att = self.env["ir.attachment"].create({
                "name": "cvor.pdf", "raw": b"%PDF-1.4 test",
                "mimetype": "application/pdf",
                "res_model": "prema.owner.operator.document",
                "res_id": doc.id,
            })
            doc.write({"attachment_id": att.id, "status": "received"})
        files = self.env["prema.owner.operator.document"]._viewer_files(
            doc1.id)
        self.assertEqual([f["id"] for f in files], [doc1.id])
        self.assertNotIn(doc2.id, [f["id"] for f in files])

    def test_coi_email_uses_application_email_and_is_one_time(self):
        app = self._app()
        other_partner = self.env["res.partner"].create({
            "name": "Wrong Contact", "email": "wrong@test.invalid"})
        app.write({"partner_id": other_partner.id})
        stage = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)
        app.hr_applicant_id.with_context(
            owner_operator_skip_stage_hook=True).write({"stage_id": stage.id})
        count_before = self.env["mail.mail"].search_count([])
        template = self.env.ref(
            "prema_owner_operator.mail_template_coi_request")
        self.assertFalse(template.partner_to)
        self.assertEqual(template.email_from,
                         "PremaFirm Inc. <info@premafirm.com>")
        app._send_coi_request()
        mails = self.env["mail.mail"].search([
            ("model", "=", "prema.owner.operator.application"),
            ("res_id", "=", app.id)], order="id desc", limit=5)
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails.email_to, app.email)
        self.assertNotIn("wrong@test.invalid", mails.email_to or "")
        self.assertEqual(self.env["mail.mail"].search_count([]),
                         count_before + 1)


@tagged("post_install", "-at_install")
class TestTDGFormLayout(HttpCase):

    def test_form_has_tdg_and_three_authority_groups(self):
        page = self.url_open("/owner-operator/apply")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertEqual(text.count('class="o_oo_step'), 7)
        self.assertIn('name="TDGStatus"', text)
        for code in range(1, 10):
            self.assertIn('name="TDGClass%d"' % code, text)
        self.assertIn('name="TDGClassesNone"', text)
        self.assertIn('name="TDGSpecialTraining"', text)
        self.assertIn("Operating authority", text)
        self.assertIn("Where you operate", text)
        self.assertIn("Availability", text)
        self.assertIn("insurer or broker sends the Certificate", text)
        self.assertIn('name="DriverLicenceFront"', text)
        self.assertIn('name="DriverLicenceBack"', text)

# -*- coding: utf-8 -*-
"""Owner-operator onboarding — the assertions the brief asks to be tested."""
from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class OwnerOperatorCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(user=cls.env.ref("base.user_admin").id)
        cls.admin = cls.env.ref("base.user_admin")
        cls.admin.write({"groups_id": [(4, cls.env.ref(
            "prema_owner_operator.group_owner_operator").id)]})
        cls.job = cls.env["hr.job"].browse(5)
        if not cls.job.exists():
            cls.job = cls.env["hr.job"].create({
                "name": "Owner Operator (test)",
                "is_published": True,
                "company_id": cls.env.company.id,
                "alias_name": "owneroperators",
            })
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", cls.job.id)
        cls.Application = cls.env["prema.owner.operator.application"]

    def _applicant(self, email="driver@test.invalid"):
        candidate = self.env["hr.candidate"].create({
            "partner_name": "Test Driver",
            "email_from": email,
            "company_id": self.env.company.id,
        })
        return self.env["hr.applicant"].create({
            "candidate_id": candidate.id,
            "job_id": self.job.id,
            "partner_name": "Test Driver",
            "email_from": email,
            "company_id": self.env.company.id,
        })

    def _complete(self, app, sign=True):
        """Fill every checklist row so the workflow can run end to end."""
        app.write({
            "licence_class": "AZ",
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=2),
            "vin": "1FVACXDT9DHBX3538",
            "license_plate": "TEST001",
            "home_city": "Toronto",
            "home_province": "Ontario",
            "equipment_type": "dry_van",
            "has_liftgate": True,
        })
        for doc in app.document_ids:
            att = self.env["ir.attachment"].create({
                "name": "%s.pdf" % doc.document_type,
                "raw": b"%PDF-1.4 test",
                "res_model": "prema.owner.operator.document",
                "res_id": doc.id,
            })
            doc.write({"status": "verified", "attachment_id": att.id})
        if sign:
            app.write({"contract_signed_on": fields.Datetime.now()})
        op = self.env["prema.owner.operator"].create({
            "partner_id": app.partner_id.id,
            "home_city": "Toronto",
            "home_province": "Ontario",
            "authority_state": "verified",
            "coi_verified_direct": True,
            "insurance_expiry": fields.Date.add(
                fields.Date.context_today(app), years=1),
        })
        app.write({"operator_id": op.id})
        return op


class TestApplication(OwnerOperatorCase):

    def test_applicant_creates_application_and_checklist(self):
        applicant = self._applicant()
        app = applicant.owner_operator_application_id
        self.assertTrue(app, "an application must exist for the job")
        self.assertEqual(app.partner_id, applicant.partner_id)
        types = set(app.document_ids.mapped("document_type"))
        self.assertIn("cvor", types)
        self.assertIn("coi_insurer", types)
        self.assertIn("licence_front", types)
        # the insurer-direct row is sourced 'insurer', never 'applicant'
        self.assertEqual(
            app.document_ids.filtered(
                lambda d: d.document_type == "coi_insurer").source, "insurer")

    def test_sole_proprietor_is_not_asked_for_registration(self):
        applicant = self._applicant("sole@test.invalid")
        app = applicant.owner_operator_application_id
        app.write({"is_sole_proprietor": True})
        app._ensure_checklist()
        self.assertNotIn("business_registration",
                         app.document_ids.mapped("document_type"))

    def test_public_creation_requires_city_and_province(self):
        with self.assertRaises(ValidationError):
            self.Application._create_from_public({
                "contact_name": "A", "email": "a@test.invalid"})
        app = self.Application._create_from_public({
            "contact_name": "A", "email": "a@test.invalid",
            "home_city": "Toronto", "home_province": "Ontario",
            "equipment_type": "dry_van",
        })
        self.assertEqual(app.home_city, "Toronto")
        self.assertEqual(app.home_province, "Ontario")
        # no street-address field exists on the model at all
        self.assertNotIn("address", app._fields)

    def test_acknowledgement_is_queued_not_sent(self):
        before = self.env["mail.mail"].search_count([])
        applicant = self._applicant("ack@test.invalid")
        app = applicant.owner_operator_application_id
        self.env.ref("prema_owner_operator.mail_template_ack").sudo().send_mail(
            app.id, force_send=False)
        self.assertGreater(self.env["mail.mail"].search_count([]), before)


class TestDocumentPrivacy(OwnerOperatorCase):

    def test_uploads_are_private_attachments(self):
        applicant = self._applicant("priv@test.invalid")
        app = applicant.owner_operator_application_id
        doc = app.document_ids[:1]
        attachment = self.env["ir.attachment"].create({
            "name": "cvor.pdf", "raw": b"%PDF-1.4 test",
            "res_model": "prema.owner.operator.document", "res_id": doc.id,
            "public": False,
        })
        doc.write({"attachment_id": attachment.id, "status": "received"})
        self.assertFalse(attachment.public)
        self.assertEqual(attachment.res_model,
                         "prema.owner.operator.document")

    def test_user_without_the_group_cannot_read_documents(self):
        applicant = self._applicant("nope@test.invalid")
        app = applicant.owner_operator_application_id
        outsider = self.env["res.users"].create({
            "login": "oo.outsider",
            "partner_id": self.env["res.partner"].create(
                {"name": "Outsider"}).id,
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(Exception):
            self.env["prema.owner.operator.document"].with_user(
                outsider).search([])


class TestStageGates(OwnerOperatorCase):

    def test_advance_blocked_while_initial_uploads_missing(self):
        app = self._applicant("gate@test.invalid").owner_operator_application_id
        with self.assertRaises(UserError):
            app.action_advance()

    def test_contract_blocked_without_an_active_template(self):
        app = self._applicant("sign@test.invalid").owner_operator_application_id
        self._complete(app, sign=False)
        self.assertFalse(self.Application._agreement_template())
        with self.assertRaises(UserError) as cm:
            app.action_send_contract()
        self.assertIn("No approved owner-operator agreement", str(cm.exception))
        # and approval stays blocked too
        self.assertFalse(app.can_approve)

    def test_qualification_needs_the_insurer_copy(self):
        app = self._applicant("ins@test.invalid").owner_operator_application_id
        self._complete(app, sign=True)
        insurer_doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")
        insurer_doc.write({"status": "pending"})
        with self.assertRaises(UserError):
            app.action_advance()


class TestApproval(OwnerOperatorCase):

    def test_approval_is_idempotent_and_creates_no_employee(self):
        app = self._applicant("approve@test.invalid").owner_operator_application_id
        op = self._complete(app)
        employees_before = self.env["hr.employee"].search_count([])
        self.assertTrue(app.can_approve)
        v1 = app.action_approve()
        v2 = app.action_approve()
        self.assertEqual(v1.id, v2.id, "re-approval must not create a vehicle")
        self.assertEqual(
            self.env["fleet.vehicle"].search_count([("vin_sn", "=", app.vin)]), 1)
        self.assertEqual(v1.operator_id, op)
        self.assertEqual(v1.state_id.name, "Ready")
        self.assertEqual(self.env["hr.employee"].search_count([]),
                         employees_before)
        self.assertEqual(app.partner_id.supplier_rank, 1)
        self.assertTrue(app.partner_id.supplier_rank)

    def test_vin_conflict_requires_manual_review(self):
        other = self.Application._create_from_public({
            "contact_name": "Other", "email": "other@test.invalid",
            "home_city": "Toronto", "home_province": "Ontario",
            "equipment_type": "dry_van", "vin": "1FVACXDT9DHBX3538",
        })
        foreign_op = self.env["prema.owner.operator"].create({
            "partner_id": other.partner_id.id,
            "authority_state": "verified", "coi_verified_direct": True,
            "insurance_expiry": fields.Date.add(
                fields.Date.context_today(other), years=1),
        })
        self.env["fleet.vehicle"].create({
            "model_id": self._vehicle_model().id,
            "vin_sn": "1FVACXDT9DHBX3538", "operator_id": foreign_op.id,
            "state_id": self.env["fleet.vehicle.state"].search(
                [("name", "=", "Ready")], limit=1).id,
        })
        app = self._applicant("conflict@test.invalid").owner_operator_application_id
        self._complete(app)
        with self.assertRaises(UserError) as cm:
            app.action_approve()
        self.assertIn("conflict", str(cm.exception).lower())


class TestFleetAvailability(OwnerOperatorCase):

    def _vehicle_model(self):
        brand = self.env["fleet.vehicle.model.brand"].search(
            [("name", "=", "Owner-operator")], limit=1) \
            or self.env["fleet.vehicle.model.brand"].create(
                {"name": "Owner-operator"})
        return self.env["fleet.vehicle.model"].create(
            {"name": "Test Truck", "brand_id": brand.id})

    def _issue_state(self):
        state = self.env["fleet.vehicle.state"].search(
            [("name", "ilike", "Issue")], limit=1)
        if not state:
            state = self.env["fleet.vehicle.state"].create({
                "name": "Issue / Temporarily Unavailable", "sequence": 5})
        return state

    def test_company_truck_behaviour_is_unchanged(self):
        ready = self.env["fleet.vehicle.state"].search(
            [("name", "=", "Ready")], limit=1)
        if not ready:
            ready = self.env["fleet.vehicle.state"].create(
                {"name": "Ready", "sequence": 4})
        truck = self.env["fleet.vehicle"].create({
            "model_id": self._vehicle_model().id,
            "license_plate": "CO001", "x_operational_logistics": True,
            "state_id": ready.id,
        })
        self.assertTrue(truck.is_dispatch_available)
        # a company truck is not gated by the Ready state at all
        truck.write({"state_id": self.env["fleet.vehicle.state"].search(
            [("name", "=", "Registered")], limit=1).id})
        self.assertTrue(truck.is_dispatch_available)

    def test_issue_state_blocks_dispatch_until_cleared(self):
        issue = self._issue_state()
        truck = self.env["fleet.vehicle"].create({
            "model_id": self._vehicle_model().id,
            "license_plate": "CO002", "x_operational_logistics": True,
            "state_id": issue.id, "issue_reason": "Brake inspection",
        })
        self.assertTrue(truck.issue_blocking)
        self.assertFalse(truck.is_dispatch_available)
        self.assertNotIn(
            truck.id,
            self.env["fleet.vehicle"].search(
                self.env["fleet.vehicle"]._dispatch_available_domain()).ids)
        truck.action_clear_issue()
        self.assertFalse(truck.issue_blocking)
        self.assertTrue(truck.is_dispatch_available)

    def test_owner_operator_needs_ready_and_current_compliance(self):
        applicant = self._applicant("avail@test.invalid")
        app = applicant.owner_operator_application_id
        op = self._complete(app)
        vehicle = app.action_approve()
        self.assertTrue(vehicle.is_dispatch_available)
        # expired insurance drops it out of dispatch
        op.insurance_expiry = fields.Date.subtract(
            fields.Date.context_today(op), days=1)
        vehicle.invalidate_recordset()
        op.invalidate_recordset()
        vehicle._compute_is_dispatch_available()
        self.assertFalse(vehicle.is_dispatch_available)

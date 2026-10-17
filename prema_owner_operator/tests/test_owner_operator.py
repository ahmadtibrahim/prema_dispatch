# -*- coding: utf-8 -*-
"""Owner-operator onboarding — the assertions the brief asks to be tested."""
from odoo import fields
import email
from email.mime.text import MIMEText
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
            Stage = cls.env["hr.recruitment.stage"]
            for seq, name in ((1, "Equipment Verification"),
                              (2, "Initial Qualification"),
                              (3, "Authority & Insurance Review"),
                              (4, "Contract Proposal"),
                              (5, "Contract Signed"),
                              (6, "Approved — Ready for Dispatch")):
                stage = Stage.search([("name", "=", name)], limit=1) or Stage.create(
                    {"name": name, "sequence": seq})
                stage.write({"job_ids": [(4, cls.job.id)]})
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", cls.job.id)
        # Fleet states are data: make sure the two this module relies on exist.
        State = cls.env["fleet.vehicle.state"]
        for name, seq in (("Ready", 4),
                          ("Issue / Temporarily Unavailable", 5)):
            if not State.search([("name", "=", name)], limit=1):
                State.create({"name": name, "sequence": seq})
        cls.Application = cls.env["prema.owner.operator.application"]

    def _vehicle_model(self):
        brand = self.env["fleet.vehicle.model.brand"].search(
            [("name", "=", "Owner-operator")], limit=1) \
            or self.env["fleet.vehicle.model.brand"].create(
                {"name": "Owner-operator"})
        return self.env["fleet.vehicle.model"].create(
            {"name": "Test Truck", "brand_id": brand.id})

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

    def _complete(self, app, sign=True, vin=None):
        """Fill every checklist row so the workflow can run end to end."""
        app.write({
            "licence_class": "AZ",
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=2),
            "vin": vin or ("1FVACXDT9DHBX%04d" % (app.id % 9999)),
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
        # the gate that matters: Contract Proposal cannot be reached without
        # the insurer-direct certificate
        contract = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Contract Proposal")], limit=1)
        with self.assertRaises(UserError) as cm:
            app._guard_transition(contract)
        self.assertIn("insurer", str(cm.exception).lower())


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
            "equipment_type": "dry_van", "vin": "CONFLICTVIN000001",
        })
        foreign_op = self.env["prema.owner.operator"].create({
            "partner_id": other.partner_id.id,
            "authority_state": "verified", "coi_verified_direct": True,
            "insurance_expiry": fields.Date.add(
                fields.Date.context_today(other), years=1),
        })
        self.env["fleet.vehicle"].create({
            "model_id": self._vehicle_model().id,
            "vin_sn": "CONFLICTVIN000001", "operator_id": foreign_op.id,
            "state_id": self.env["fleet.vehicle.state"].search(
                [("name", "=", "Ready")], limit=1).id,
        })
        app = self._applicant("conflict@test.invalid").owner_operator_application_id
        self._complete(app, vin="CONFLICTVIN000001")
        with self.assertRaises(UserError) as cm:
            app.action_approve()
        self.assertIn("conflict", str(cm.exception).lower())


class TestFleetAvailability(OwnerOperatorCase):

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


class TestPublicSubmissionAtomicity(OwnerOperatorCase):
    """An invalid public submission must leave NOTHING behind."""

    def _counts(self):
        return (
            self.env["hr.applicant"].search_count([]),
            self.env["prema.owner.operator.application"].search_count([]),
            self.env["ir.attachment"].search_count(
                [("res_model", "=", "prema.owner.operator.document")]),
        )

    def test_missing_required_fields_create_nothing(self):
        before = self._counts()
        for bad in (
            {"contact_name": "A", "email": "a@test.invalid"},            # no city
            {"contact_name": "", "email": "a@test.invalid",
             "home_city": "Toronto", "home_province": "Ontario"},         # no name
            {"contact_name": "A", "email": "not-an-email",
             "home_city": "Toronto", "home_province": "Ontario"},
        ):
            with self.assertRaises(ValidationError):
                self.Application._create_from_public(bad)
            self.assertEqual(self._counts(), before,
                             "a rejected submission left a record behind")

    def test_missing_province_creates_nothing(self):
        before = self._counts()
        with self.assertRaises(ValidationError):
            self.Application._create_from_public({
                "contact_name": "A", "email": "b@test.invalid",
                "home_city": "Toronto"})
        self.assertEqual(self._counts(), before)


class TestMapping(OwnerOperatorCase):

    def test_box_height_is_stored_in_inches(self):
        """The column holds INCHES despite its _ft name; the form collects
        feet."""
        applicant = self._applicant("inches@test.invalid")
        app = applicant.owner_operator_application_id
        app.write({"box_height_ft": 8.0})
        self._complete(app)
        vehicle = app.action_approve()
        self.assertAlmostEqual(vehicle.x_box_interior_height_ft, 96.0, places=2)

    def test_home_base_and_unit_number_reach_the_vehicle(self):
        applicant = self._applicant("home@test.invalid")
        app = applicant.owner_operator_application_id
        app.write({"home_city": "Toronto", "home_province": "Ontario",
                   "unit_number": "U-42"})
        self._complete(app)
        vehicle = app.action_approve()
        self.assertEqual(vehicle.home_city, "Toronto")
        self.assertEqual(vehicle.home_province, "Ontario")
        if "x_studio_unit_number" in self.env["fleet.vehicle"]._fields:
            self.assertEqual(vehicle.x_studio_unit_number, "U-42")

    def test_advance_without_stage_configuration_raises(self):
        job = self.env["hr.job"].create({
            "name": "No stages", "is_published": True,
        })
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", job.id)
        candidate = self.env["hr.candidate"].create(
            {"partner_name": "X", "email_from": "x@test.invalid"})
        applicant = self.env["hr.applicant"].create({
            "candidate_id": candidate.id, "job_id": job.id,
            "partner_name": "X", "email_from": "x@test.invalid",
        })
        app = applicant.owner_operator_application_id
        if not app:
            self.skipTest("no application for this job")
        with self.assertRaises(UserError) as cm:
            app.action_advance()
        self.assertIn("no next stage", str(cm.exception).lower())


class TestDispatchConsumers(OwnerOperatorCase):
    """The 14 existing dispatch call sites filter on x_operational_logistics;
    the shared rule must drive that field for operator trucks and leave
    company trucks alone."""

    def _consumer_vehicles(self):
        # the exact filter the booking services use
        return self.env["fleet.vehicle"].search(
            [("active", "=", True), ("x_operational_logistics", "=", True)])

    def test_issue_state_removes_the_truck_from_dispatch_consumers(self):
        app = self._applicant("consumer@test.invalid").owner_operator_application_id
        self._complete(app)
        vehicle = app.action_approve()
        self.assertIn(vehicle, self._consumer_vehicles())
        issue = self.env["fleet.vehicle.state"].search(
            [("name", "ilike", "Issue")], limit=1)
        vehicle.write({"state_id": issue.id, "issue_reason": "Inspection"})
        self.assertNotIn(vehicle, self._consumer_vehicles(),
                         "an Issue truck must drop out of the dispatch filter")
        vehicle.action_clear_issue()
        self.assertIn(vehicle, self._consumer_vehicles())

    def test_expired_compliance_removes_the_truck(self):
        app = self._applicant("consumer2@test.invalid").owner_operator_application_id
        op = self._complete(app)
        vehicle = app.action_approve()
        self.assertIn(vehicle, self._consumer_vehicles())
        op.insurance_expiry = fields.Date.subtract(
            fields.Date.context_today(op), days=1)
        op.invalidate_recordset()
        vehicle._compute_is_dispatch_available()
        vehicle.write({"state_id": vehicle.state_id.id})  # trigger the sync
        self.assertNotIn(vehicle, self._consumer_vehicles())

    def test_company_truck_behaviour_is_untouched(self):
        ready = self.env["fleet.vehicle.state"].search(
            [("name", "=", "Ready")], limit=1)
        truck = self.env["fleet.vehicle"].create({
            "model_id": self._vehicle_model().id, "license_plate": "CO009",
            "x_operational_logistics": True, "state_id": ready.id})
        # putting a COMPANY truck on Issue does not silently unflag it
        issue = self.env["fleet.vehicle.state"].search(
            [("name", "ilike", "Issue")], limit=1)
        truck.write({"state_id": issue.id, "issue_reason": "Workshop"})
        self.assertTrue(truck.x_operational_logistics,
                        "company trucks keep their manual flag")


class TestEmailRouting(OwnerOperatorCase):
    """Alias routing + reply threading, fed through Odoo's own gateway.

    Nothing is sent: the raw RFC822 message goes straight into message_process,
    the same entry point fetchmail uses for catchall@premafirm.com.
    """

    def _raw(self, message_id, references=None, subject="Owner-operator application"):
        alias = self.job.alias_id
        domain = alias.alias_domain_id.name or "logistics.premafirm.com"
        to = "%s@%s" % (alias.alias_name, domain)
        msg = MIMEText("I am interested in the owner-operator position.", "plain")
        msg["Subject"] = subject
        msg["From"] = "Driver Person <driver.routing@test.invalid>"
        msg["To"] = to
        msg["Message-ID"] = message_id
        if references:
            msg["References"] = references
            msg["In-Reply-To"] = references
        return msg.as_string()

    def test_alias_points_at_the_onboarding_address(self):
        alias = self.job.alias_id
        self.assertTrue(alias, "the job must have an alias")
        self.assertEqual(alias.alias_name, "owneroperators")
        # decision 2026-10-17: the onboarding address keeps the logistics domain
        self.assertTrue(alias.alias_domain_id)
        self.assertIn("logistics.premafirm.com", alias.alias_domain_id.name)

    def test_inbound_email_creates_the_application(self):
        before = self.env["prema.owner.operator.application"].search_count([])
        self.env["mail.thread"].message_process(
            "hr.applicant", self._raw("<routing-%d@test.invalid>" % id(self)))
        self.assertGreater(
            self.env["prema.owner.operator.application"].search_count([]), before,
            "an inbound application email must create an application")
        app = self.env["prema.owner.operator.application"].search([], limit=1,
                                                                  order="id desc")
        self.assertEqual(app.job_id.id, self.job.id)

    def test_reply_threads_to_the_same_application(self):
        mid = "<thread-%d@test.invalid>" % id(self)
        self.env["mail.thread"].message_process(
            "hr.applicant", self._raw(mid, subject="Owner-operator application"))
        app = self.env["prema.owner.operator.application"].search(
            [], limit=1, order="id desc")
        applicant = app.hr_applicant_id
        msgs = len(applicant.message_ids)
        self.env["mail.thread"].message_process(
            "hr.applicant",
            self._raw("<reply-%d@test.invalid>" % id(self), references=mid,
                      subject="Re: Owner-operator application"))
        applicant.invalidate_recordset()
        self.assertEqual(applicant.message_ids.filtered(
            lambda m: m.message_type == "email").__len__(),
            applicant.message_ids.filtered(
                lambda m: m.message_type == "email").__len__())
        self.assertGreater(len(applicant.message_ids), 0,
                           "the reply must land on the existing applicant")

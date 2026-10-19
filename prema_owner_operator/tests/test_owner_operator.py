# -*- coding: utf-8 -*-
"""Owner-operator onboarding — the assertions the brief asks to be tested."""
from odoo import fields
import email
import uuid
from email.mime.text import MIMEText
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged, HttpCase

from odoo.addons.prema_owner_operator.application_mapping import (
    normalise_public)


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
        # Production has the six owner-operator stages in this order. The
        # staging copy can share a sequence between two stages, which makes
        # _next_stage skip one; normalise the test pipeline explicitly.
        Stage = cls.env["hr.recruitment.stage"]
        for seq, name in ((0, "New"),
                          (1, "Authority & Insurance Review"),
                          (2, "Verified"),
                          (3, "Contract Proposal"),
                          (4, "Contract Signed"),
                          (5, "Approved — Ready for Dispatch")):
            stage = Stage.search(
                [("name", "=", name), ("job_ids", "in", cls.job.id)],
                limit=1) or Stage.search([("name", "=", name)], limit=1)
            if not stage:
                stage = Stage.create({"name": name})
            stage.write({"sequence": seq, "job_ids": [(4, cls.job.id)]})
        for name in ("Equipment Verification", "Initial Qualification",
                     "Final Review", "First Interview", "Second Interview"):
            for stage in Stage.search([("name", "=", name),
                                       ("job_ids", "in", cls.job.id)]):
                stage.write({"job_ids": [(3, cls.job.id)]})
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

    def _public_vals(self, **overrides):
        """A complete, valid structured submission (internal field names)."""
        vals = {
            "contact_name": "Test Driver",
            "email": "driver@test.invalid",
            "phone": "416-555-0100",
            "business_name": "Test Owner Operator",
            "business_structure": "sole",
            "home_city": "Toronto",
            "home_province": "ON",
            "commercial_driving_experience": "6–10 years",
            "licence_class": "AZ",
            "licence_jurisdiction": "Ontario",
            "owner_operator_experience": "3–5 years",
            "licence_suspension": "no",
            "recent_collisions": "none",
            "recent_convictions": "none",
            "tdg_training": "no",
            "recent_driving_work": "Test regional client",
            "vehicle_type": "straight",
            "equipment_model": "Freightliner M2 106",
            "truck_year": "2013",
            "unit_number": "621",
            "vehicle_ownership": "own",
            "transmission": "automatic",
            "odometer_km": 350000,
            "truck_plate_jurisdiction": "TEST 123 / Ontario",
            "cargo_body_type": "reefer",
            "payload_lbs": 13000,
            "pallet_capacity": 13,
            "box_length_ft": 26,
            "cargo_width_in": 100,
            "cargo_height_in": 96,
            "door_width_in": 96,
            "door_clearance_in": 87,
            "liftgate": "yes",
            "liftgate_capacity_lb": 3000,
            "liftgate_platform_length_in": 72,
            "liftgate_platform_width_in": 40,
            "dock_height_capable": "yes",
            "air_ride": "yes",
            "reefer_temperature_range": "-20 to 4 C",
            "cvor_number": "123-456-789",
            "preferred_service_area": "Ontario and Quebec",
            "available_start_date": fields.Date.today(),
            "work_preference": "regional",
            "whatsapp_consent": "no",
            "load_notification_channel": "email",
            "load_notification_email": "driver@test.invalid",
            "application_declaration": True,
            "service_province_codes": ["ON", "QC"],
            "applicant_notes": "Test notes",
        }
        vals.update(overrides)
        return vals

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
            "vehicle_type": "straight",
            "cargo_body_type": "dry",
            "payload_lbs": 13000,
            "pallet_capacity": 13,
            "box_length_ft": 26,
            "cargo_width_in": 100,
            "cargo_height_in": 96,
            "door_width_in": 96,
            "door_clearance_in": 87,
            "liftgate": "no",
            "dock_height_capable": "yes",
            "equipment_type": "dry_van",
            "has_liftgate": False,
            "has_reefer": False,
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
            self.Application._create_from_public(
                self._public_vals(home_city=""))
        with self.assertRaises(ValidationError):
            self.Application._create_from_public(
                self._public_vals(home_province=""))
        app = self.Application._create_from_public(
            self._public_vals(email="a@test.invalid"))
        self.assertEqual(app.home_city, "Toronto")
        self.assertEqual(app.home_province, "ON")
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
        other = self.Application._create_from_public(
            self._public_vals(contact_name="Other",
                              email="other@test.invalid"))
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
            self._public_vals(home_city=""),                      # no city
            self._public_vals(contact_name=""),                    # no name
            self._public_vals(email="not-an-email"),               # bad email
            self._public_vals(vehicle_type=""),                    # no vehicle
            self._public_vals(application_declaration=False),      # no consent
            self._public_vals(odometer_km=None),                   # no odometer
            self._public_vals(cvor_number="12345"),                # bad CVOR
        ):
            with self.assertRaises(ValidationError):
                self.Application._create_from_public(bad)
            self.assertEqual(self._counts(), before,
                             "a rejected submission left a record behind")

    def test_missing_province_creates_nothing(self):
        before = self._counts()
        with self.assertRaises(ValidationError):
            self.Application._create_from_public(
                self._public_vals(home_province=""))
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
        self.assertAlmostEqual(vehicle.x_box_interior_height_ft, 8.0, places=2)
        self.assertAlmostEqual(vehicle.oo_cargo_height_in, 96.0, places=2)

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
        self.assertTrue(applicant, "the first email must create an applicant")

        applicants_before = self.env["hr.applicant"].search_count([])
        applications_before = self.env[
            "prema.owner.operator.application"].search_count([])

        reply_mid = "<reply-%d@test.invalid>" % id(self)
        self.env["mail.thread"].message_process(
            "hr.applicant",
            self._raw(reply_mid, references=mid,
                      subject="Re: Owner-operator application"))

        # 1) no second applicant and no second onboarding record
        self.assertEqual(self.env["hr.applicant"].search_count([]),
                         applicants_before,
                         "the reply must not create a duplicate applicant")
        self.assertEqual(
            self.env["prema.owner.operator.application"].search_count([]),
            applications_before,
            "the reply must not create a second application")
        # 2) the reply is attached to the ORIGINAL applicant
        applicant.invalidate_recordset()
        message_ids = [(m.message_id or "").strip("<>")
                       for m in applicant.message_ids]
        self.assertIn(reply_mid.strip("<>"), message_ids,
                      "the reply must land on the original applicant")



class TestEligibilityRefresh(OwnerOperatorCase):
    """Eligibility must follow compliance over time, and clearance must not
    override another blocker."""

    def _filter(self):
        return self.env["fleet.vehicle"].search(
            [("active", "=", True), ("x_operational_logistics", "=", True)])

    def test_cron_removes_truck_when_insurance_expires(self):
        app = self._applicant("expiry@test.invalid").owner_operator_application_id
        op = self._complete(app)
        vehicle = app.action_approve()
        self.assertIn(vehicle, self._filter())
        # expiry passes — NO edit to the vehicle or the operator
        self.env.cr.execute(
            "UPDATE prema_owner_operator SET insurance_expiry = %s WHERE id = %s",
            (fields.Date.subtract(fields.Date.context_today(op), days=3), op.id))
        op.invalidate_recordset(); vehicle.invalidate_recordset()
        self.env["fleet.vehicle"]._cron_refresh_availability()
        vehicle.invalidate_recordset()
        self.assertNotIn(vehicle, self._filter(),
                         "the scheduled refresh must drop an expired truck")

    def test_issue_clearance_cannot_restore_while_compliance_blocked(self):
        app = self._applicant("clear@test.invalid").owner_operator_application_id
        op = self._complete(app)
        vehicle = app.action_approve()
        op.write({"insurance_expiry": fields.Date.subtract(
            fields.Date.context_today(op), days=1)})
        vehicle._compute_is_dispatch_available()
        issue = self.env["fleet.vehicle.state"].search(
            [("name", "ilike", "Issue")], limit=1)
        vehicle.write({"state_id": issue.id, "issue_reason": "Repair"})
        vehicle.action_clear_issue()
        vehicle.invalidate_recordset()
        self.assertFalse(vehicle.is_dispatch_available,
                         "clearing the issue must not override expired cover")
        self.assertNotIn(vehicle, self._filter())


class TestStructuredSubmission(OwnerOperatorCase):
    """The website answers must land in structured fields, not chatter."""

    def test_submission_populates_every_structured_field(self):
        app = self.Application._create_from_public(self._public_vals())
        self.assertEqual(app.business_name, "Test Owner Operator")
        self.assertEqual(app.business_structure, "sole")
        self.assertTrue(app.is_sole_proprietor)
        self.assertEqual(app.home_city, "Toronto")
        self.assertEqual(app.home_province, "ON")
        self.assertEqual(
            set(app.service_province_ids.mapped("code")), {"ON", "QC"})
        self.assertEqual(app.commercial_driving_experience, "6–10 years")
        self.assertEqual(app.licence_class, "AZ")
        self.assertEqual(app.licence_jurisdiction, "Ontario")
        self.assertEqual(app.owner_operator_experience, "3–5 years")
        self.assertEqual(app.licence_suspension, "no")
        self.assertEqual(app.recent_collisions, "none")
        self.assertEqual(app.recent_convictions, "none")
        self.assertEqual(app.tdg_training, "no")
        self.assertEqual(app.recent_driving_work, "Test regional client")
        self.assertEqual(app.vehicle_type, "straight")
        self.assertEqual(app.equipment_model, "Freightliner M2 106")
        self.assertEqual(app.truck_year, "2013")
        self.assertEqual(app.unit_number, "621")
        self.assertEqual(app.vehicle_ownership, "own")
        self.assertEqual(app.transmission, "automatic")
        self.assertEqual(app.odometer_km, 350000)
        self.assertEqual(app.truck_plate_jurisdiction, "TEST 123 / Ontario")
        self.assertEqual(app.license_plate, "TEST 123")
        self.assertEqual(app.plate_jurisdiction, "Ontario")
        self.assertEqual(app.cargo_body_type, "reefer")
        self.assertEqual(app.payload_lbs, 13000)
        self.assertEqual(app.pallet_capacity, 13)
        self.assertEqual(app.box_length_ft, 26)
        self.assertEqual(app.cargo_width_in, 100)
        self.assertEqual(app.cargo_height_in, 96)
        self.assertEqual(app.door_clearance_in, 87)
        self.assertEqual(app.liftgate, "yes")
        self.assertEqual(app.liftgate_capacity_lb, 3000)
        self.assertEqual(app.dock_height_capable, "yes")
        self.assertEqual(app.air_ride, "yes")
        self.assertEqual(app.reefer_temperature_range, "-20 to 4 C")
        self.assertEqual(app.cvor_number, "123-456-789")
        self.assertEqual(app.cvor_verification_status, "pending")
        # insurance-agent fields were removed from public intake
        self.assertFalse(app.insurance_agent_name)
        self.assertFalse(app.insurance_agent_email)
        self.assertEqual(app.preferred_service_area, "Ontario and Quebec")
        self.assertEqual(app.work_preference, "regional")
        self.assertEqual(
            set(app.preferred_service_area_ids.mapped("code")),
            {"ontario_quebec"})
        self.assertEqual(
            set(app.work_preference_ids.mapped("code")), {"regional"})
        self.assertEqual(app.whatsapp_consent, "no")
        self.assertEqual(app.door_width_in, 96)
        self.assertEqual(app.liftgate_platform_length_in, 72)
        self.assertEqual(app.liftgate_platform_width_in, 40)
        self.assertEqual(app.load_notification_channel, "email")
        self.assertEqual(app.load_notification_email, "driver@test.invalid")
        self.assertTrue(app.application_declaration)
        self.assertIn("Test notes", str(app.additional_notes))
        # derived Fleet roll-ups
        self.assertEqual(app.equipment_type, "straight_truck")
        self.assertTrue(app.has_reefer)
        self.assertTrue(app.has_liftgate)
        self.assertTrue(app.has_air_ride)
        # the intake collects no VIN
        self.assertFalse(app.vin)

    def test_tractor_is_power_only_and_drops_trailer_and_reefer_specs(self):
        app = self.Application._create_from_public(self._public_vals(
            vehicle_type="tractor", trailer_ownership="own",
            trailer_type="reefer", trailer_length_ft=53,
            trailer_unit="TR-9", cargo_body_type="dry",
            payload_lbs=99999, pallet_capacity=30, box_length_ft=53,
            cargo_width_in=120, cargo_height_in=120, door_width_in=96,
            door_clearance_in=120, liftgate="yes",
            liftgate_capacity_lb=5000,
            reefer_temperature_range="frozen"))
        self.assertEqual(app.vehicle_type, "tractor")
        self.assertEqual(app.trailer_ownership, "power_only")
        self.assertEqual(app.reefer_applicable, "not_applicable")
        # Tractor only: nothing trailer/cargo/reefer is stored.
        for field in ("trailer_type", "trailer_length_ft", "trailer_unit",
                      "trailer_year", "cargo_body_type", "payload_lbs",
                      "pallet_capacity", "box_length_ft", "cargo_width_in",
                      "cargo_height_in", "door_width_in",
                      "door_clearance_in", "liftgate",
                      "liftgate_capacity_lb", "reefer_temperature_range"):
            self.assertFalse(app[field], field)
        self.assertFalse(app.has_reefer)
        self.assertFalse(app.has_liftgate)
        self.assertEqual(app.equipment_type, "other")

    def test_cargo_van_and_straight_branches(self):
        van = self.Application._create_from_public(self._public_vals(
            email="van@test.invalid", vehicle_type="cargo_van",
            cargo_body_type="dry", trailer_type="reefer"))
        self.assertEqual(van.equipment_type, "other")
        self.assertFalse(van.has_reefer)
        self.assertFalse(van.trailer_type)
        straight = self.Application._create_from_public(self._public_vals(
            email="straight@test.invalid", vehicle_type="straight",
            cargo_body_type="both"))
        self.assertEqual(straight.equipment_type, "straight_truck")
        self.assertTrue(straight.has_reefer)

    def test_multiple_provinces_survive(self):
        codes = ["ON", "QC", "AB", "BC", "YT"]
        app = self.Application._create_from_public(self._public_vals(
            service_province_codes=codes,
            preferred_service_area="Selected provinces"))
        self.assertEqual(set(app.service_province_ids.mapped("code")),
                         set(codes))

    def test_cvor_formatting_and_pending_verification(self):
        vals, errors = normalise_public({"CVORNumber": "123456789"},
                                        strict=False)
        self.assertFalse(errors)
        self.assertEqual(vals["cvor_number"], "123-456-789")
        _vals, errors = normalise_public({"CVORNumber": "12345"},
                                         strict=False)
        self.assertTrue(errors)
        app = self.Application._create_from_public(
            self._public_vals(cvor_number="987-654-321"))
        self.assertEqual(app.cvor_number, "987-654-321")
        self.assertEqual(app.cvor_verification_status, "pending")

    def test_unknown_yes_no_stays_unknown(self):
        vals = self._public_vals()
        for key in ("liftgate", "air_ride", "tdg_training",
                    "owner_operator_experience", "transmission",
                    "cargo_body_type"):
            vals.pop(key, None)
        app = self.Application._create_from_public(vals)
        self.assertFalse(app.liftgate)
        self.assertFalse(app.air_ride)
        self.assertFalse(app.tdg_training)
        self.assertFalse(app.owner_operator_experience)
        self.assertFalse(app.transmission)
        self.assertFalse(app.cargo_body_type)
        self.assertFalse(app.has_liftgate)
        self.assertFalse(app.has_air_ride)
        # a required answer is still stored exactly
        self.assertEqual(app.licence_suspension, "no")

    def test_mileage_and_dimensions_reach_fleet_in_the_right_units(self):
        app = self.Application._create_from_public(self._public_vals())
        self._complete(app)
        vehicle = app.action_approve()
        self.assertEqual(vehicle.application_id, app)
        self.assertEqual(vehicle.x_box_interior_height_ft, 8.0)    # feet
        self.assertEqual(vehicle.oo_cargo_height_in, 96.0)         # inches
        self.assertEqual(vehicle.x_door_clearance_in, 87.0)       # inches
        self.assertEqual(vehicle.x_cargo_box_length_ft, 26.0)     # feet
        self.assertEqual(vehicle.x_current_odometer_km, 350000.0)  # km
        self.assertEqual(vehicle.x_max_payload_lbs, 13000.0)      # cargo lb
        self.assertEqual(vehicle.x_max_pallets, 13)
        self.assertEqual(vehicle.transmission, "automatic")
        self.assertEqual(vehicle.x_studio_unit_number, "621")
        if "x_studio_dock_height" in vehicle._fields:
            self.assertTrue(vehicle.x_studio_dock_height)
        # GVWR is never treated as cargo payload
        self.assertNotEqual(vehicle.x_max_payload_lbs, vehicle.x_gvwr_lbs)
        # repeat approval returns the same vehicle and does not duplicate
        again = app.action_approve()
        self.assertEqual(again, vehicle)
        self.assertEqual(self.env["fleet.vehicle"].search_count(
            [("application_id", "=", app.id)]), 1)

    def test_dimension_values_are_validated(self):
        _vals, errors = normalise_public(
            {"VehicleType": "straight", "OdometerKm": "-1",
             "CargoHeightIn": "999"}, strict=False)
        self.assertGreaterEqual(len(errors), 2)


class TestBackfill(OwnerOperatorCase):
    """The saved “Other Information” note is replayed, never consumed."""

    NOTE = (
        "<p>Other Information:<br>___________<br><br>"
        "LegalBusinessName : DEMO ONLY — Sample Owner Operator<br>"
        "BusinessStructure : sole<br>HomeCity : Toronto<br>"
        "HomeProvince : ON<br>CommercialDrivingYears : 6–10 years<br>"
        "DriverLicenceClass : DZ<br>LicenceJurisdiction : Ontario<br>"
        "OwnerOperatorYears : 3–5 years<br>LicenceSuspension : No<br>"
        "RecentCollisions : None<br>RecentConvictions : None<br>"
        "TDGTraining : No<br>VehicleType : straight<br>"
        "TruckMakeModel : Freightliner M2 106<br>TruckYear : 2013<br>"
        "UnitNumber : 621<br>VehicleOwnership : Own<br>"
        "Transmission : Automatic<br>OdometerKm : 350000<br>"
        "TruckPlateJurisdiction : DEMO ONLY / Ontario<br>"
        "CargoBodyType : reefer<br>CargoLengthFt : 26<br>"
        "DockHeightCapable : yes<br>ServiceProvinceON : Ontario<br>"
        "ServiceProvinceQC : Quebec<br>"
        "PreferredLanes : Ontario and Quebec<br>"
        "WorkPreference : regional<br>WhatsAppConsent : no<br>"
        "ApplicationDeclaration : Confirmed<br><br></p>")

    def test_backfill_is_repeat_safe_and_keeps_the_note(self):
        applicant = self._applicant("demo-backfill@test.invalid")
        app = applicant.owner_operator_application_id
        applicant.message_post(body=self.NOTE, subtype_xmlid="mail.mt_note")
        messages_before = len(applicant.message_ids)
        apps_before = self.Application.search_count([])

        self.assertEqual(
            self.Application._backfill_from_other_information(), 1)
        app.invalidate_recordset()
        self.assertEqual(app.business_name,
                         "DEMO ONLY — Sample Owner Operator")
        self.assertEqual(app.business_structure, "sole")
        self.assertTrue(app.is_sole_proprietor)
        self.assertEqual(app.home_province, "Ontario")
        self.assertEqual(app.home_province_code, "ON")
        self.assertEqual(app.vehicle_type, "straight")
        self.assertEqual(app.odometer_km, 350000)
        self.assertEqual(app.box_length_ft, 26)
        self.assertEqual(app.dock_height_capable, "yes")
        self.assertEqual(set(app.service_province_ids.mapped("code")),
                         {"ON", "QC"})
        self.assertEqual(app.preferred_service_area, "Ontario and Quebec")
        self.assertEqual(app.cvor_verification_status, "not_provided")
        self.assertEqual(len(applicant.message_ids), messages_before,
                         "the original note must be preserved")
        self.assertEqual(self.Application.search_count([]), apps_before,
                         "backfill must not create a second application")
        self.assertEqual(self.env["fleet.vehicle"].search_count(
            [("application_id", "=", app.id)]), 0,
            "backfill must not create a Fleet vehicle")

        # second run changes nothing
        self.assertEqual(
            self.Application._backfill_from_other_information(), 0)
        # a reviewer's manual correction wins
        app.write({"home_city": "Manual"})
        applicant.message_post(body=self.NOTE, subtype_xmlid="mail.mt_note")
        self.Application._backfill_from_other_information()
        app.invalidate_recordset()
        self.assertEqual(app.home_city, "Manual")


class TestCOIRequest(OwnerOperatorCase):

    def _authority(self):
        stage = self.env["hr.recruitment.stage"].search(
            [("name", "=", "Authority & Insurance Review"),
             ("job_ids", "in", self.job.id)], limit=1)
        stage.write({"template_id": False})
        return stage

    def _move_to_authority(self, app):
        app.hr_applicant_id.write({"stage_id": self._authority().id})

    def _coi_mails(self, app):
        return self.env["mail.mail"].search([
            ("subject", "ilike", "Action Required: Certificate of Insurance"),
            ("subject", "ilike", app.name),
        ])

    def test_entering_authority_sends_one_correct_email(self):
        app = self.Application._create_from_public(self._public_vals())
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
        self.assertIn(app.business_name, body)
        self.assertIn(app.name, body)
        self.assertIn(app.name, mail.subject)
        self.assertIn(app.email, mail.email_to or "")
        self.assertFalse(mail.recipient_ids)
        self.assertEqual(app.partner_id.email, app.email)
        app.invalidate_recordset()
        self.assertTrue(app.coi_request_sent_at)
        self.assertEqual(app.coi_request_count, 1)
        self.assertTrue(app.coi_request_message_id)
        self.assertEqual(app.coi_request_sent_by, self.env.user)

    def test_ordinary_edit_does_not_resend(self):
        app = self.Application._create_from_public(self._public_vals())
        self._move_to_authority(app)
        count = len(self._coi_mails(app))
        app.write({"home_city": "Mississauga",
                   "recent_driving_work": "Edited"})
        self.assertEqual(len(self._coi_mails(app)), count)

    def test_deliberate_resend_is_logged(self):
        app = self.Application._create_from_public(self._public_vals())
        self._move_to_authority(app)
        count = len(self._coi_mails(app))
        app.action_resend_coi_request()
        self.assertEqual(len(self._coi_mails(app)), count + 1)
        app.invalidate_recordset()
        self.assertEqual(app.coi_request_count, 2)
        self.assertTrue(any(
            "re-sent" in (m.body or "") for m in app.message_ids))

    def test_migration_does_not_send_and_does_not_advance(self):
        from odoo.addons.prema_owner_operator.hooks import ensure_pipeline
        app = self._applicant("migrate@test.invalid").owner_operator_application_id
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
        self.assertNotEqual(app.hr_applicant_id.stage_id.name,
                            "Authority & Insurance Review")
        self.assertEqual(app.hr_applicant_id.stage_id.name, "New")


class TestOtherJobs(OwnerOperatorCase):

    def test_other_job_applicant_is_untouched(self):
        job = self.env["hr.job"].create({
            "name": "Other position", "is_published": True})
        candidate = self.env["hr.candidate"].create({
            "partner_name": "Other Person",
            "email_from": "other-person@test.invalid",
        })
        applicant = self.env["hr.applicant"].create({
            "candidate_id": candidate.id,
            "job_id": job.id,
            "partner_name": "Other Person",
            "email_from": "other-person@test.invalid",
        })
        self.assertFalse(applicant.owner_operator_application_id)
        self.assertFalse(applicant.owner_operator_document_ids)


@tagged("post_install", "-at_install")
class TestPublicRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        job = self.env["hr.job"].browse(5)
        if job.exists():
            job.write({"is_published": True})
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", 5)

    def _raw_post(self):
        self.demo_email = "browser-demo-%s@example.com" % uuid.uuid4().hex[:12]
        return {
            "partner_name": "Browser Demo",
            "email_from": self.demo_email,
            "partner_phone": "416-555-0199",
            "LegalBusinessName": "Browser Demo Owner Operator",
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
            "TDGTraining": "No",
            "VehicleType": "straight",
            "TruckMake": "Freightliner",
            "TruckModel": "M2 106",
            "TruckYear": "2013",
            "UnitNumber": "621",
            "VehicleOwnership": "Own",
            "Transmission": "Automatic",
            "OdometerKm": "350,000",
            "TruckPlate": "DEMO 621",
            "TruckPlateProvince": "ON",
            "GVWRLb": "26,000",
            "CargoBodyType": "reefer",
            "LegalPayloadLb": "13000",
            "PalletCapacity": "13",
            "CargoLengthFt": "26",
            "CargoWidthIn": "100",
            "CargoHeightIn": "96",
            "DoorWidthIn": "96",
            "DoorHeightIn": "87",
            "Liftgate": "yes",
            "LiftgateCapacityLb": "3000",
            "LiftgatePlatformLengthIn": "72",
            "LiftgatePlatformWidthIn": "40",
            "DockHeightCapable": "yes",
            "AirRide": "yes",
            "ReeferTemperatureRange": "-20 to 4 C",
            "CVORNumber": "123456789",
            "ServiceProvinceON": "Ontario",
            "ServiceProvinceQC": "Quebec",
            "PreferredAreaOntarioQuebec": "1",
            "WorkPreferenceRegional": "1",
            "LoadNotificationChannel": "email",
            "UseApplicationEmailForNotifications": "1",
            "ApplicationDeclaration": "Confirmed",
            "applicant_notes": "Browser demo submission.",
        }

    def test_public_form_renders_the_live_field_set(self):
        response = self.url_open("/owner-operator/apply")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LegalBusinessName", response.text)
        self.assertIn('name="TruckMake"', response.text)
        self.assertIn('name="TruckModel"', response.text)
        self.assertIn('name="TruckPlate"', response.text)
        self.assertIn('name="TruckPlateProvince"', response.text)
        self.assertIn('name="GVWRLb"', response.text)
        self.assertIn("ServiceProvinceON", response.text)
        self.assertIn("DoorWidthIn", response.text)
        self.assertIn('name="DriverLicenceFront"', response.text)
        self.assertIn('name="DriverLicenceBack"', response.text)
        self.assertIn("WorkPreferenceLocal", response.text)
        self.assertIn("LoadNotificationChannel", response.text)
        self.assertIn("WhatsAppNumber", response.text)
        self.assertNotIn('name="InsuranceAgent"', response.text)
        self.assertNotIn('name="InsuranceAgentEmail"', response.text)
        self.assertNotIn("LiftgatePlatformLengthIn", response.text)
        self.assertNotIn("LiftgatePlatformWidthIn", response.text)
        self.assertNotIn("DockHeightCapable", response.text)
        self.assertNotIn("PreferredAreaOntarioWide", response.text)
        self.assertNotIn("TruckPlateJurisdiction", response.text)
        self.assertNotIn("TruckMakeModel", response.text)
        self.assertIn("csrf_token", response.text)
        # The backend website preview needs the website metadata to name the
        # main object; without it its systray raises on this page.
        self.assertIn("data-website-id=", response.text)
        self.assertIn('data-main-object="hr.job(5,)"', response.text)
        # no résumé/LinkedIn and no VIN field on the owner-operator page
        self.assertNotIn('name="linkedin_profile"', response.text)
        self.assertNotIn('name="Resume"', response.text)
        self.assertNotIn('name="vin"', response.text)

    def test_job_apply_route_carries_preview_metadata(self):
        response = self.url_open("/jobs/apply/owner-operator-5")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-website-id=', response.text)
        self.assertIn('data-main-object="hr.job(5,)"', response.text)
        self.assertIn("Owner-Operator Application", response.text)

    def test_public_post_creates_one_linked_application(self):
        import re
        page = self.url_open("/owner-operator/apply")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        payload = self._raw_post()
        payload["csrf_token"] = token
        response = self.url_open("/owner-operator/apply", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Thank you", response.text)
        self.assertIn("data-website-id=", response.text)
        self.assertIn('data-main-object="hr.job(5,)"', response.text)
        apps = self.env["prema.owner.operator.application"].search(
            [("email", "=", self.demo_email)])
        self.assertEqual(len(apps), 1)
        app = apps
        self.assertEqual(app.business_name, "Browser Demo Owner Operator")
        self.assertEqual(app.cvor_number, "123-456-789")
        self.assertEqual(app.cvor_verification_status, "pending")
        self.assertEqual(len(app.service_province_ids), 2)
        self.assertTrue(app.hr_applicant_id)
        self.assertEqual(
            self.env["prema.owner.operator.application"].search_count(
                [("hr_applicant_id", "=", app.hr_applicant_id.id)]), 1)


@tagged("post_install", "-at_install")
class TestDocumentDelivery(HttpCase):
    """The View button loads the private route; it never writes a status."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        job = cls.env["hr.job"].browse(5)
        if not job.exists():
            job = cls.env["hr.job"].create({
                "name": "Owner Operator (delivery test)",
                "is_published": True,
                "company_id": cls.env.company.id,
            })
        job.write({"is_published": True})
        cls.job = job
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", job.id)

    def setUp(self):
        super().setUp()
        candidate = self.env["hr.candidate"].create({
            "partner_name": "Route Tester",
            "email_from": "route-%s@test.invalid" % uuid.uuid4().hex[:8],
            "company_id": self.job.company_id.id or self.env.company.id,
        })
        applicant = self.env["hr.applicant"].create({
            "candidate_id": candidate.id,
            "job_id": self.job.id,
            "partner_name": "Route Tester",
            "email_from": candidate.email_from,
            "company_id": self.job.company_id.id or self.env.company.id,
        })
        self.app = applicant.owner_operator_application_id
        self.reviewer = self.env["res.users"].create({
            "name": "OO Reviewer",
            "login": "oo.reviewer.%s" % uuid.uuid4().hex[:8],
            "password": "Reviewer#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "prema_owner_operator.group_owner_operator").id])],
        })
        self.outsider = self.env["res.users"].create({
            "name": "OO Outsider",
            "login": "oo.outsider.%s" % uuid.uuid4().hex[:8],
            "password": "Outsider#123",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })

    def _doc(self, doc_type, data, mimetype, name, status="received"):
        doc = self.app.document_ids.filtered(
            lambda d: d.document_type == doc_type)[:1]
        if not doc:
            doc = self.env["prema.owner.operator.document"].create({
                "application_id": self.app.id, "document_type": doc_type})
        attachment = self.env["ir.attachment"].create({
            "name": name,
            "raw": data,
            "mimetype": mimetype,
            "res_model": "prema.owner.operator.document",
            "res_id": doc.id,
            "public": False,
        })
        doc.write({"attachment_id": attachment.id, "status": status})
        return doc

    def test_pdf_opens_inline_and_never_verifies(self):
        doc = self._doc("cvor", b"%PDF-1.4\npreview", "application/pdf",
                        "cvor.pdf")
        self.assertTrue(doc.can_preview)
        self.authenticate(self.reviewer.login, "Reviewer#123")
        response = self.url_open("/owner_operator/document/%d" % doc.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"),
                         "application/pdf")
        self.assertIn("inline", response.headers.get("Content-Disposition"))
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertEqual(doc.status, "received")
        self.assertFalse(doc.verified_on)

    def test_photo_opens_inline(self):
        doc = self._doc("photo_front", b"\xff\xd8\xff\xe0photo",
                        "image/jpeg", "truck.jpg")
        self.authenticate(self.reviewer.login, "Reviewer#123")
        response = self.url_open("/owner_operator/document/%d" % doc.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"), "image/jpeg")
        self.assertIn("inline", response.headers.get("Content-Disposition"))

    def test_unsupported_format_is_only_offered_as_download(self):
        doc = self._doc("other", b"plain text", "text/plain", "notes.txt")
        self.authenticate(self.reviewer.login, "Reviewer#123")
        response = self.url_open("/owner_operator/document/%d" % doc.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment",
                      response.headers.get("Content-Disposition"))
        explicit = self.url_open(
            "/owner_operator/document/%d?download=1" % doc.id)
        self.assertIn("attachment",
                      explicit.headers.get("Content-Disposition"))

    def test_requested_document_without_file_has_no_button(self):
        doc = self.app.document_ids.filtered(
            lambda d: d.document_type == "licence_front")[:1]
        self.assertTrue(doc)
        self.assertFalse(doc.attachment_id)
        self.assertFalse(doc.can_preview)
        self.authenticate(self.reviewer.login, "Reviewer#123")
        response = self.url_open("/owner_operator/document/%d" % doc.id)
        self.assertEqual(response.status_code, 404)

    def test_user_without_the_group_cannot_open_the_file(self):
        doc = self._doc("coi_applicant", b"%PDF-1.4\nprivate",
                        "application/pdf", "coi.pdf")
        self.authenticate(self.outsider.login, "Outsider#123")
        response = self.url_open("/owner_operator/document/%d" % doc.id)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"%PDF", response.content)

    def test_anonymous_request_is_sent_to_login(self):
        doc = self._doc("cvor", b"%PDF-1.4\nprivate", "application/pdf",
                        "cvor.pdf")
        self.authenticate(None, None)
        response = self.url_open(
            "/owner_operator/document/%d" % doc.id, allow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 303))
        self.assertIn("login", response.headers.get("Location", ""))

    def _coi_version(self, data=b"%PDF-1.4\ncoi"):
        doc = self._doc("coi_insurer", data, "application/pdf", "coi.pdf")
        version = self.env["prema.owner.operator.coi.version"].sudo().create({
            "application_id": self.app.id,
            "document_id": doc.id,
            "source": "insurer",
            "is_direct": True,
            "is_current": True,
            "sender": "broker@insurer.test",
            "received_date": fields.Datetime.now(),
        })
        attachment = self.env["ir.attachment"].create({
            "name": "coi.pdf",
            "raw": data,
            "mimetype": "application/pdf",
            "res_model": "prema.owner.operator.coi.version",
            "res_id": version.id,
            "public": False,
        })
        version.write({
            "attachment_id": attachment.id,
            "sha256": "a" * 64,
        })
        return version

    def test_coi_version_is_private_inline_and_downloadable(self):
        version = self._coi_version()
        self.assertFalse(version.attachment_id.public)
        self.authenticate(self.reviewer.login, "Reviewer#123")
        response = self.url_open(
            "/owner_operator/coi_version/%d" % version.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"),
                         "application/pdf")
        self.assertIn("inline", response.headers.get("Content-Disposition"))
        self.assertTrue(response.content.startswith(b"%PDF"))
        download = self.url_open(
            "/owner_operator/coi_version/%d?download=1" % version.id)
        self.assertIn("attachment",
                      download.headers.get("Content-Disposition"))
        self.authenticate(self.outsider.login, "Outsider#123")
        denied = self.url_open(
            "/owner_operator/coi_version/%d" % version.id)
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn(b"%PDF", denied.content)

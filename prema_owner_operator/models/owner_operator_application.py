# -*- coding: utf-8 -*-
"""prema.owner.operator.document + .application.

The application is the ONE structured record for a candidate, and every
document, signature and approval hangs off it explicitly. Nothing is matched
by email address and nothing is matched by number plate: an existing partner
or vehicle is only ever linked through an id stored on this record, and a VIN
that already belongs to someone else stops the approval for manual review.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..application_mapping import (
    REQUIRED_INTERNAL, normalise_public,
    parse_other_information, split_plate_jurisdiction)

# Ordered so the checklist reads the way the process runs.
DOCUMENT_TYPES = [
    ("cvor", "CVOR / provincial certificate"),
    ("coi_applicant", "Certificate of insurance (applicant copy)"),
    ("coi_insurer", "Certificate of insurance (from insurer)"),
    ("business_registration", "Corporate / business registration"),
    ("vehicle_registration", "Vehicle registration / inspection"),
    ("photo_front", "Truck photo — front"),
    ("photo_rear", "Truck photo — rear"),
    ("photo_left", "Truck photo — left side"),
    ("photo_right", "Truck photo — right side"),
    ("photo_cargo_interior", "Truck photo — cargo interior"),
    ("licence_front", "Driver licence — front"),
    ("licence_back", "Driver licence — back"),
    ("signed_agreement", "Signed agreement / NDA"),
    ("other", "Other document"),
]

# Which stage a document becomes mandatory at. "initial" is required for the
# application to be complete at all; the others gate a stage transition.
DOCUMENT_STAGE = {
    "cvor": "initial",
    "coi_applicant": "initial",
    "business_registration": "initial",
    "vehicle_registration": "initial",
    "photo_front": "initial",
    "photo_rear": "initial",
    "photo_left": "initial",
    "photo_right": "initial",
    "photo_cargo_interior": "initial",
    "licence_front": "qualification",
    "licence_back": "qualification",
    "coi_insurer": "qualification",
    "signed_agreement": "contract",
}

SOURCE = [
    ("applicant", "Applicant upload"),
    ("insurer", "Received from insurer"),
    ("private", "Collected privately (dispatcher)"),
]

STATUS = [
    ("pending", "Requested"),
    ("received", "Received"),
    ("verified", "Verified"),
    ("rejected", "Rejected"),
]

APPLICATION_STATE = [
    ("submitted", "Submitted"),
    ("qualifying", "Qualifying"),
    ("authority_review", "Authority & insurance review"),
    ("contract", "Contract proposal"),
    ("signed", "Contract signed"),
    ("approved", "Approved — handed to Fleet"),
    ("rejected", "Rejected"),
]

# The ONLY fields the public form may write. Workflow, approval, signature,
# verification, Fleet and operator links are deliberately absent.
SUBMISSION_FIELDS = {
    "contact_name", "email", "phone", "business_name", "business_structure",
    "home_city", "home_province", "commercial_driving_experience",
    "licence_class", "licence_jurisdiction", "owner_operator_experience",
    "licence_suspension", "recent_collisions", "recent_convictions",
    "tdg_training", "recent_driving_work", "vehicle_type", "equipment_model",
    "truck_year", "unit_number", "vehicle_ownership", "transmission",
    "odometer_km", "truck_plate_jurisdiction", "license_plate",
    "plate_jurisdiction", "cargo_body_type", "trailer_ownership",
    "trailer_type", "trailer_length_ft", "trailer_unit", "payload_lbs",
    "pallet_capacity", "box_length_ft", "cargo_width_in", "cargo_height_in",
    "door_clearance_in", "liftgate", "liftgate_capacity_lb",
    "dock_height_capable", "air_ride", "reefer_temperature_range",
    "cvor_number", "insurance_agent_name", "insurance_agent_email",
    "preferred_service_area", "available_start_date", "work_preference",
    "whatsapp_consent", "application_declaration",
}


def _normalise_vin(value):
    """VINs differ only in separators and case; the DATA does not."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper()) or False


class OwnerOperatorDocument(models.Model):
    _name = "prema.owner.operator.document"
    _description = "Owner-Operator Application Document"
    _order = "application_id, document_type, id"

    application_id = fields.Many2one(
        "prema.owner.operator.application", required=True, index=True,
        ondelete="cascade")
    operator_id = fields.Many2one(related="application_id.operator_id",
                                  store=True, index=True)
    company_id = fields.Many2one(related="application_id.company_id",
                                 store=True, index=True)
    document_type = fields.Selection(DOCUMENT_TYPES, required=True)
    name = fields.Char(compute="_compute_name", store=True)
    required_stage = fields.Selection(
        [("initial", "Initial upload"),
         ("qualification", "Initial Qualification"),
         ("contract", "Contract")],
        compute="_compute_required_stage", store=True)

    attachment_id = fields.Many2one(
        "ir.attachment", string="File", ondelete="restrict",
        help="Stored privately: the attachment is bound to this record and "
             "served only by the module's scoped route — never through a "
             "public /web/content URL.")
    file_name = fields.Char(related="attachment_id.name")
    mimetype = fields.Char(related="attachment_id.mimetype")

    source = fields.Selection(SOURCE, default="applicant", required=True)
    status = fields.Selection(STATUS, default="pending", required=True)
    requested_on = fields.Datetime(default=fields.Datetime.now)
    received_on = fields.Datetime()
    verified_by = fields.Many2one("res.users")
    verified_on = fields.Datetime()
    notes = fields.Text()

    # insurer-direct COI metadata — captured from the certificate itself
    insurer_name = fields.Char(string="Insurer on certificate")
    policy_number = fields.Char(string="Policy number")
    expiry_date = fields.Date(string="Coverage expires")

    _sql_constraints = [
        ("document_unique",
         "UNIQUE(application_id, document_type)",
         "This document is already tracked on the application."),
    ]

    @api.depends("document_type")
    def _compute_name(self):
        labels = dict(DOCUMENT_TYPES)
        for doc in self:
            doc.name = labels.get(doc.document_type, doc.document_type or "")

    @api.depends("document_type")
    def _compute_required_stage(self):
        for doc in self:
            doc.required_stage = DOCUMENT_STAGE.get(doc.document_type, "initial")

    @api.constrains("attachment_id", "status")
    def _check_file_present(self):
        for doc in self:
            if doc.status in ("received", "verified") and not doc.attachment_id:
                raise ValidationError(_(
                    "“%s” is marked received but has no file.") % doc.name)

    def action_verify(self):
        self.write({"status": "verified",
                    "verified_by": self.env.user.id,
                    "verified_on": fields.Datetime.now()})
        return True

    def action_reject(self):
        self.write({"status": "rejected"})
        return True


class OwnerOperatorApplication(models.Model):
    _name = "prema.owner.operator.application"
    _description = "Owner-Operator Application"
    _order = "create_date desc, id desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    company_id = fields.Many2one(
        "res.company", required=True, index=True,
        default=lambda self: self.env.company)

    # ── canonical links (explicit, never inferred) ──────────────────
    hr_applicant_id = fields.Many2one(
        "hr.applicant", string="Job application", ondelete="restrict",
        index=True,
        help="The recruitment record and email thread. The owner-operator "
             "workflow advances THIS record's stage.")
    job_id = fields.Many2one(related="hr_applicant_id.job_id", store=True)
    partner_id = fields.Many2one(
        "res.partner", string="Applicant contact", ondelete="restrict",
        index=True,
        help="The exact partner created for/linked to this application. It is "
             "NEVER re-matched to an existing partner by email address.")
    operator_id = fields.Many2one(
        "prema.owner.operator", string="Operator profile", index=True,
        ondelete="restrict")
    fleet_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Equipment", index=True, ondelete="restrict",
        help="Set ONCE at approval. Its presence is what makes re-approval "
             "idempotent — a second approval returns this same vehicle.")

    # ── contact / business ──────────────────────────────────────────
    contact_name = fields.Char(string="Full name")
    email = fields.Char()
    phone = fields.Char()
    business_name = fields.Char(string="Legal business / owner name")
    business_number = fields.Char(string="Business / registration no.")
    business_structure = fields.Selection([
        ("sole", "Sole proprietor"),
        ("corporation", "Corporation"),
        ("partnership", "Partnership"),
    ], string="Business structure")
    is_sole_proprietor = fields.Boolean(
        string="Sole proprietor",
        help="Kept in step with Business structure; drives the conditional "
             "document set.")

    # ── home base: CITY + PROVINCE only, by design ──────────────────
    home_city = fields.Char(string="Home city")
    home_province = fields.Char(
        string="Home province",
        help="Province or state only. No street address is collected here.")
    service_province_ids = fields.Many2many(
        "res.country.state", "prema_owner_operator_service_province_rel",
        "application_id", "state_id", string="Provinces / territories served",
        domain=[("country_id.code", "=", "CA")],
        help="Multiple Canadian provinces/territories selected on the form.")

    # ── equipment ───────────────────────────────────────────────────
    equipment_type = fields.Selection([
        ("dry_van", "Dry van"),
        ("reefer", "Reefer"),
        ("flatbed", "Flatbed"),
        ("step_deck", "Step deck"),
        ("straight_truck", "Straight truck"),
        ("other", "Other"),
    ], string="Equipment type")
    equipment_model = fields.Char(string="Make / model")
    vin = fields.Char(string="VIN", index=True)
    unit_number = fields.Char(string="Unit number")
    license_plate = fields.Char(string="Plate")
    plate_jurisdiction = fields.Char(string="Plate jurisdiction")

    # Units are part of the label on purpose — see the unit audit in the
    # master log: the legacy x_* fields mix ft and inches under *_ft names.
    payload_lbs = fields.Float(string="Legal payload (lbs)")
    pallet_capacity = fields.Integer(string="Pallet capacity")
    box_length_ft = fields.Float(string="Cargo box length (ft)")
    box_height_ft = fields.Float(string="Cargo box interior height (ft)")
    overall_length_ft = fields.Float(string="Overall length (ft)")
    vehicle_height_ft = fields.Float(string="Vehicle height (ft)")
    door_clearance_in = fields.Float(string="Door clearance (in)")
    dock_height_in = fields.Float(string="Dock height (in)")
    gvwr_lbs = fields.Float(string="GVWR (lbs)")
    has_reefer = fields.Boolean(string="Reefer")
    has_liftgate = fields.Boolean(string="Liftgate")
    has_air_ride = fields.Boolean(string="Air-ride suspension")

    # ── the live website's structured answers ───────────────────────
    commercial_driving_experience = fields.Selection([
        ("Less than 1 year", "Less than 1 year"),
        ("1–2 years", "1–2 years"),
        ("3–5 years", "3–5 years"),
        ("6–10 years", "6–10 years"),
        ("11–15 years", "11–15 years"),
        ("16+ years", "16+ years"),
    ], string="Commercial driving experience")
    owner_operator_experience = fields.Selection([
        ("New owner-operator", "New owner-operator"),
        ("Less than 1 year", "Less than 1 year"),
        ("1–2 years", "1–2 years"),
        ("3–5 years", "3–5 years"),
        ("6–10 years", "6–10 years"),
        ("11–15 years", "11–15 years"),
        ("16+ years", "16+ years"),
    ], string="Owner-operator experience")
    licence_suspension = fields.Selection([
        ("no", "No"), ("yes", "Yes")],
        string="Licence ever suspended / revoked")
    recent_collisions = fields.Selection([
        ("none", "None"), ("1", "1"), ("2", "2"), ("3+", "3 or more")],
        string="Collisions in the past 3 years")
    recent_convictions = fields.Selection([
        ("none", "None"), ("1", "1"), ("2", "2"), ("3+", "3 or more")],
        string="Driving convictions in the past 3 years")
    tdg_training = fields.Selection([
        ("yes", "Current certificate"),
        ("no", "No"),
        ("expired", "Expired / renewal needed")],
        string="Dangerous goods training")
    recent_driving_work = fields.Text(string="Recent driving work / clients")

    vehicle_type = fields.Selection([
        ("cargo_van", "Cargo Van"),
        ("straight", "Straight Truck"),
        ("tractor", "Tractor / Power Unit"),
    ], string="Vehicle type")
    truck_year = fields.Char(
        string="Model year",
        help="Stored as the website sends it, including “2000 or earlier”. "
             "Never cast into a numeric year.")
    vehicle_ownership = fields.Selection([
        ("own", "Own"), ("lease", "Lease"), ("financed", "Financed")],
        string="Own / lease / financed")
    transmission = fields.Selection([
        ("automatic", "Automatic"),
        ("manual", "Manual / standard"),
        ("automated_manual", "Automated manual")],
        string="Transmission")
    odometer_km = fields.Float(string="Odometer (km)")
    truck_plate_jurisdiction = fields.Char(
        string="Plate and province / territory",
        help="The combined value the applicant typed. license_plate and "
             "plate_jurisdiction hold the best-effort split.")
    cargo_body_type = fields.Selection([
        ("dry", "Dry freight"),
        ("reefer", "Refrigerated / reefer"),
        ("both", "Reefer and dry capable"),
        ("other", "Other / not applicable")],
        string="Cargo body type")
    cargo_width_in = fields.Float(string="Cargo interior width (in)")
    cargo_height_in = fields.Float(string="Cargo interior height (in)")
    liftgate = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Liftgate")
    liftgate_capacity_lb = fields.Float(string="Liftgate capacity (lb)")
    dock_height_capable = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Dock-height capable")
    air_ride = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Air-ride suspension")
    reefer_temperature_range = fields.Char(
        string="Reefer temperature range")
    trailer_ownership = fields.Selection([
        ("own", "Own / leased trailer"),
        ("power_only", "Power-only service")],
        string="Trailer ownership")
    trailer_type = fields.Selection([
        ("dry", "Dry Van"), ("reefer", "Reefer"),
        ("flatbed", "Flatbed"), ("other", "Other")],
        string="Trailer type")
    trailer_length_ft = fields.Float(string="Trailer length (ft)")
    trailer_unit = fields.Char(string="Trailer unit / identification")

    # ── authority (conditional in the form) ─────────────────────────
    crosses_border = fields.Boolean(
        string="Crosses the border (MC/USDOT required)")
    mc_number = fields.Char(string="MC number")
    usdot_number = fields.Char(string="USDOT number")
    authority_state = fields.Selection([
        ("not_provided", "Not provided"),
        ("pending_manual", "Pending manual verification"),
        ("verified", "Verified"),
    ], default="not_provided", required=True,
        help="MC/USDOT/CVOR lookup is deferred; the record shows PENDING "
             "MANUAL verification until someone checks it.")
    cvor_number = fields.Char(string="CVOR number")
    cvor_verification_status = fields.Selection([
        ("not_provided", "Not provided"),
        ("pending", "Pending manual verification"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ], default="not_provided", required=True,
        help="Formatting the number is NOT authority verification.")
    insurance_agent_name = fields.Char(string="Insurance agent / broker")
    insurance_agent_email = fields.Char(string="Insurance agent email")

    # ── driver licence (collected privately at Initial Qualification) ─
    licence_class = fields.Char(string="Licence class")
    licence_jurisdiction = fields.Char(string="Licence jurisdiction")
    licence_expiry = fields.Date(string="Licence expiry")

    # ── preferences ─────────────────────────────────────────────────
    preferred_lanes = fields.Text(
        string="Preferred lanes (legacy free text)",
        help="Set from the structured service area for older consumers.")
    preferred_service_area = fields.Selection([
        ("Toronto / GTA", "Toronto / GTA local"),
        ("Ontario wide", "Ontario wide"),
        ("Quebec", "Quebec"),
        ("Ontario and Quebec", "Ontario and Quebec"),
        ("Canada wide", "Canada wide"),
        ("Selected provinces", "Selected provinces / territories"),
    ], string="Preferred service area")
    available_start_date = fields.Date(string="Available start date")
    work_preference = fields.Selection([
        ("local", "Local"), ("regional", "Regional"),
        ("longhaul", "Long haul"), ("flexible", "Flexible")],
        string="Work preference")
    whatsapp_consent = fields.Selection([
        ("yes", "Yes"), ("no", "No")],
        string="WhatsApp load notifications")
    application_declaration = fields.Boolean(
        string="Application declaration signed")
    availability = fields.Text(string="Availability (legacy free text)")
    additional_notes = fields.Html(
        related="hr_applicant_id.applicant_notes", readonly=True,
        string="Additional information / equipment notes")

    # ── workflow ────────────────────────────────────────────────────
    state = fields.Selection(APPLICATION_STATE, default="submitted",
                             required=True, index=True, tracking=True)
    stage_id = fields.Many2one(related="hr_applicant_id.stage_id",
                               string="Recruitment stage", store=True)
    document_ids = fields.One2many(
        "prema.owner.operator.document", "application_id", string="Documents")

    sign_request_id = fields.Many2one("sign.request", string="Sign request",
                                      ondelete="restrict", copy=False)
    sign_template_id = fields.Many2one("sign.template", string="Agreement",
                                       ondelete="restrict", copy=False)
    contract_sent_on = fields.Datetime()
    contract_signed_on = fields.Datetime()

    approved_on = fields.Datetime(readonly=True, copy=False)
    approved_by = fields.Many2one("res.users", readonly=True, copy=False)

    missing_initial = fields.Text(compute="_compute_missing")
    missing_qualification = fields.Text(compute="_compute_missing")
    missing_contract = fields.Text(compute="_compute_missing")
    missing_verification = fields.Text(
        string="Documents not yet reviewed",
        compute="_compute_missing",
        help="A file that was merely received is not a reviewed document. "
             "Final approval needs every required document VERIFIED.")
    licence_current = fields.Boolean(compute="_compute_missing")
    can_approve = fields.Boolean(compute="_compute_missing")

    # ------------------------------------------------------------------
    # completion
    # ------------------------------------------------------------------
    def _required_types(self):
        """The document types this application must have, by stage bucket.

        Conditional rules live here, in one place, so the form and the
        server-side gate can never disagree:
          * a sole proprietor is not asked for corporate registration;
          * a photo set is always required (front/rear/both sides/interior).
        """
        self.ensure_one()
        initial = ["cvor", "coi_applicant", "photo_front", "photo_rear",
                   "photo_left", "photo_right", "photo_cargo_interior"]
        if not self.is_sole_proprietor:
            initial.append("business_registration")
        return {
            "initial": initial,
            "qualification": ["licence_front", "licence_back", "coi_insurer"],
            "contract": ["signed_agreement"],
        }

    def _blocking(self, bucket):
        self.ensure_one()
        have = self.document_ids.filtered(
            lambda d: d.status in ("received", "verified")).mapped(
                "document_type")
        return [t for t in self._required_types()[bucket] if t not in have]

    def _verified_missing(self, bucket):
        """Types that are present but not yet VERIFIED by a reviewer."""
        self.ensure_one()
        have = self.document_ids.filtered(
            lambda d: d.status == "verified").mapped("document_type")
        return [t for t in self._required_types()[bucket] if t not in have]

    def _label(self, types):
        labels = dict(DOCUMENT_TYPES)
        return ", ".join(labels.get(t, t) for t in types)

    @api.depends("document_ids.status", "document_ids.document_type",
                 "is_sole_proprietor", "operator_id.compliance_ok",
                 "contract_signed_on", "fleet_vehicle_id",
                 "licence_expiry", "licence_class")
    def _compute_missing(self):
        today = fields.Date.context_today(self)
        for app in self:
            ini = app._blocking("initial")
            qual = app._blocking("qualification")
            con = app._blocking("contract")
            app.missing_initial = app._label(ini) or False
            app.missing_qualification = app._label(qual) or False
            app.missing_contract = app._label(con) or False
            unverified = []
            for bucket in ("initial", "qualification", "contract"):
                for doc_type in app._verified_missing(bucket):
                    if doc_type not in unverified:
                        unverified.append(doc_type)
            app.missing_verification = app._label(unverified) or False
            app.licence_current = bool(
                app.licence_class and app.licence_expiry
                and app.licence_expiry >= today)
            app.can_approve = bool(
                not unverified and app.licence_current
                and app.contract_signed_on
                and app.operator_id and app.operator_id.compliance_ok
                and not app.fleet_vehicle_id
                and app.state not in ("rejected",))
            # licence fields live on the application, not in the checklist
            if app.licence_class and app.licence_expiry:
                app.missing_qualification = app.missing_qualification or False
            else:
                extra = _("driver licence class and expiry")
                app.missing_qualification = ", ".join(
                    x for x in (app.missing_qualification, extra) if x)

    # ------------------------------------------------------------------
    # form submission (public)
    # ------------------------------------------------------------------
    @api.model
    def _create_from_public(self, vals):
        """Create application + applicant + checklist in ONE transaction.

        The only supported entry point for a website submission. Access is
        public by necessity, so only ``SUBMISSION_FIELDS`` are accepted and
        every record is created after validation, never before.
        """
        data = {k: v for k, v in vals.items() if k in SUBMISSION_FIELDS}
        notes = (vals.get("applicant_notes") or "").strip()
        missing = [field for field in REQUIRED_INTERNAL
                   if not data.get(field)]
        if missing:
            labels = [
                self._fields[f].string for f in missing
                if f in self._fields]
            raise ValidationError(_(
                "Please complete: %s.") % ", ".join(labels))
        email = (data.get("email") or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValidationError(
                _("That email address does not look valid."))
        cvor = (data.get("cvor_number") or "").strip()
        if cvor and not re.match(r"^\d{3}-\d{3}-\d{3}$", cvor):
            raise ValidationError(
                _("CVOR must contain exactly 9 digits (###-###-###)."))
        if not (data.get("home_city") or "").strip() \
                or not (data.get("home_province") or "").strip():
            raise ValidationError(
                _("Please give your home city and province."))
        job = self._public_job()
        if not job:
            raise ValidationError(
                _("The owner-operator position is not open right now."))

        # Odoo 18 requires a candidate on every applicant; the candidate owns
        # the partner. We keep THAT partner explicitly rather than looking one
        # up by email later.
        candidate = self.env["hr.candidate"].sudo().create({
            "partner_name": data["contact_name"],
            "email_from": data["email"],
            "company_id": job.company_id.id,
        })
        applicant = self.env["hr.applicant"].sudo().create({
            "candidate_id": candidate.id,
            "job_id": job.id,
            "partner_name": data["contact_name"],
            "email_from": data["email"],
            "partner_phone": data.get("phone") or False,
            "applicant_notes": notes or False,
            "company_id": job.company_id.id,
        })
        # hr.applicant.create() already builds the onboarding record for the
        # owner-operator job. REUSE it — creating another here would give the
        # applicant two applications and break the reply/backfill guarantees.
        app = (applicant.owner_operator_application_id
               or applicant.owner_operator_application_ids[:1])
        if not app:
            app = self.sudo().create({
                "hr_applicant_id": applicant.id,
                "partner_id": applicant.partner_id.id or False,
                "company_id": job.company_id.id,
                "contact_name": data["contact_name"],
                "email": data["email"],
                "phone": data.get("phone") or False,
            })
        else:
            app.sudo().write({
                "partner_id":
                    app.partner_id.id or applicant.partner_id.id or False,
                "company_id": app.company_id.id or job.company_id.id,
                "contact_name": app.contact_name or data["contact_name"],
                "email": app.email or data["email"],
                "phone": app.phone or data.get("phone") or False,
            })
        app.name = "OO-%06d" % app.id
        app.apply_submission_data(vals)
        app._ensure_checklist()
        app.message_post(body=_("Application received from the website."),
                         subtype_xmlid="mail.mt_note")
        return app

    def apply_submission_data(self, vals, only_empty=False):
        """Write validated public answers, deriving the legacy roll-ups.

        ``only_empty`` is the audit backfill mode: it never overwrites a value
        a reviewer has already corrected.
        """
        self.ensure_one()
        vals = dict(vals)
        supplied_type = vals.get("vehicle_type")
        if supplied_type == "tractor":
            # A power unit has no cargo body; never store capacity answers.
            for field in (
                    "cargo_body_type", "payload_lbs", "pallet_capacity",
                    "box_length_ft", "cargo_width_in", "cargo_height_in",
                    "door_clearance_in", "liftgate", "liftgate_capacity_lb",
                    "dock_height_capable", "reefer_temperature_range"):
                vals.pop(field, None)
        elif supplied_type in ("cargo_van", "straight"):
            for field in (
                    "trailer_ownership", "trailer_type",
                    "trailer_length_ft", "trailer_unit"):
                vals.pop(field, None)
        write_vals = {}

        def put(field, value):
            if field not in self._fields:
                return
            if value is None or value == "":
                return
            if only_empty and self[field]:
                return
            if self[field] == value:
                return
            write_vals[field] = value

        for key, value in vals.items():
            if key in SUBMISSION_FIELDS:
                put(key, value)

        raw_plate = write_vals.get("truck_plate_jurisdiction")
        if raw_plate:
            plate_no, jurisdiction = split_plate_jurisdiction(raw_plate)
            if plate_no and "license_plate" not in write_vals:
                put("license_plate", plate_no[:32])
            if jurisdiction and "plate_jurisdiction" not in write_vals:
                put("plate_jurisdiction", jurisdiction[:64])

        codes = vals.get("service_province_codes") or []
        if codes:
            states = self.env["res.country.state"].search([
                ("country_id.code", "=", "CA"), ("code", "in", codes)])
            if states and set(states.ids) != set(self.service_province_ids.ids):
                write_vals["service_province_ids"] = [(6, 0, states.ids)]

        business = write_vals.get(
            "business_structure", self.business_structure)
        if business:
            put("is_sole_proprietor", business == "sole")

        vehicle = write_vals.get("vehicle_type", self.vehicle_type)
        trailer = write_vals.get("trailer_type", self.trailer_type)
        cargo = write_vals.get("cargo_body_type", self.cargo_body_type)
        liftgate = write_vals.get("liftgate", self.liftgate)
        air_ride = write_vals.get("air_ride", self.air_ride)
        if vehicle == "tractor":
            put("has_reefer", trailer == "reefer")
            put("has_liftgate", False)
            put("equipment_type", {
                "dry": "dry_van", "reefer": "reefer",
                "flatbed": "flatbed"}.get(trailer, "other"))
        elif vehicle in ("cargo_van", "straight"):
            put("has_reefer", cargo in ("reefer", "both"))
            put("has_liftgate", liftgate == "yes")
            put("equipment_type",
                "straight_truck" if vehicle == "straight" else "other")
        if air_ride:
            put("has_air_ride", air_ride == "yes")

        area = write_vals.get(
            "preferred_service_area", self.preferred_service_area)
        if area:
            put("preferred_lanes", area)
        start = write_vals.get(
            "available_start_date", self.available_start_date)
        if start:
            put("availability",
                start.isoformat() if hasattr(start, "isoformat") else str(start))
        if "cvor_number" in write_vals \
                and self.cvor_verification_status == "not_provided":
            write_vals["cvor_verification_status"] = "pending"

        if write_vals:
            self.write(write_vals)
        return bool(write_vals)

    @api.model
    def _backfill_from_other_information(self):
        """Replay saved “Other Information” notes into structured fields.

        Repeat-safe: only empty fields are filled, the original note is left
        untouched, and no email, signature or Fleet record is created.
        """
        job = self._public_job()
        if not job:
            return 0
        updated = 0
        applicants = self.env["hr.applicant"].sudo().search(
            [("job_id", "=", job.id)])
        for applicant in applicants:
            app = applicant.owner_operator_application_id
            if not app:
                app = applicant.owner_operator_application_ids[:1]
            if not app:
                app = applicant._ensure_owner_application()
            if not app:
                continue
            raw = {}
            for message in applicant.message_ids:
                raw.update(parse_other_information(message.body or ""))
            if not raw:
                continue
            vals, _errors = normalise_public(raw, strict=False)
            if app.apply_submission_data(vals, only_empty=True):
                updated += 1
        return updated

    @api.model
    def _public_job(self):
        job_id = self.env["ir.config_parameter"].sudo().get_param(
            "prema_owner_operator.job_id")
        job = self.env["hr.job"].sudo().browse(
            int(job_id)).exists() if job_id else self.env["hr.job"]
        if not job:
            job = self.env["hr.job"].sudo().search(
                [("id", "=", 5)], limit=1)
        return job if job and job.is_published else self.env["hr.job"]

    def _ensure_checklist(self):
        """Create one row per required type, idempotently."""
        self.ensure_one()
        Document = self.env["prema.owner.operator.document"].sudo()
        have = self.document_ids.mapped("document_type")
        types = [t for bucket in self._required_types().values()
                 for t in bucket]
        for doc_type in types:
            if doc_type in have:
                continue
            Document.create({
                "application_id": self.id,
                "document_type": doc_type,
                "source": "insurer" if doc_type == "coi_insurer" else "applicant",
                "status": "pending",
            })
        # A requirement that no longer applies (a sole proprietor switching
        # away from corporate registration) loses its PENDING row only — a
        # document already received is never discarded.
        stale = self.document_ids.filtered(
            lambda d: d.status == "pending" and d.document_type not in types)
        stale.sudo().unlink()
        return True

    # ------------------------------------------------------------------
    # stage flow
    # ------------------------------------------------------------------
    def _job_stages(self):
        """Odoo 18 links stages to jobs from the STAGE side (stage.job_ids);
        hr.job has no stage_ids field."""
        self.ensure_one()
        job = self.hr_applicant_id.job_id
        if not job:
            return self.env["hr.recruitment.stage"]
        return self.env["hr.recruitment.stage"].search(
            [("job_ids", "in", job.id)]).sorted(
                key=lambda s: (s.sequence, s.id))

    def _next_stage(self):
        self.ensure_one()
        current = self.hr_applicant_id.stage_id
        for stage in self._job_stages():
            if stage.sequence > current.sequence:
                return stage
        return self.env["hr.recruitment.stage"]

    def _guard_transition(self, target):
        """Refuse to advance while the target stage's documents are missing."""
        self.ensure_one()
        seq = target.sequence
        problems = []
        if seq >= 1 and self._blocking("initial"):
            problems.append(_("initial uploads: %s")
                            % self._label(self._blocking("initial")))
        if seq >= 4 and self._blocking("qualification"):
            problems.append(_("qualification documents: %s")
                            % self._label(self._blocking("qualification")))
        if seq >= 4 and not (self.licence_class and self.licence_expiry):
            problems.append(_("driver licence class and expiry"))
        if seq >= 4 and not self.licence_current:
            problems.append(_("a CURRENT driver licence (expiry has passed)"))
        if seq >= 5 and (self._blocking("contract") or not self.contract_signed_on):
            problems.append(_("a completed signature"))
        if seq >= 6:
            unverified = []
            for bucket in ("initial", "qualification", "contract"):
                for doc_type in self._verified_missing(bucket):
                    if doc_type not in unverified:
                        unverified.append(doc_type)
            if unverified:
                problems.append(_("reviewed documents: %s")
                                % self._label(unverified))
            if not self.operator_id or not self.operator_id.compliance_ok:
                problems.append(_("a compliant operator profile "
                                  "(authority + insurer-direct COI)"))
            if not self._vin_clear():
                problems.append(_("VIN conflict resolution"))
        if problems:
            raise UserError(_(
                "Cannot move to “%s” yet:\n• %s")
                % (target.name, "\n• ".join(problems)))

    def action_advance(self):
        for app in self:
            target = app._next_stage()
            if not target:
                raise UserError(_(
                    "%s has no next stage: the job’s recruitment pipeline is "
                    "not configured (or this is its last stage). Fix the job’s "
                    "stages before advancing.") % app.name)
            app._guard_transition(target)
            app.hr_applicant_id.sudo().write({"stage_id": target.id})
            app._sync_state(target)
            app._send_stage_mail(target)
        return True

    def _sync_state(self, stage):
        self.ensure_one()
        seq = stage.sequence
        state = "submitted"
        if seq >= 1:
            state = "qualifying"
        if seq >= 3:
            state = "authority_review"
        if seq >= 4:
            state = "contract"
        if seq >= 5:
            state = "signed"
        if seq >= 6:
            state = "approved"
        self.write({"state": state})

    # ------------------------------------------------------------------
    # manual CVOR review (formatting is not verification)
    # ------------------------------------------------------------------
    def action_verify_cvor(self):
        self.write({"cvor_verification_status": "verified"})
        return True

    def action_reject_cvor(self):
        self.write({"cvor_verification_status": "rejected"})
        return True

    # ------------------------------------------------------------------
    # contract (Odoo Sign) — blocked while no approved template exists
    # ------------------------------------------------------------------
    @api.model
    def _agreement_template(self):
        """The configured agreement template, ONLY if it is active.

        Returns an empty recordset when nothing usable is configured — the
        caller must then refuse to send. This module never activates an
        unrelated template and never invents contract text.
        """
        icp = self.env["ir.config_parameter"].sudo()
        tmpl_id = icp.get_param("prema_owner_operator.agreement_template_id")
        tmpl = self.env["sign.template"].sudo().browse(
            int(tmpl_id)).exists() if tmpl_id else self.env["sign.template"]
        return tmpl if tmpl and tmpl.active else self.env["sign.template"]

    def action_send_contract(self):
        self.ensure_one()
        if self.sign_request_id:
            return self.sign_request_id
        tmpl = self._agreement_template()
        if not tmpl:
            raise UserError(_(
                "No approved owner-operator agreement/NDA template is "
                "configured for Odoo Sign. Upload and activate the approved "
                "template and set prema_owner_operator.agreement_template_id "
                "— contract sending and final approval stay blocked until "
                "then. No contract text is generated by this system."))
        if not self.partner_id:
            raise UserError(_("The application has no contact to sign."))
        request = self.env["sign.request"].sudo().create({
            "template_id": tmpl.id,
            "reference": self.name,
            "subject": _("Owner-Operator Agreement — %s") % self.name,
            "request_item_ids": [(0, 0, {
                "partner_id": self.partner_id.id,
                "role_id": tmpl.sign_item_ids[:1].role_id.id,
            })],
        })
        self.write({"sign_request_id": request.id,
                    "sign_template_id": tmpl.id,
                    "contract_sent_on": fields.Datetime.now()})
        self.message_post(body=_("Agreement sent for signature."),
                          subtype_xmlid="mail.mt_note")
        return request

    @api.model
    def _cron_sync_signatures(self):
        """Mark applications signed when the Sign request completes."""
        for app in self.search([("sign_request_id", "!=", False),
                                ("contract_signed_on", "=", False)]):
            if app.sign_request_id.state == "signed":
                doc = app.document_ids.filtered(
                    lambda d: d.document_type == "signed_agreement")[:1]
                if doc and doc.status != "verified":
                    doc.sudo().write({
                        "status": "verified",
                        "attachment_id": app.sign_request_id.signed_document_id.id,
                        "verified_on": fields.Datetime.now(),
                    })
                app.write({"contract_signed_on": fields.Datetime.now()})
                app.message_post(body=_("Agreement signed."),
                                 subtype_xmlid="mail.mt_note")
        return True

    # ------------------------------------------------------------------
    # final approval → Fleet hand-off (idempotent, transactional)
    # ------------------------------------------------------------------
    def _vin_clear(self):
        """True when no OTHER application owns this VIN and no existing
        vehicle belongs to someone else.

        The intake form deliberately collects no VIN, so an empty VIN is not
        a conflict — there is simply nothing to match.
        """
        self.ensure_one()
        vin = _normalise_vin(self.vin)
        if not vin:
            return True
        Vehicle = self.env["fleet.vehicle"].sudo()
        for vehicle in Vehicle.search([("vin_sn", "!=", False)]):
            if _normalise_vin(vehicle.vin_sn) != vin:
                continue
            if self.fleet_vehicle_id and vehicle.id == self.fleet_vehicle_id.id:
                return True
            # an existing vehicle is a conflict unless it is already ours
            if vehicle.operator_id and self.operator_id \
                    and vehicle.operator_id.id == self.operator_id.id:
                return True
            return False
        return True

    def _resolve_vehicle_model(self):
        """fleet.vehicle.model_id is mandatory, so the applicant's stated
        make/model becomes a catalogue entry if the fleet has never seen it.
        Nothing is guessed: the record is named exactly what they wrote."""
        self.ensure_one()
        Model = self.env["fleet.vehicle.model"].sudo()
        name = (self.equipment_model or "").strip()
        if not name:
            name = "Owner-operator equipment"
        model = Model.search([("name", "=", name)], limit=1)
        if model:
            return model
        # brand_id is mandatory too: match the brand the applicant named, and
        # only fall back to a neutral one when the text does not name a known
        # brand.
        Brand = self.env["fleet.vehicle.model.brand"].sudo()
        brand = Brand
        low = name.lower()
        for candidate in Brand.search([]):
            if candidate.name and candidate.name.lower() in low:
                brand = candidate
                break
        if not brand:
            brand = Brand.search([("name", "=", "Owner-operator")], limit=1)
        if not brand:
            brand = Brand.create({"name": "Owner-operator"})
        return Model.create({"name": name, "brand_id": brand.id})

    def action_approve(self):
        """Create/link the operator profile and its fleet.vehicle.

        Idempotent: a second call returns the SAME vehicle because the link
        is stored on this application. A VIN/ownership conflict raises for
        manual review instead of merging records.
        """
        self.ensure_one()
        if self.fleet_vehicle_id:
            return self.fleet_vehicle_id
        if not self.can_approve:
            raise UserError(_(
                "This application cannot be approved yet — missing documents, "
                "signature or compliance."))
        if not self._vin_clear():
            raise UserError(_(
                "This VIN is already attached to other equipment or another "
                "owner-operator. Resolve the conflict manually before "
                "approval — the system will not merge vehicles."))

        Operator = self.env["prema.owner.operator"].sudo()
        operator = self.operator_id
        if not operator:
            operator = Operator.create({
                "partner_id": self.partner_id.id,
                "company_id": self.company_id.id,
                "is_sole_proprietor": self.is_sole_proprietor,
                "business_number": self.business_number,
                "home_city": self.home_city,
                "home_province": self.home_province,
                "mc_number": self.mc_number,
                "usdot_number": self.usdot_number,
                "cvor_number": self.cvor_number,
                "insurance_agent_name": self.insurance_agent_name,
                "insurance_agent_email": self.insurance_agent_email,
                "authority_state": self.authority_state,
            })
            self.operator_id = operator.id
        else:
            updates = {}
            for field in ("cvor_number", "insurance_agent_name",
                          "insurance_agent_email", "home_city",
                          "home_province"):
                if not operator[field] and self[field]:
                    updates[field] = self[field]
            if updates:
                operator.write(updates)
        self.partner_id.sudo().write({"supplier_rank": 1})

        ready = self.env["fleet.vehicle.state"].sudo().search(
            [("name", "=", "Ready")], limit=1)
        model = self._resolve_vehicle_model()
        year = (self.truck_year or "").strip()
        if year.isdigit() and "model_year" in model._fields \
                and not model.model_year:
            # model_year lives on the shared model; never overwrite a value.
            model.write({"model_year": int(year)})
        transmission = {
            "automatic": "automatic", "manual": "manual",
        }.get(self.transmission)
        # x_box_interior_height_ft stores INCHES despite its _ft name; the
        # application now collects inches directly. The legacy feet field is
        # only converted for older records.
        interior_height = self.cargo_height_in or (
            self.box_height_ft * 12.0 if self.box_height_ft else 0.0)
        vehicle_vals = {
            "model_id": model.id,
            "application_id": self.id,
            "operator_id": operator.id,
            "company_id": self.company_id.id,
            "license_plate": self.license_plate,
            "vin_sn": self.vin or False,
            "home_city": self.home_city,
            "home_province": self.home_province,
            "driver_id": self.partner_id.id,
            "x_operational_logistics": True,
            "x_reefer": self.has_reefer,
            "x_liftgate": self.has_liftgate,
            "x_air_ride": self.has_air_ride,
            "x_max_pallets": self.pallet_capacity or 0,
            # Cargo payload — never GVWR.
            "x_max_payload_lbs": self.payload_lbs or 0.0,
            "x_cargo_box_length_ft": self.box_length_ft or 0.0,
            "x_box_interior_height_ft": interior_height,
            "x_overall_length_ft": self.overall_length_ft or 0.0,
            "x_vehicle_height_ft": self.vehicle_height_ft or 0.0,
            "x_door_clearance_in": self.door_clearance_in or 0.0,
            "x_dock_height_in": self.dock_height_in or 0.0,
            "x_current_odometer_km": self.odometer_km or 0.0,
            "state_id": ready.id or False,
        }
        if transmission:
            vehicle_vals["transmission"] = transmission
        if "x_studio_dock_height" in self.env["fleet.vehicle"]._fields:
            vehicle_vals["x_studio_dock_height"] = (
                self.dock_height_capable == "yes")
        vehicle = self.env["fleet.vehicle"].sudo().create(vehicle_vals)
        # Unit number: reuse the Studio field the fleet already uses; never
        # create a second column for the same thing.
        if self.unit_number and "x_studio_unit_number" in self.env[
                "fleet.vehicle"]._fields:
            vehicle.write({"x_studio_unit_number": self.unit_number})
        self.write({"fleet_vehicle_id": vehicle.id,
                    "approved_on": fields.Datetime.now(),
                    "approved_by": self.env.user.id,
                    "state": "approved"})
        self.message_post(
            body=_("Approved — equipment %s handed to Fleet.")
            % (vehicle.license_plate or vehicle.id),
            subtype_xmlid="mail.mt_note")
        return vehicle

    # ------------------------------------------------------------------
    # stage emails
    # ------------------------------------------------------------------
    def _send_stage_mail(self, stage):
        self.ensure_one()
        xmlid = self.env["ir.config_parameter"].sudo().get_param(
            "prema_owner_operator.stage_mail_%s" % stage.sequence)
        if not xmlid:
            return False
        tmpl = self.env.ref(xmlid, raise_if_not_found=False)
        if tmpl:
            tmpl.sudo().send_mail(self.id, force_send=False)
        return True

# -*- coding: utf-8 -*-
"""prema.owner.operator.document + .application.

The application is the ONE structured record for a candidate, and every
document, signature and approval hangs off it explicitly. Nothing is matched
by email address and nothing is matched by number plate: an existing partner
or vehicle is only ever linked through an id stored on this record, and a VIN
that already belongs to someone else stops the approval for manual review.
"""
import base64
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..application_mapping import (
    CARGO_BODIES, PROVINCES, REQUIRED_INTERNAL, TRAILER_TYPES, VEHICLE_TYPES,
    normalise_public, parse_other_information, split_plate_jurisdiction)

# Ordered so the checklist reads the way the process runs.
DOCUMENT_TYPES = [
    ("cvor", "CVOR / provincial certificate"),    ("coi_applicant", "Certificate of insurance (applicant copy)"),
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

# Exactly the 13 Canadian province/territory choices, alphabetically.
PROVINCE_CHOICES = [
    ("AB", "Alberta"),
    ("BC", "British Columbia"),
    ("MB", "Manitoba"),
    ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NT", "Northwest Territories"),
    ("NU", "Nunavut"),
    ("ON", "Ontario"),
    ("PE", "Prince Edward Island"),
    ("QC", "Quebec"),
    ("SK", "Saskatchewan"),
    ("YT", "Yukon"),
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
    ("verified", "Insurance verified"),
    ("contract", "Contract proposal"),
    ("signed", "Contract signed"),
    ("approved", "Approved — handed to Fleet"),
    ("rejected", "Rejected"),
]

VERIFICATION_STATUS = [
    ("pending", "PENDING"),
    ("review", "REVIEW"),
    ("verified", "VERIFIED"),
]

# The owner-operator pipeline for job 5, in order. Names are the stable key;
# sequences are assigned by the migration/hook. The removed stages keep their
# records and any history, but job 5 is unlinked from them.
STAGE_NEW = "New"
STAGE_AUTHORITY = "Authority & Insurance Review"
STAGE_VERIFIED = "Verified"
STAGE_CONTRACT = "Contract Proposal"
STAGE_SIGNED = "Contract Signed"
STAGE_APPROVED = "Approved — Ready for Dispatch"
PIPELINE = [
    STAGE_NEW,
    STAGE_AUTHORITY,
    STAGE_VERIFIED,
    STAGE_CONTRACT,
    STAGE_SIGNED,
    STAGE_APPROVED,
]
REMOVED_FROM_JOB5 = [
    "Equipment Verification",
    "Initial Qualification",
    "Final Review",
    "First Interview",
    "Second Interview",
]

# The exact certificate-holder wording the applicant forwards to the broker.
CERTIFICATE_HOLDER = (
    "PremaFirm Inc.\n"
    "994 Westport Cres, Unit 7A\n"
    "Mississauga, ON L5T 1G1\n"
    "Canada"
)

WHATSAPP_CONSENT_TEXT = (
    "I agree to receive load quote requests and load offers by WhatsApp at "
    "the number I provided. I may change this preference or opt out at any "
    "time."
)

# Raw public-form keys -> structured equipment fields. A key is "provided"
# when the submitted string is non-empty, even when it is exactly "0".
EQUIPMENT_RAW_MAP = {
    "CargoBodyType": "cargo_body_type",
    "LegalPayloadLb": "payload_lbs",
    "PalletCapacity": "pallet_capacity",
    "CargoLengthFt": "box_length_ft",
    "CargoWidthIn": "cargo_width_in",
    "CargoHeightIn": "cargo_height_in",
    "DoorWidthIn": "door_width_in",
    "DoorHeightIn": "door_clearance_in",
    "Liftgate": "liftgate",
    "LiftgateCapacityLb": "liftgate_capacity_lb",
    "LiftgatePlatformLengthIn": "liftgate_platform_length_in",
    "LiftgatePlatformWidthIn": "liftgate_platform_width_in",
    "DockHeightCapable": "dock_height_capable",
    "ReeferTemperatureRange": "reefer_temperature_range",
    "TrailerType": "trailer_type",
    "TrailerLengthFt": "trailer_length_ft",
}

EQUIPMENT_LABELS = {
    "cargo_body_type": "Cargo body type",
    "payload_lbs": "Legal payload",
    "gvwr_lbs": "GVWR",
    "pallet_capacity": "Pallet capacity",
    "box_length_ft": "Cargo interior length",
    "cargo_width_in": "Cargo interior width",
    "cargo_height_in": "Cargo interior height",
    "door_width_in": "Rear door opening width",
    "door_clearance_in": "Rear door opening height",
    "liftgate": "Liftgate",
    "liftgate_capacity_lb": "Liftgate capacity",
    "liftgate_platform_length_in": "Liftgate platform length",
    "liftgate_platform_width_in": "Liftgate platform width",
    "dock_height_capable": "Dock-height capability",
    "reefer_temperature_range": "Reefer temperature range",
    "trailer_type": "Trailer type",
    "trailer_length_ft": "Trailer length",
    "trailer_year": "Trailer model year",
    "trailer_unit": "Trailer unit / identification",
}

# The ONLY fields the public form may write. Workflow, approval, signature,
# verification, Fleet and operator links are deliberately absent.
SUBMISSION_FIELDS = {
    "contact_name", "email", "phone", "business_name", "business_structure",
    "home_city", "home_province", "home_province_code", "home_city_place_id",
    "home_latitude", "home_longitude", "home_location_verified",
    "home_location_review", "home_location_review_reason",
    "commercial_driving_experience",
    "licence_class", "licence_jurisdiction", "licence_province",
    "owner_operator_experience",
    "licence_suspension", "recent_collisions", "recent_convictions",
    "tdg_training", "recent_driving_work", "vehicle_type", "equipment_model",
    "truck_make", "truck_model", "gvwr_lbs",
    "truck_year", "unit_number", "vehicle_ownership", "transmission",
    "odometer_km", "truck_plate_jurisdiction", "license_plate",
    "plate_jurisdiction", "plate_province", "cargo_body_type",
    "trailer_ownership",
    "trailer_type", "trailer_length_ft", "trailer_year", "trailer_unit",
    "payload_lbs",
    "pallet_capacity", "box_length_ft", "cargo_width_in", "cargo_height_in",
    "door_width_in", "door_clearance_in", "liftgate",
    "liftgate_capacity_lb", "liftgate_platform_length_in",
    "liftgate_platform_width_in", "dock_height_capable", "air_ride",
    "reefer_applicable", "reefer_temperature_range", "cvor_number",
    "preferred_service_area",
    "available_start_date", "work_preference", "preferred_service_area_codes",
    "work_preference_codes", "whatsapp_consent", "application_declaration",
    "equipment_provided", "load_notification_channel",
    "load_notification_email", "whatsapp_number",
    "use_contact_phone_for_whatsapp",
    "whatsapp_consent_granted", "whatsapp_consent_text",
    "whatsapp_consent_version", "whatsapp_consent_at",
    "load_notification_opt_out", "load_notification_opt_out_at",
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
    can_preview = fields.Boolean(
        string="View", compute="_compute_can_preview",
        help="True only while a file is attached; drives the row/detail "
             "preview button. Requested documents without a file show none.")

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

    @api.depends("attachment_id")
    def _compute_can_preview(self):
        for doc in self:
            doc.can_preview = bool(doc.attachment_id)

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
    driver_partner_id = fields.Many2one(
        "res.partner", string="Driver contact", index=True,
        ondelete="restrict", copy=False,
        help="The separate INDIVIDUAL driver contact created at approval. "
             "Never the business partner and never matched by name/email.")
    generated_tag_ids = fields.Many2many(
        "fleet.vehicle.tag", "prema_oo_app_generated_tag_rel",
        "application_id", "tag_id", string="Generated Fleet tags", copy=False,
        help="Only the tags this module generated. Manual Fleet tags are "
             "never removed.")
    documents_folder_id = fields.Many2one(
        "documents.document", string="Private documents folder",
        ondelete="set null", copy=False)
    licence_number = fields.Char(
        string="Driver licence number (reviewer)",
        help="Entered and verified by a reviewer; never inferred from an "
             "image.")
    licence_verification_status = fields.Selection([
        ("not_verified", "Not verified"),
        ("verified", "Verified"),
    ], default="not_verified", required=True, copy=False,
        string="Licence verification")
    licence_verified_on = fields.Datetime(readonly=True, copy=False)
    licence_verified_by = fields.Many2one(
        "res.users", readonly=True, copy=False)

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
    home_city = fields.Char(
        string="Home city",
        help="City where the truck normally parks or returns between jobs. "
             "Used to identify nearby load opportunities; no street address "
             "is collected at this stage.")
    home_province = fields.Char(
        string="Home province",
        help="Province or territory only, paired with the home city where the "
             "truck normally parks or returns between jobs. No street "
             "address is collected here.")
    home_province_code = fields.Selection(
        PROVINCE_CHOICES, string="Home province code")
    home_city_place_id = fields.Char(
        string="Home city Place ID", copy=False,
        help="Google Places ID for the selected Canadian city.")
    home_latitude = fields.Float(
        string="Home latitude", digits=(10, 7), copy=False)
    home_longitude = fields.Float(
        string="Home longitude", digits=(10, 7), copy=False)
    home_location_verified = fields.Boolean(
        string="City verified", copy=False,
        help="True only when a Google Places city selection was resolved to "
             "Canadian coordinates on the server.")
    home_location_review = fields.Boolean(
        string="Location needs review", copy=False,
        help="Set when the applicant had to enter the city manually because "
             "Google was unavailable or the place could not be verified.")
    home_location_review_reason = fields.Char(
        string="Location review reason", copy=False)
    service_province_ids = fields.Many2many(
        "res.country.state", "prema_owner_operator_service_province_rel",
        "application_id", "state_id", string="Provinces / territories served",
        domain=[("country_id.code", "=", "CA")],
        help="Multiple Canadian provinces/territories selected on the form.")
    preferred_service_area_ids = fields.Many2many(
        "prema.owner.operator.service.area",
        "prema_oo_app_service_area_rel", "application_id", "area_id",
        string="Preferred service areas",
        help="Multiple choices. The legacy single-value field below is kept "
             "only for historical records and older consumers.")
    work_preference_ids = fields.Many2many(
        "prema.owner.operator.work.preference",
        "prema_oo_app_work_pref_rel", "application_id", "preference_id",
        string="Work preferences",
        help="Multiple choices. The legacy single-value field below is kept "
             "only for historical records and older consumers.")

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
    door_width_in = fields.Float(string="Rear door opening width (in)")
    door_clearance_in = fields.Float(string="Rear door opening height (in)")
    liftgate_platform_length_in = fields.Float(
        string="Liftgate usable platform length (in)")
    liftgate_platform_width_in = fields.Float(
        string="Liftgate usable platform width (in)")
    dock_height_in = fields.Float(string="Dock height (in)")
    gvwr_lbs = fields.Float(string="GVWR (lbs)")
    equipment_provided = fields.Json(
        string="Submitted equipment answers", default=dict, copy=True,
        help="Which equipment answers the applicant actually supplied, "
             "including an explicit zero. Distinguishes NOT PROVIDED from 0.")
    equipment_data_confirmed = fields.Boolean(
        string="Equipment data reviewed",
        help="Set by a reviewer when a deliberate zero is valid for a field "
             "that the completeness gate would otherwise treat as missing.")
    equipment_missing = fields.Text(
        string="Missing dispatch-critical equipment", compute="_compute_equipment")
    equipment_complete = fields.Boolean(
        string="Dispatch equipment complete", compute="_compute_equipment")
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
        ("tractor", "Tractor Truck"),
        ("semi", "Semi-Truck"),
    ], string="Vehicle type")
    truck_make = fields.Char(
        string="Make",
        help="Separate from Model for new records; equipment_model keeps the "
             "legacy combined value for old records and Fleet matching.")
    truck_model = fields.Char(string="Model")
    truck_year = fields.Char(
        string="Model year",
        help="Stored as the website sends it, including “2000 or earlier”. "
             "Never cast into a numeric year.")
    vehicle_ownership = fields.Selection([
        ("own", "Owned"), ("lease", "Leased"), ("financed", "Financed")],
        string="Own / lease / financed")
    transmission = fields.Selection([
        ("automatic", "Automatic"),
        ("automated_manual", "Automated Manual"),
        ("manual", "Manual"),
        ("other", "Other")],
        string="Transmission")
    odometer_km = fields.Float(string="Odometer (km)")
    gvwr_lbs = fields.Float(string="GVWR (lb)")
    truck_plate_jurisdiction = fields.Char(
        string="Plate and province / territory",
        help="The combined value the applicant typed. license_plate and "
             "plate_jurisdiction hold the best-effort split.")
    plate_province = fields.Selection(
        PROVINCE_CHOICES, string="Plate province / territory")
    licence_province = fields.Selection(
        PROVINCE_CHOICES, string="Licence province / territory")
    cargo_body_type = fields.Selection([
        ("dry", "Dry freight"),
        ("reefer", "Refrigerated / reefer"),
        ("both", "Reefer and dry capable"),
        ("other", "Other / not applicable")],
        string="Cargo body type")
    cargo_width_in = fields.Float(string="Cargo interior width (in)")
    cargo_height_in = fields.Float(string="Cargo interior height (in)")
    liftgate = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Liftgate equipped?")
    liftgate_capacity_lb = fields.Float(string="Liftgate capacity (lb)")
    dock_height_capable = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Dock-height capable")
    air_ride = fields.Selection([
        ("yes", "Yes"), ("no", "No")], string="Air-ride suspension")
    reefer_applicable = fields.Selection([
        ("yes", "Yes"),
        ("no", "No"),
        ("not_applicable", "Not applicable")],
        string="Reefer applicable", default="not_applicable")
    reefer_temperature_range = fields.Char(
        string="Reefer temperature range")
    trailer_ownership = fields.Selection([
        ("own", "Own / leased trailer"),
        ("power_only", "Tractor only")],
        string="Trailer ownership")
    trailer_type = fields.Selection([
        ("dry", "Dry Van"), ("reefer", "Reefer"),
        ("flatbed", "Flatbed"), ("other", "Other")],
        string="Trailer type")
    trailer_length_ft = fields.Float(string="Trailer length (ft)")
    trailer_year = fields.Char(string="Trailer model year")
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
        string="WhatsApp load notifications (legacy)",
        help="Historical single answer. New records use the explicit "
             "notification channel and consent fields below.")
    load_notification_channel = fields.Selection([
        ("whatsapp", "WhatsApp"),
        ("email", "Email"),
        ("both", "WhatsApp and email"),
        ("none", "Do not send load opportunities"),
    ], string="Load-notification channel", tracking=True)
    load_notification_email = fields.Char(
        string="Load-notification email", tracking=True,
        help="Stored separately from the application email.")
    use_contact_phone_for_whatsapp = fields.Boolean(
        string="WhatsApp number is the contact phone", tracking=True,
        help="When set, whatsapp_number mirrors the normalized contact phone. "
             "It is never inferred without this explicit choice.")
    whatsapp_number = fields.Char(
        string="WhatsApp number", tracking=True,
        help="Normalised E.164 number, stored separately from the contact "
             "phone. Never inferred from the phone number alone.")
    whatsapp_consent_granted = fields.Boolean(
        string="WhatsApp consent given", tracking=True)
    whatsapp_consent_text = fields.Text(string="Consent wording")
    whatsapp_consent_version = fields.Char(string="Consent version")
    whatsapp_consent_at = fields.Datetime(string="Consent captured at")
    load_notification_opt_out = fields.Boolean(
        string="Opted out of load notifications", tracking=True)
    load_notification_opt_out_at = fields.Datetime(string="Opted out at")
    notification_preference_log_ids = fields.One2many(
        "prema.owner.operator.notification.log", "application_id",
        string="Notification preference changes")
    application_declaration = fields.Boolean(
        string="Application declaration signed")
    availability = fields.Text(string="Availability (legacy free text)")
    additional_notes = fields.Html(
        related="hr_applicant_id.applicant_notes", readonly=True,
        string="Additional information / equipment notes")

    # ── insurance verification (COI) ───────────────────────────────
    verification_status = fields.Selection(
        VERIFICATION_STATUS, default="pending", required=True, index=True,
        tracking=True,
        help="PENDING until the COI is processed, REVIEW when anything is "
             "missing/uncertain, VERIFIED only after every check or a manual "
             "reviewer approval.")
    verification_reasons = fields.Text(
        string="Verification review reasons", readonly=True, copy=False)
    verification_date = fields.Datetime(readonly=True, copy=False)
    verification_method = fields.Char(readonly=True, copy=False)
    verification_confidence = fields.Float(readonly=True, copy=False)
    verification_reviewer_id = fields.Many2one(
        "res.users", string="Verification approved by", readonly=True,
        copy=False)
    verification_reviewed_on = fields.Datetime(readonly=True, copy=False)
    verification_review_reason = fields.Text(
        string="Reviewer reason",
        help="Reviewer enters the reason before approving, rejecting or "
             "requesting an updated COI. It is kept in the audit trail.")
    coi_email_ids = fields.One2many(
        "prema.owner.operator.verification", "matched_application_id",
        string="COI emails", readonly=True)
    coi_email_count = fields.Integer(compute="_compute_coi_email_count")
    coi_version_ids = fields.One2many(
        "prema.owner.operator.coi.version", "application_id",
        string="COI versions", readonly=True)
    coi_current_version_id = fields.Many2one(
        "prema.owner.operator.coi.version", string="Current COI",
        readonly=True, copy=False, ondelete="restrict")

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
    contract_completed_on = fields.Datetime(
        string="Sign request fully completed", readonly=True, copy=False)
    setup_email_sent_at = fields.Datetime(readonly=True, copy=False)
    setup_email_message_id = fields.Char(readonly=True, copy=False)
    home_base_tag_issue = fields.Text(
        string="Home-base tag issue", readonly=True, copy=False,
        help="Set when city/province tags could not be applied. Correct the "
             "structured home city/province; never invent a location.")

    coi_request_sent_at = fields.Datetime(readonly=True, copy=False)
    coi_request_sent_by = fields.Many2one(
        "res.users", readonly=True, copy=False)
    coi_request_message_id = fields.Char(readonly=True, copy=False)
    coi_request_last_subject = fields.Char(readonly=True, copy=False)
    coi_request_count = fields.Integer(
        default=0, readonly=True, copy=False,
        help="How many times the insurer-direct COI request was sent.")
    coi_min_auto_liability = fields.Float(
        string="Review threshold — auto liability",
        compute="_compute_review_thresholds")
    coi_min_cargo = fields.Float(
        string="Review threshold — cargo",
        compute="_compute_review_thresholds")

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

    @api.depends("vehicle_type", "trailer_ownership", "trailer_type",
                 "trailer_length_ft", "trailer_year", "cargo_body_type",
                 "payload_lbs", "pallet_capacity", "box_length_ft",
                 "cargo_width_in", "cargo_height_in", "door_width_in",
                 "door_clearance_in", "liftgate", "liftgate_capacity_lb",
                 "reefer_applicable", "gvwr_lbs",
                 "reefer_temperature_range", "equipment_provided",
                 "equipment_data_confirmed")
    def _compute_equipment(self):
        for app in self:
            missing = []
            provided = app.equipment_provided or {}

            def has(field):
                if app.equipment_data_confirmed:
                    return bool(app[field]) or field in provided
                return bool(app[field]) or bool(provided.get(field))

            def need(field):
                if not has(field):
                    missing.append(EQUIPMENT_LABELS[field])

            vehicle = app.vehicle_type
            if vehicle in ("cargo_van", "straight"):
                need("cargo_body_type")
                need("payload_lbs")
                need("pallet_capacity")
                need("box_length_ft")
                need("cargo_width_in")
                need("cargo_height_in")
                need("door_width_in")
                need("door_clearance_in")
                need("liftgate")
                if app.liftgate == "yes":
                    need("liftgate_capacity_lb")
                if app.cargo_body_type == "reefer":
                    need("reefer_temperature_range")
            elif vehicle == "tractor":
                # Tractor only: no trailer, cargo body, capacity or reefer.
                pass
            elif vehicle == "semi":
                need("trailer_type")
                need("trailer_length_ft")
                need("trailer_year")
                need("trailer_unit")
                if app.trailer_type in ("dry", "reefer"):
                    need("cargo_body_type")
                    need("payload_lbs")
                    need("pallet_capacity")
                    need("box_length_ft")
                    need("cargo_width_in")
                    need("cargo_height_in")
                    need("door_width_in")
                    need("door_clearance_in")
                    need("liftgate")
                    if app.liftgate == "yes":
                        need("liftgate_capacity_lb")
                    if app.trailer_type == "reefer":
                        need("reefer_temperature_range")
                elif app.trailer_type == "flatbed":
                    need("payload_lbs")
                    need("box_length_ft")
                    need("cargo_width_in")
            else:
                missing.append(_("Vehicle type"))
            app.equipment_missing = ", ".join(missing) or False
            app.equipment_complete = not missing

    @api.depends("coi_email_ids")
    def _compute_coi_email_count(self):
        for app in self:
            app.coi_email_count = len(app.coi_email_ids)

    def _compute_review_thresholds(self):
        icp = self.env["ir.config_parameter"].sudo()
        auto = icp.get_param(
            "prema_owner_operator.coi_min_auto_liability") or 0.0
        cargo = icp.get_param(
            "prema_owner_operator.coi_min_cargo") or 0.0
        for app in self:
            app.coi_min_auto_liability = float(auto)
            app.coi_min_cargo = float(cargo)

    @api.depends("document_ids.status", "document_ids.document_type",
                 "is_sole_proprietor", "operator_id.compliance_ok",
                 "contract_signed_on", "fleet_vehicle_id",
                 "licence_expiry", "licence_class", "equipment_complete")
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
                and app.equipment_complete
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
        supplied_type = vals.get("vehicle_type") or self.vehicle_type
        cargo_fields = (
            "cargo_body_type", "payload_lbs", "pallet_capacity",
            "box_length_ft", "cargo_width_in", "cargo_height_in",
            "door_width_in", "door_clearance_in", "liftgate",
            "liftgate_capacity_lb", "liftgate_platform_length_in",
            "liftgate_platform_width_in", "dock_height_capable",
            "reefer_temperature_range",
        )
        trailer_fields = (
            "trailer_ownership", "trailer_type", "trailer_length_ft",
            "trailer_year", "trailer_unit",
        )
        if supplied_type == "tractor":
            # Tractor only: no trailer, cargo body, capacity or reefer.
            for field in cargo_fields + trailer_fields:
                vals[field] = False
            vals["trailer_ownership"] = "power_only"
            vals["reefer_applicable"] = "not_applicable"
        elif supplied_type == "semi":
            vals["trailer_ownership"] = "own"
            trailer_kind = vals.get("trailer_type") or self.trailer_type
            if trailer_kind in ("dry", "reefer"):
                vals["cargo_body_type"] = (
                    "reefer" if trailer_kind == "reefer" else "dry")
                vals["reefer_applicable"] = (
                    "yes" if trailer_kind == "reefer" else "no")
            elif trailer_kind == "flatbed":
                for field in ("cargo_body_type", "pallet_capacity",
                              "cargo_height_in", "door_width_in",
                              "door_clearance_in",
                              "reefer_temperature_range"):
                    vals[field] = False
                vals["reefer_applicable"] = "not_applicable"
            else:
                for field in cargo_fields:
                    vals[field] = False
                vals["reefer_applicable"] = "not_applicable"
        elif supplied_type in ("cargo_van", "straight"):
            for field in trailer_fields:
                vals[field] = False
            cargo_kind = vals.get("cargo_body_type") or self.cargo_body_type
            vals["reefer_applicable"] = (
                "yes" if cargo_kind == "reefer" else "not_applicable")
            if cargo_kind != "reefer":
                vals["reefer_temperature_range"] = False
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

        make = write_vals.get("truck_make") or self.truck_make
        model = write_vals.get("truck_model") or self.truck_model
        if make or model:
            put("equipment_model",
                ("%s %s" % (make or "", model or "")).strip())
        plate_no = write_vals.get("license_plate") or self.license_plate
        plate_code = write_vals.get("plate_province") or self.plate_province
        if plate_no and plate_code:
            put("truck_plate_jurisdiction", "%s / %s" % (
                plate_no, dict(PROVINCE_CHOICES).get(plate_code, plate_code)))
        if vals.get("use_contact_phone_for_whatsapp"):
            phone = write_vals.get("phone") or self.phone
            if phone:
                put("whatsapp_number", phone)

        codes = vals.get("service_province_codes") or []
        if codes:
            states = self.env["res.country.state"].search([
                ("country_id.code", "=", "CA"), ("code", "in", codes)])
            if states and set(states.ids) != set(self.service_province_ids.ids):
                write_vals["service_province_ids"] = [(6, 0, states.ids)]

        area_codes = vals.get("preferred_service_area_codes") or []
        if not area_codes and vals.get("preferred_service_area"):
            legacy_label = vals["preferred_service_area"]
            area = self.env["prema.owner.operator.service.area"].search(
                [("name", "=", legacy_label)], limit=1)
            if area:
                area_codes = [area.code]
        if area_codes:
            areas = self.env["prema.owner.operator.service.area"].search(
                [("code", "in", area_codes)])
            if areas and (not only_empty
                          or not self.preferred_service_area_ids):
                write_vals["preferred_service_area_ids"] = [
                    (6, 0, areas.ids)]
                write_vals["preferred_service_area"] = \
                    self._fields["preferred_service_area"].convert_to_cache(
                        areas[0].name, self)
                write_vals["preferred_lanes"] = ", ".join(areas.mapped("name"))

        pref_codes = vals.get("work_preference_codes") or []
        if not pref_codes and vals.get("work_preference"):
            legacy_code = vals["work_preference"]
            pref = self.env["prema.owner.operator.work.preference"].search(
                [("code", "=", legacy_code)], limit=1)
            if pref:
                pref_codes = [pref.code]
        if pref_codes:
            prefs = self.env["prema.owner.operator.work.preference"].search(
                [("code", "in", pref_codes)])
            if prefs and (not only_empty or not self.work_preference_ids):
                write_vals["work_preference_ids"] = [(6, 0, prefs.ids)]
                write_vals["work_preference"] = self._fields[
                    "work_preference"].convert_to_cache(
                        prefs[0].code, self)

        provided = dict(self.equipment_provided or {})
        provided.update(vals.get("equipment_provided") or {})
        if provided != (self.equipment_provided or {}):
            write_vals["equipment_provided"] = provided

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
            put("has_reefer", False)
            put("has_liftgate", False)
            put("equipment_type", "other")
        elif vehicle == "semi":
            put("has_reefer", trailer == "reefer")
            put("has_liftgate", liftgate == "yes")
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
        if area and not area_codes:
            put("preferred_lanes", area)
        start = write_vals.get(
            "available_start_date", self.available_start_date)
        if start:
            put("availability",
                start.isoformat() if hasattr(start, "isoformat") else str(start))
        if "cvor_number" in write_vals \
                and self.cvor_verification_status == "not_provided":
            write_vals["cvor_verification_status"] = "pending"

        if write_vals.get("whatsapp_consent_granted"):
            write_vals["whatsapp_consent_version"] = (
                self.env["ir.config_parameter"].sudo().get_param(
                    "prema_owner_operator.whatsapp_consent_version") or "1")

        if write_vals:
            self.write(write_vals)
        return bool(write_vals)

    # ------------------------------------------------------------------
    # notification preference audit
    # ------------------------------------------------------------------
    NOTIFICATION_FIELDS = {
        "load_notification_channel", "load_notification_email",
        "whatsapp_number", "whatsapp_consent_granted",
        "whatsapp_consent_text", "whatsapp_consent_version",
        "whatsapp_consent_at", "load_notification_opt_out",
        "load_notification_opt_out_at",
    }

    def write(self, vals):
        tracked = bool(self.NOTIFICATION_FIELDS & set(vals))
        before = {}
        if tracked:
            before = {
                app.id: {
                    f: app[f] for f in self.NOTIFICATION_FIELDS
                } for app in self
            }
        res = super().write(vals)
        if tracked and not self.env.context.get("oo_skip_notification_log"):
            for app in self:
                app._log_notification_change(before.get(app.id, {}))
        return res

    def _log_notification_change(self, before):
        self.ensure_one()
        after = {f: self[f] for f in self.NOTIFICATION_FIELDS}
        if before == after:
            return
        self.env["prema.owner.operator.notification.log"].sudo().create({
            "application_id": self.id,
            "user_id": self.env.user.id,
            "channel": self.load_notification_channel,
            "whatsapp_number": self.whatsapp_number,
            "email": self.load_notification_email,
            "consent_given": self.whatsapp_consent_granted,
            "consent_text": self.whatsapp_consent_text,
            "consent_version": self.whatsapp_consent_version,
            "opt_out": self.load_notification_opt_out,
            "note": _("Preference changed from public form or backend."),
        })

    # ------------------------------------------------------------------
    # stage transitions and the insurer-direct COI request
    # ------------------------------------------------------------------
    def _stage_by_name(self, name):
        self.ensure_one()
        job = self.hr_applicant_id.job_id or self._public_job()
        if not job:
            return self.env["hr.recruitment.stage"]
        return self.env["hr.recruitment.stage"].search(
            [("name", "=", name), ("job_ids", "in", job.id)], limit=1)

    def _on_stage_change(self, old_stage, new_stage):
        """Called from hr.applicant.write after a deliberate stage move.

        Entering Authority & Insurance Review sends the COI request. The later
        stages are guarded here as well as in action_advance, so a direct
        stage-bar move cannot skip the compliance review, signature or Fleet
        activation. Automated/migration moves use the skip context.
        """
        self.ensure_one()
        if self.env.context.get("owner_operator_skip_stage_hook"):
            return False
        if not new_stage:
            return False
        if old_stage and old_stage.id == new_stage.id:
            return False
        if new_stage.name == STAGE_AUTHORITY:
            if self.coi_request_sent_at:
                return False
            self._send_coi_request()
            return True
        if new_stage.name == STAGE_VERIFIED:
            if self.verification_status != "verified":
                raise UserError(_(
                    "Insurance verification must be approved before moving "
                    "this applicant to Verified."))
            self._sync_state(new_stage)
            return False
        if new_stage.name in (STAGE_CONTRACT, STAGE_SIGNED):
            self._guard_transition(new_stage)
            return False
        if new_stage.name == STAGE_APPROVED:
            if not self.fleet_vehicle_id:
                raise UserError(_(
                    "Use “Approve → Fleet” to reach Approved — Ready for "
                    "Dispatch. A stage move alone cannot activate equipment."))
            self._guard_transition(new_stage)
            return False
        return False

    def _send_coi_request(self):
        self.ensure_one()
        if not (self.business_name or "").strip():
            raise UserError(_(
                "The insured’s legal business name is missing; the COI "
                "request cannot be sent."))
        template = self.env.ref(
            "prema_owner_operator.mail_template_coi_request",
            raise_if_not_found=False)
        if not template:
            raise UserError(_("The COI request template is missing."))
        mail_id = template.sudo().send_mail(self.id, force_send=False)
        mail = self.env["mail.mail"].sudo().browse(mail_id)
        self.sudo().write({
            "coi_request_sent_at": fields.Datetime.now(),
            "coi_request_sent_by": self.env.user.id,
            "coi_request_message_id": mail.message_id or False,
            "coi_request_last_subject": mail.subject,
            "coi_request_count": (self.coi_request_count or 0) + 1,
        })
        self.message_post(
            body=_(
                "Insurer-direct COI request sent to %(email)s "
                "(subject: %(subject)s).") % {
                    "email": self.email or "",
                    "subject": mail.subject or "",
                },
            subtype_xmlid="mail.mt_note")
        return mail

    def action_resend_coi_request(self):
        self.ensure_one()
        mail = self._send_coi_request()
        self.message_post(
            body=_("COI request deliberately re-sent by %s.")
            % self.env.user.name,
            subtype_xmlid="mail.mt_note")
        return True

    # ------------------------------------------------------------------
    # insurance verification status / reviewer actions
    # ------------------------------------------------------------------
    def _is_verification_reviewer(self):
        self.ensure_one()
        user = self.env.user
        return bool(
            user.has_group("prema_owner_operator.group_owner_operator")
            or user.has_group("hr_recruitment.group_hr_recruitment_manager"))

    def _verification_review_user(self):
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        raw = icp.get_param("prema_owner_operator.verification_reviewer_id")
        if raw:
            user = self.env["res.users"].browse(int(raw)).exists()
            if user:
                return user
        if self.hr_applicant_id.user_id:
            return self.hr_applicant_id.user_id
        group = self.env.ref(
            "prema_owner_operator.group_owner_operator",
            raise_if_not_found=False)
        if group and group.users[:1]:
            return group.users[:1]
        return self.env.ref("base.user_admin", raise_if_not_found=False)

    def _ensure_verification_activity(self, reasons):
        """One open reviewer activity per application, updated repeat-safely."""
        self.ensure_one()
        user = self._verification_review_user()
        if not user:
            return False
        summary = _("COI verification review")
        note = "\n".join(reasons) if isinstance(reasons, (list, tuple)) \
            else (reasons or "")
        Activity = self.env["mail.activity"].sudo()
        activity = Activity.search([
            ("res_model", "=", "prema.owner.operator.application"),
            ("res_id", "=", self.id),
            ("summary", "=", summary),
        ], limit=1)
        if activity:
            activity.write({"note": note, "user_id": user.id})
        else:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=summary, note=note, user_id=user.id)
        return True

    def _after_coi_version(self, version):
        """Set the initial card state when a COI version is stored."""
        self.ensure_one()
        if not version.is_direct:
            reason = {
                "applicant_forwarded": _(
                    "Applicant forwarded the COI themselves; an "
                    "insurer/broker-direct certificate is required."),
                "unknown": _(
                    "Unknown or untrusted sender; manual review required."),
                "manual": _(
                    "Manually attached COI requires reviewer verification."),
            }.get(version.source, _("COI requires manual review."))
            self._set_verification_review([reason])
            return True
        cfg = version._config()
        missing = []
        if cfg["min_auto"] <= 0:
            missing.append(_(
                "Automobile liability coverage threshold is not configured."))
        if cfg["min_cargo"] <= 0:
            missing.append(_("Cargo coverage threshold is not configured."))
        if cfg["min_conf"] <= 0:
            missing.append(_("AI confidence threshold is not configured."))
        if missing:
            self._set_verification_review(missing)
            return True
        if self.verification_status != "verified":
            self.write({
                "verification_status": "pending",
                "verification_reasons": False,
            })
        return True

    def _set_verification_review(self, reasons, method=None, confidence=0.0):
        self.ensure_one()
        reasons = [r for r in reasons if r]
        text = "\n".join(reasons)
        previous = self.verification_status
        vals = {
            "verification_status": "review",
            "verification_reasons": text or _("Verification requires review."),
            "verification_method": method or self.verification_method or "ai",
            "verification_confidence": confidence
            or self.verification_confidence or 0.0,
        }
        self.write(vals)
        self._ensure_verification_activity(reasons or [_("Manual review")])
        if previous != "review":
            self.message_post(
                body=_("Insurance verification requires review:\n• %s")
                % "\n• ".join(reasons or [_("Manual review")]),
                subtype_xmlid="mail.mt_note")
        return True

    def _move_to_verified_stage(self):
        self.ensure_one()
        verified = self._stage_by_name(STAGE_VERIFIED)
        applicant = self.hr_applicant_id
        if not verified or not applicant:
            return False
        current = applicant.stage_id
        if current and current.sequence >= verified.sequence:
            return False
        applicant.sudo().with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": verified.id})
        self._sync_state(verified)
        return True

    def _set_verification_verified(self, version, method="ai",
                                   confidence=0.0, reason=None):
        self.ensure_one()
        if not version or not version.attachment_id:
            raise UserError(_("There is no COI attachment to verify."))
        already = (self.verification_status == "verified"
                   and self.coi_current_version_id == version)
        now = fields.Datetime.now()
        vals = {
            "verification_status": "verified",
            "verification_reasons": False,
            "verification_date": now,
            "verification_method": method,
            "verification_confidence": confidence or 0.0,
            "coi_current_version_id": version.id,
        }
        if method == "manual":
            vals.update({
                "verification_reviewer_id": self.env.user.id,
                "verification_reviewed_on": now,
                "verification_review_reason": reason or "",
            })
        self.write(vals)
        version.sudo().write({
            "verification_status": "verified",
            "verified_on": now,
            "verified_by": self.env.user.id if method == "manual" else False,
        })
        doc = self.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1]
        if not doc:
            doc = self.env["prema.owner.operator.document"].sudo().create({
                "application_id": self.id,
                "document_type": "coi_insurer",
                "source": "insurer",
            })
        doc.sudo().write({
            "attachment_id": version.attachment_id.id,
            "status": "verified",
            "source": "insurer",
            "received_on": doc.received_on or now,
            "verified_on": now,
            "verified_by": self.env.user.id if method == "manual" else False,
            "notes": _("Verified %(method)s on %(date)s — COI version %(version)s")
            % {
                "method": method,
                "date": now,
                "version": version.id,
            },
        })
        self._move_to_verified_stage()
        self.env["mail.activity"].sudo().search([
            ("res_model", "=", "prema.owner.operator.application"),
            ("res_id", "=", self.id),
            ("summary", "=", _("COI verification review")),
        ]).unlink()
        if not already:
            self.message_post(
                body=_(
                    "Insurance verification passed (%(method)s). COI version "
                    "%(version)s is current; policy %(policy)s, expiry %(expiry)s.") % {
                        "method": method,
                        "version": version.id,
                        "policy": version.policy_number or _("n/a"),
                        "expiry": version.expiry_date or _("n/a"),
                    },
                subtype_xmlid="mail.mt_note")
        return True

    def action_verify_licence(self):
        """Reviewer confirms licence number + expiry from the uploaded images.

        Nothing is inferred from an image; both values must be entered.
        """
        self.ensure_one()
        if not self._is_verification_reviewer():
            raise AccessError(_("Not available to this user."))
        if not (self.licence_number or "").strip():
            raise UserError(_("Enter the driver licence number first."))
        if not self.licence_expiry:
            raise UserError(_("Enter the licence expiry date first."))
        self.write({
            "licence_verification_status": "verified",
            "licence_verified_on": fields.Datetime.now(),
            "licence_verified_by": self.env.user.id,
        })
        self.message_post(
            body=_("Driver licence verified by %s.") % self.env.user.name,
            subtype_xmlid="mail.mt_note")
        return True

    def action_reset_licence_verification(self):
        self.ensure_one()
        if not self._is_verification_reviewer():
            raise AccessError(_("Not available to this user."))
        self.write({
            "licence_verification_status": "not_verified",
            "licence_verified_on": False,
            "licence_verified_by": False,
        })
        return True

    def _require_verification_reason(self):
        self.ensure_one()
        reason = (self.verification_review_reason or "").strip()
        if not reason:
            raise UserError(_(
                "Enter the reviewer reason before approving, rejecting or "
                "requesting an updated COI."))
        return reason

    def action_approve_verification(self):
        self.ensure_one()
        if not self._is_verification_reviewer():
            raise AccessError(_("Not available to this user."))
        reason = self._require_verification_reason()
        version = self.coi_current_version_id or \
            self.coi_version_ids.filtered("attachment_id")[:1]
        if not version:
            raise UserError(_("There is no COI version to approve."))
        self._set_verification_verified(
            version, method="manual",
            confidence=version.extraction_confidence or 0.0,
            reason=reason)
        return True

    def action_reject_coi(self):
        self.ensure_one()
        if not self._is_verification_reviewer():
            raise AccessError(_("Not available to this user."))
        reason = self._require_verification_reason()
        reasons = [
            _("COI rejected by reviewer: %s") % reason,
        ]
        previous = self.verification_status
        prior = (self.verification_reasons or "").strip()
        text = "\n".join(x for x in (prior, *reasons) if x)
        self.write({
            "verification_status": "review",
            "verification_reasons": text,
            "verification_reviewer_id": self.env.user.id,
            "verification_reviewed_on": fields.Datetime.now(),
            "verification_review_reason": reason,
        })
        self._ensure_verification_activity(reasons)
        if previous != "review":
            self.message_post(
                body=_("COI rejected by %(user)s: %(reason)s") % {
                    "user": self.env.user.name, "reason": reason},
                subtype_xmlid="mail.mt_note")
        return True

    def action_request_updated_coi(self):
        self.ensure_one()
        if not self._is_verification_reviewer():
            raise AccessError(_("Not available to this user."))
        reason = self._require_verification_reason()
        self.action_resend_coi_request()
        msg = _("Updated COI requested by %(user)s: %(reason)s") % {
            "user": self.env.user.name, "reason": reason}
        prior = (self.verification_reasons or "").strip()
        self.write({
            "verification_status": "review",
            "verification_reasons": "\n".join(
                x for x in (prior, msg) if x),
            "verification_reviewer_id": self.env.user.id,
            "verification_reviewed_on": fields.Datetime.now(),
            "verification_review_reason": reason,
        })
        self._ensure_verification_activity([msg])
        self.message_post(body=msg, subtype_xmlid="mail.mt_note")
        return True

    def action_open_coi_emails(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("COI Emails — %s") % (self.name or ""),
            "res_model": "prema.owner.operator.verification",
            "view_mode": "list,form",
            "domain": [("matched_application_id", "=", self.id)],
            "context": {"default_matched_application_id": self.id},
            "target": "current",
        }

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
        """Refuse to advance while the target stage's requirements are unmet.

        Stage NAMES are the stable key: the pipeline was renumbered when
        Equipment Verification and Initial Qualification left job 5, so a
        sequence-number gate would silently skip checks.
        """
        self.ensure_one()
        name = target.name
        problems = []
        if name == STAGE_AUTHORITY:
            if self._blocking("initial"):
                problems.append(_("initial uploads: %s")
                                % self._label(self._blocking("initial")))
        if name == STAGE_VERIFIED:
            if self.verification_status != "verified":
                problems.append(_("approved insurance verification"))
        if name in (STAGE_CONTRACT, STAGE_SIGNED, STAGE_APPROVED):
            missing_initial = self._verified_missing("initial")
            missing_qual = self._verified_missing("qualification")
            if missing_initial:
                problems.append(_("verified initial documents: %s")
                                % self._label(missing_initial))
            if missing_qual:
                problems.append(_("verified qualification documents: %s")
                                % self._label(missing_qual))
            if not (self.licence_class and self.licence_expiry):
                problems.append(_("driver licence class and expiry"))
            if not self.licence_current:
                problems.append(_(
                    "a CURRENT driver licence (expiry has passed)"))
            if not self.operator_id or not self.operator_id.compliance_ok:
                problems.append(_(
                    "a completed compliance review: operator authority "
                    "verified and insurer-direct COI checked with coverage "
                    "dates"))
        if name in (STAGE_SIGNED, STAGE_APPROVED):
            if self._blocking("contract") or not self.contract_signed_on:
                problems.append(_("a completed signature"))
        if name == STAGE_APPROVED:
            unverified = []
            for bucket in ("initial", "qualification", "contract"):
                for doc_type in self._verified_missing(bucket):
                    if doc_type not in unverified:
                        unverified.append(doc_type)
            if unverified:
                problems.append(_("reviewed documents: %s")
                                % self._label(unverified))
            if not self._vin_clear():
                problems.append(_("VIN conflict resolution"))
            if not self.equipment_complete:
                problems.append(_("dispatch-critical equipment data: %s")
                                % (self.equipment_missing or ""))
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
            if target.name == STAGE_APPROVED:
                # Final activation is the Fleet hand-off, not a bare stage
                # move. action_approve sets the stage itself.
                app.action_approve()
                continue
            app.hr_applicant_id.sudo().write({"stage_id": target.id})
            app._sync_state(target)
        return True

    def _sync_state(self, stage):
        self.ensure_one()
        mapping = {
            STAGE_NEW: "submitted",
            STAGE_AUTHORITY: "authority_review",
            STAGE_VERIFIED: "verified",
            STAGE_CONTRACT: "contract",
            STAGE_SIGNED: "signed",
            STAGE_APPROVED: "approved",
        }
        state = mapping.get(stage.name)
        if state is None:
            seq = stage.sequence
            state = "submitted"
            if seq >= 1:
                state = "qualifying"
            if seq >= 2:
                state = "verified"
            if seq >= 3:
                state = "contract"
            if seq >= 4:
                state = "signed"
            if seq >= 5:
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

    def _completed_signature_attachment(self):
        """The genuine completed PDF, or an empty recordset."""
        self.ensure_one()
        request = self.sign_request_id
        if not request:
            return self.env["ir.attachment"]
        attachment = request.completed_document_attachment_ids[:1]
        if attachment:
            return attachment
        if request.completed_document:
            return self.env["ir.attachment"].sudo().create({
                "name": "%s-signed.pdf" % (self.name or "agreement"),
                "datas": request.completed_document,
                "mimetype": "application/pdf",
                "res_model": "sign.request",
                "res_id": request.id,
            })
        return self.env["ir.attachment"]

    def _signature_is_genuine_and_complete(self):
        self.ensure_one()
        request = self.sign_request_id
        if not request or request.state != "signed" or request.nb_wait:
            return False
        items = request.request_item_ids
        if not items or any(item.state != "completed" for item in items):
            return False
        # The application's own contact must be one of the required signers.
        if self.partner_id and self.partner_id not in items.partner_id:
            return False
        if not self._completed_signature_attachment():
            return False
        return True

    def _move_to_signed_stage(self):
        self.ensure_one()
        signed = self._stage_by_name(STAGE_SIGNED)
        applicant = self.hr_applicant_id
        if not signed or not applicant:
            return False
        current = applicant.stage_id
        if current and current.sequence >= signed.sequence:
            return False
        applicant.sudo().with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": signed.id})
        self._sync_state(signed)
        return True

    def _apply_home_base_tags(self):
        """Append city + canonical province tags, preserving all others.

        Structured home city/province remain the source of truth; tags are a
        filter aid only and never a distance calculation.
        """
        self.ensure_one()
        city = re.sub(r"\s+", " ", (self.home_city or "")).strip()
        province = re.sub(r"\s+", " ", (self.home_province or "")).strip()
        applicant = self.hr_applicant_id
        if not applicant:
            return False
        if not city or not province:
            issue = _(
                "Home-base tags not applied: the structured home city and "
                "province are incomplete. Correct them; do not invent a "
                "location.")
            self.write({"home_base_tag_issue": issue})
            self.message_post(body=issue, subtype_xmlid="mail.mt_note")
            return False
        code = province.upper()
        if code not in PROVINCES:
            code = next(
                (key for key, name in PROVINCES.items()
                 if name.lower() == province.lower()), None)
        if not code:
            issue = _(
                "Home-base tags not applied: “%s” is not a recognised "
                "Canadian province or territory.") % province
            self.write({"home_base_tag_issue": issue})
            self.message_post(body=issue, subtype_xmlid="mail.mt_note")
            return False
        Category = self.env["hr.applicant.category"].sudo()

        def tag_for(value):
            found = Category.search([("name", "=ilike", value)], limit=1)
            return found or Category.create({"name": value})

        city_tag = tag_for(city.title())
        province_tag = tag_for(code)
        applicant.sudo().write({"categ_ids": [
            (4, city_tag.id), (4, province_tag.id)]})
        self.write({"home_base_tag_issue": False})
        self.message_post(
            body=_("Home-base tags applied: %(city)s, %(province)s.")
            % {"city": city_tag.name, "province": province_tag.name},
            subtype_xmlid="mail.mt_note")
        return True

    def _send_account_setup_email(self):
        self.ensure_one()
        if self.setup_email_sent_at:
            return False
        template = self.env.ref(
            "prema_owner_operator.mail_template_account_setup",
            raise_if_not_found=False)
        if not template:
            return False
        mail_id = template.sudo().send_mail(self.id, force_send=False)
        mail = self.env["mail.mail"].sudo().browse(mail_id)
        self.write({
            "setup_email_sent_at": fields.Datetime.now(),
            "setup_email_message_id": mail.message_id or False,
        })
        self.message_post(
            body=_("Account-setup acknowledgement sent."),
            subtype_xmlid="mail.mt_note")
        return True

    @api.model
    def _cron_sync_signatures(self):
        """Advance genuine, fully-completed Sign requests exactly once.

        Everything is verified from the Sign request itself: fully-signed
        state, no waiting signer, every item completed, the application
        contact among the required signers, and a completed PDF. Duplicate
        completion events are absorbed by contract_signed_on.
        """
        for app in self.search([("sign_request_id", "!=", False),
                                ("contract_signed_on", "=", False)]):
            if not app._signature_is_genuine_and_complete():
                continue
            attachment = app._completed_signature_attachment()
            now = fields.Datetime.now()
            doc = app.document_ids.filtered(
                lambda d: d.document_type == "signed_agreement")[:1]
            if not doc:
                doc = self.env["prema.owner.operator.document"].sudo().create({
                    "application_id": app.id,
                    "document_type": "signed_agreement",
                    "source": "private",
                })
            if doc:
                doc.sudo().write({
                    "status": "received",
                    "source": "private",
                    "attachment_id": attachment.id,
                    "received_on": now,
                    "notes": _("Completed via Odoo Sign request %s.")
                    % app.sign_request_id.id,
                })
            app.write({
                "contract_signed_on": now,
                "contract_completed_on": now,
            })
            app._move_to_signed_stage()
            app._apply_home_base_tags()
            app._send_account_setup_email()
            app.message_post(
                body=_("Odoo Sign request %s completed; signed PDF linked.")
                % app.sign_request_id.id,
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

    # ------------------------------------------------------------------
    # approval → driver contact → documents → Fleet
    # ------------------------------------------------------------------
    CATEGORY_BY_TYPE = {
        "cargo_van": "Cargo Van",
        "straight": "Straight Truck",
        "tractor": "Tractor Truck",
        "semi": "Semi-Truck",
    }
    DRIVER_DOC_TYPES = ("licence_front", "licence_back", "signed_agreement")

    def _home_province_code(self):
        self.ensure_one()
        if self.home_province_code:
            return self.home_province_code
        raw = (self.home_province or "").strip()
        if raw.upper() in PROVINCES:
            return raw.upper()
        for code, name in PROVINCES.items():
            if raw.lower() == name.lower():
                return code
        return False

    def _canadian_state(self):
        self.ensure_one()
        code = self._home_province_code()
        if not code:
            return self.env["res.country.state"]
        return self.env["res.country.state"].sudo().search([
            ("country_id.code", "=", "CA"),
            ("code", "=", code),
        ], limit=1)

    def _driver_partner_vals(self):
        self.ensure_one()
        state = self._canadian_state()
        country = self.env["res.country"].sudo().search(
            [("code", "=", "CA")], limit=1)
        phone = self.phone or False
        mobile = self.whatsapp_number or (
            phone if self.use_contact_phone_for_whatsapp else False)
        vals = {
            "name": self.contact_name or self.business_name or self.name,
            "is_company": False,
            "type": "contact",
            "parent_id": self.partner_id.id or False,
            "ref": self.name,
            "email": self.email or False,
            "phone": phone,
            "mobile": mobile,
            "function": _("Driver"),
            "city": self.home_city or False,
            "state_id": state.id if state else False,
            "country_id": country.id if country else False,
            "oo_is_driver": True,
            "oo_application_id": self.id,
        }
        # Licence details only once a reviewer has actually verified them.
        if self.licence_verification_status == "verified":
            vals.update({
                "oo_licence_class": self.licence_class or False,
                "oo_licence_province": self.licence_province
                or self._home_province_code(),
                "oo_licence_expiry": self.licence_expiry or False,
                "oo_licence_number": self.licence_number or False,
                "oo_licence_verified": True,
                "oo_licence_verified_on":
                    self.licence_verified_on or fields.Datetime.now(),
                "oo_licence_verified_by":
                    self.licence_verified_by.id or self.env.user.id,
            })
        return vals

    def _ensure_driver_partner(self):
        """Create or update the SEPARATE individual driver contact."""
        self.ensure_one()
        driver = self.driver_partner_id.exists()
        if not driver:
            driver = self.env["res.partner"].sudo().search([
                ("oo_application_id", "=", self.id),
                ("oo_is_driver", "=", True),
            ], limit=1)
        if not driver:
            driver = self.env["res.partner"].sudo().search([
                ("ref", "=", self.name),
                ("oo_is_driver", "=", True),
            ], limit=1)
        vals = self._driver_partner_vals()
        if driver:
            driver.write(vals)
        else:
            driver = self.env["res.partner"].sudo().create(vals)
        self.driver_partner_id = driver.id
        return driver

    def _documents_folder(self):
        self.ensure_one()
        if self.documents_folder_id:
            return self.documents_folder_id
        Documents = self.env["documents.document"].sudo()
        parent = Documents.search([
            ("type", "=", "folder"),
            ("name", "=", "Fleet"),
            ("folder_id", "=", False),
        ], limit=1)
        folder = Documents.create({
            "name": _("Owner-Operator %s") % self.name,
            "type": "folder",
            "folder_id": parent.id or False,
            "owner_id": self.env.user.id,
            "access_internal": "view",
            "access_via_link": "none",
            "oo_application_id": self.id,
        })
        self.documents_folder_id = folder.id
        return folder

    def _vehicle_category(self):
        self.ensure_one()
        name = self.CATEGORY_BY_TYPE.get(
            self.vehicle_type, "Owner-operator equipment")
        Category = self.env["fleet.vehicle.model.category"].sudo()
        return Category.search([("name", "=", name)], limit=1) \
            or Category.create({"name": name})

    def _vehicle_vals(self, driver, update=False):
        """The single source → Fleet mapping table (see FLEET_FIELD_MAP)."""
        self.ensure_one()
        model = self._resolve_vehicle_model()
        category = self._vehicle_category()
        code = self._home_province_code()
        home_label = ", ".join(p for p in (self.home_city, code) if p) or False
        vals = {
            "model_id": model.id,
            "name": "%s — %s" % (
                self.equipment_model or _("Owner-operator equipment"),
                self.license_plate or self.unit_number or self.name),
            "category_id": category.id,
            "driver_id": driver.id,
            "x_current_driver_contact_id": driver.id,
            "operator_id": self.operator_id.id,
            "application_id": self.id,
            "company_id": self.company_id.id,
            "license_plate": (self.license_plate or "").upper() or False,
            "vin_sn": self.vin or False,
            "model_year": self.truck_year or False,
            "oo_plate_province": self.plate_province or False,
            "oo_vehicle_type": self.vehicle_type or False,
            "oo_home_province_code": code or False,
            "oo_home_place_id": self.home_city_place_id or False,
            "home_city": self.home_city or False,
            "home_province": self.home_province or False,
            "home_latitude": self.home_latitude or False,
            "home_longitude": self.home_longitude or False,
            "home_location": home_label,
            "x_home_base_address": home_label,
            "x_home_base_lat": self.home_latitude or False,
            "x_home_base_lng": self.home_longitude or False,
            "x_air_ride": self.has_air_ride,
            "x_current_odometer_km": self.odometer_km or 0.0,
            "x_gvwr_lbs": self.gvwr_lbs or 0.0,
            "x_studio_gvwr_lbs": int(self.gvwr_lbs or 0),
            "x_max_payload_lbs": self.payload_lbs or 0.0,
            "payload_capacity_lbs": self.payload_lbs or 0.0,
            "x_max_pallets": self.pallet_capacity or 0,
            "x_cargo_box_length_ft": self.box_length_ft or 0.0,
            # despite the _ft name, this field stores INCHES
            "x_box_interior_height_ft": self.cargo_height_in or 0.0,
            "x_overall_length_ft": self.overall_length_ft or 0.0,
            "x_vehicle_height_ft": self.vehicle_height_ft or 0.0,
            "x_door_clearance_in": self.door_clearance_in or 0.0,
            "x_dock_height_in": self.dock_height_in or 0.0,
            "oo_cargo_body_type": self.cargo_body_type or False,
            "oo_cargo_width_in": self.cargo_width_in or 0.0,
            "oo_reefer_temperature_range":
                self.reefer_temperature_range or False,
            "x_reefer": self.has_reefer,
            "x_liftgate": self.has_liftgate,
        }
        if self.dock_height_capable:
            vals["x_studio_dock_height"] = self.dock_height_capable == "yes"
        transmission = {
            "automatic": "automatic",
            "automated_manual": "automatic",
            "manual": "manual",
        }.get(self.transmission)
        if transmission:
            vals["transmission"] = transmission
        if self.vehicle_type == "tractor":
            # Tractor only: no trailer, cargo body, capacity or reefer.
            for field in (
                    "x_max_pallets", "x_max_payload_lbs",
                    "payload_capacity_lbs", "x_cargo_box_length_ft",
                    "x_box_interior_height_ft", "x_overall_length_ft",
                    "x_vehicle_height_ft", "x_door_clearance_in",
                    "x_dock_height_in", "oo_cargo_body_type",
                    "oo_cargo_width_in", "oo_reefer_temperature_range",
                    "x_studio_dock_height"):
                vals.pop(field, None)
            vals.update({"x_reefer": False, "x_liftgate": False})
        elif self.vehicle_type == "semi":
            vals.update({
                "oo_trailer_type": self.trailer_type or False,
                "oo_trailer_year": self.trailer_year or False,
                "oo_trailer_unit": self.trailer_unit or False,
                "oo_trailer_length_ft": self.trailer_length_ft or 0.0,
                "x_reefer": self.trailer_type == "reefer",
            })
        else:
            for field in ("oo_trailer_type", "oo_trailer_year",
                          "oo_trailer_unit", "oo_trailer_length_ft"):
                vals.pop(field, None)
        if not update:
            ready = self.env["fleet.vehicle.state"].sudo().search(
                [("name", "=", "Ready")], limit=1)
            vals["state_id"] = ready.id or False
            vals["x_operational_logistics"] = True
        return vals

    def _generated_tag_names(self):
        self.ensure_one()
        labels = dict(VEHICLE_TYPES)
        names = ["Type: %s" % labels.get(self.vehicle_type, self.vehicle_type)]
        if self.cargo_body_type:
            names.append("Body: %s" % dict(CARGO_BODIES).get(
                self.cargo_body_type, self.cargo_body_type))
        if self.box_length_ft:
            names.append("Length: %g ft" % self.box_length_ft)
        if self.home_city:
            names.append("Home: %s" % self.home_city)
        code = self._home_province_code()
        if code:
            names.append("Province: %s" % code)
        if self.vehicle_type == "semi" and self.trailer_type:
            names.append("Trailer: %s" % dict(TRAILER_TYPES).get(
                self.trailer_type, self.trailer_type))
        if self.has_reefer:
            names.append("Reefer: Yes")
        if self.has_liftgate:
            names.append("Liftgate: Yes")
        if self.has_air_ride:
            names.append("Air Ride: Yes")
        if self.gvwr_lbs:
            names.append("GVWR: %s lb" % format(int(self.gvwr_lbs), ","))
        return names

    def _apply_generated_tags(self, vehicle):
        """Repeat-safe tags. Only this module's tags are ever removed."""
        self.ensure_one()
        Tag = self.env["fleet.vehicle.tag"].sudo()
        desired = Tag
        for name in self._generated_tag_names():
            tag = Tag.search([("name", "=", name)], limit=1) \
                or Tag.create({"name": name})
            desired |= tag
        manual = vehicle.tag_ids - self.generated_tag_ids
        generated = desired - manual
        vehicle.write({"tag_ids": [(6, 0, (manual | generated).ids)]})
        self.generated_tag_ids = [(6, 0, generated.ids)]

    def _front_photo_payload(self):
        self.ensure_one()
        doc = self.document_ids.filtered(
            lambda d: d.document_type == "photo_front"
            and d.attachment_id)[:1]
        if not doc:
            return None
        payload = doc.attachment_id.raw or b""
        if not payload or len(payload) > 5 * 1024 * 1024:
            return None
        if payload.startswith(b"\xff\xd8\xff") \
                or payload.startswith(b"\x89PNG\r\n\x1a\n") \
                or (payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"):
            return payload
        return None

    def _apply_front_photo(self, vehicle):
        if vehicle.image_1920:
            return False
        payload = self._front_photo_payload()
        if not payload:
            return False
        vehicle.write({"image_1920": base64.b64encode(payload)})
        return True

    def _signed_agreement_document(self):
        self.ensure_one()
        doc = self.document_ids.filtered(
            lambda d: d.document_type == "signed_agreement"
            and d.attachment_id)[:1]
        request = self.sign_request_id
        if not doc or not self.contract_signed_on or not request:
            return self.env["prema.owner.operator.document"]
        if request.state != "signed":
            return self.env["prema.owner.operator.document"]
        if not all(item.state == "completed"
                   for item in request.request_item_ids):
            return self.env["prema.owner.operator.document"]
        return doc

    def _ensure_document(self, folder, role, partner, target_model, target_id,
                         source_doc=None, coi_version=None):
        self.ensure_one()
        attachment = (coi_version.attachment_id if coi_version
                      else source_doc.attachment_id)
        domain = [
            ("oo_application_id", "=", self.id),
            ("oo_role", "=", role),
        ]
        if coi_version:
            domain.append(("oo_coi_version_id", "=", coi_version.id))
        else:
            domain.append(("oo_source_document_id", "=", source_doc.id))
        existing = self.env["documents.document"].sudo().search(
            domain, limit=1)
        if existing:
            existing.write({
                "res_model": target_model,
                "res_id": target_id,
                "partner_id": partner.id or False,
            })
            return existing
        copy = self.env["ir.attachment"].sudo().create({
            "name": attachment.name or "document",
            "raw": attachment.raw or b"",
            "mimetype": attachment.mimetype,
            "public": False,
        })
        return self.env["documents.document"].sudo().create({
            "name": attachment.name or _("Document"),
            "type": "binary",
            "attachment_id": copy.id,
            "folder_id": folder.id,
            "owner_id": self.env.user.id,
            "partner_id": partner.id or False,
            "res_model": target_model,
            "res_id": target_id,
            "company_id": self.company_id.id,
            "access_internal": "view",
            "access_via_link": "none",
            "oo_application_id": self.id,
            "oo_role": role,
            "oo_source_document_id": source_doc.id if source_doc else False,
            "oo_coi_version_id": coi_version.id if coi_version else False,
            "oo_source_attachment_id": attachment.id,
            "oo_source_checksum": attachment.checksum or False,
        })

    def _ensure_approval_documents(self, driver, vehicle):
        self.ensure_one()
        folder = self._documents_folder()
        signed = self._signed_agreement_document()
        for doc in self.document_ids.filtered(lambda d: d.attachment_id):
            if doc.document_type == "coi_insurer" and self.coi_version_ids:
                continue
            self._ensure_document(
                folder, "vehicle", self.partner_id, "fleet.vehicle",
                vehicle.id, doc)
            if (doc.document_type in ("licence_front", "licence_back")
                    or (signed and doc == signed)):
                self._ensure_document(
                    folder, "driver", driver, "res.partner", driver.id, doc)
        for version in self.coi_version_ids.filtered(
                lambda v: v.attachment_id):
            self._ensure_document(
                folder, "vehicle", self.partner_id, "fleet.vehicle",
                vehicle.id, coi_version=version)
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

    def _operator_vals(self):
        self.ensure_one()
        return {
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
        }

    def _open_operator(self, operator):
        return {
            "type": "ir.actions.act_window",
            "name": _("Operator profile"),
            "res_model": "prema.owner.operator",
            "res_id": operator.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_prepare_operator(self):
        """Create/link the compliance profile WITHOUT approving dispatch.

        The compliance review needs a place to verify authority and the
        insurer-direct COI. Before this action the application's operator link
        was read-only and the profile was only created by approval itself,
        which left the approval gate unreachable. This creates or links the
        profile and opens it; it does not verify anything, create a vehicle or
        change the stage.
        """
        self.ensure_one()
        if self.operator_id:
            return self._open_operator(self.operator_id)
        if not self.partner_id:
            raise UserError(_("The application has no contact to link."))
        Operator = self.env["prema.owner.operator"].sudo()
        operator = Operator.search(
            [("partner_id", "=", self.partner_id.id)], limit=1)
        if not operator:
            operator = Operator.create(self._operator_vals())
        self.write({"operator_id": operator.id})
        self.message_post(
            body=_("Compliance profile created/linked for review."),
            subtype_xmlid="mail.mt_note")
        return self._open_operator(operator)

    def action_approve(self):
        """Create/link the operator profile and its fleet.vehicle.

        Idempotent: a second call returns the SAME vehicle because the link
        is stored on this application. A VIN/ownership conflict raises for
        manual review instead of merging records.
        """
        self.ensure_one()
        if self.fleet_vehicle_id:
            driver = self._ensure_driver_partner()
            vehicle = self.fleet_vehicle_id
            vehicle.write(self._vehicle_vals(driver, update=True))
            self._apply_generated_tags(vehicle)
            self._apply_front_photo(vehicle)
            self._ensure_approval_documents(driver, vehicle)
            approved = self._stage_by_name(STAGE_APPROVED)
            if approved and self.hr_applicant_id \
                    and self.hr_applicant_id.stage_id.id != approved.id:
                self.hr_applicant_id.sudo().with_context(
                    owner_operator_skip_stage_hook=True).write(
                        {"stage_id": approved.id})
                self._sync_state(approved)
            return vehicle
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
            operator = Operator.create(self._operator_vals())
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

        driver = self._ensure_driver_partner()
        vehicle_vals = self._vehicle_vals(driver)
        vehicle = self.env["fleet.vehicle"].sudo().create(vehicle_vals)
        # Unit number: reuse the Studio field the fleet already uses; never
        # create a second column for the same thing.
        if self.unit_number and "x_studio_unit_number" in self.env[
                "fleet.vehicle"]._fields:
            vehicle.write({"x_studio_unit_number": self.unit_number})
        self._apply_front_photo(vehicle)
        self._apply_generated_tags(vehicle)
        self._ensure_approval_documents(driver, vehicle)
        self.write({"fleet_vehicle_id": vehicle.id,
                    "approved_on": fields.Datetime.now(),
                    "approved_by": self.env.user.id,
                    "state": "approved"})
        approved = self._stage_by_name(STAGE_APPROVED)
        if approved and self.hr_applicant_id:
            self.hr_applicant_id.sudo().with_context(
                owner_operator_skip_stage_hook=True).write(
                    {"stage_id": approved.id})
            self._sync_state(approved)
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

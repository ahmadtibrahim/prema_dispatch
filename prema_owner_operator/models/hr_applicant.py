# -*- coding: utf-8 -*-
"""hr.applicant ↔ owner-operator application.

An application is created automatically for the Owner Operator job so that an
emailed CV (which threads into the standard applicant) still gets a checklist
and an acknowledgement. Creation is idempotent: an applicant that already has
an application is never given a second one.
"""
import re

from odoo import _, api, fields, models


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    owner_operator_application_ids = fields.One2many(
        "prema.owner.operator.application", "hr_applicant_id",
        string="Owner-operator application")
    owner_operator_application_id = fields.Many2one(
        "prema.owner.operator.application", compute="_compute_owner_operator_app",
        store=True, string="Owner-operator application")
    owner_operator_document_ids = fields.One2many(
        related="owner_operator_application_id.document_ids",
        string="Owner-operator documents", readonly=True)
    oo_service_provinces = fields.Char(
        string="Provinces / territories served",
        compute="_compute_oo_service_provinces")

    # Read-only mirrors of the ONE application record. They are related
    # fields, not a second copy of the data.
    oo_business_name = fields.Char(related="owner_operator_application_id.business_name", readonly=True)
    oo_business_structure = fields.Selection(related="owner_operator_application_id.business_structure", readonly=True)
    oo_home_city = fields.Char(related="owner_operator_application_id.home_city", readonly=True)
    oo_home_province = fields.Char(related="owner_operator_application_id.home_province", readonly=True)
    oo_licence_class = fields.Char(related="owner_operator_application_id.licence_class", readonly=True)
    oo_licence_jurisdiction = fields.Char(related="owner_operator_application_id.licence_jurisdiction", readonly=True)
    oo_licence_expiry = fields.Date(related="owner_operator_application_id.licence_expiry", readonly=True)
    oo_licence_suspension = fields.Selection(related="owner_operator_application_id.licence_suspension", readonly=True)
    oo_commercial_driving_experience = fields.Selection(related="owner_operator_application_id.commercial_driving_experience", readonly=True)
    oo_owner_operator_experience = fields.Selection(related="owner_operator_application_id.owner_operator_experience", readonly=True)
    oo_tdg_training = fields.Selection(related="owner_operator_application_id.tdg_training", readonly=True)
    oo_tdg_status = fields.Selection(
        related="owner_operator_application_id.tdg_status", readonly=True)
    oo_tdg_class_ids = fields.Many2many(
        related="owner_operator_application_id.tdg_class_ids", readonly=True)
    oo_tdg_special_training = fields.Text(
        related="owner_operator_application_id.tdg_special_training",
        readonly=True)
    oo_tdg_certificate_result = fields.Selection(
        related="owner_operator_application_id.tdg_certificate_result",
        readonly=True)
    oo_recent_collisions = fields.Selection(related="owner_operator_application_id.recent_collisions", readonly=True)
    oo_recent_convictions = fields.Selection(related="owner_operator_application_id.recent_convictions", readonly=True)
    oo_recent_driving_work = fields.Text(related="owner_operator_application_id.recent_driving_work", readonly=True)
    oo_vehicle_type = fields.Selection(related="owner_operator_application_id.vehicle_type", readonly=True)
    oo_equipment_model = fields.Char(related="owner_operator_application_id.equipment_model", readonly=True)
    oo_truck_year = fields.Char(related="owner_operator_application_id.truck_year", readonly=True)
    oo_unit_number = fields.Char(related="owner_operator_application_id.unit_number", readonly=True)
    oo_vehicle_ownership = fields.Selection(related="owner_operator_application_id.vehicle_ownership", readonly=True)
    oo_transmission = fields.Selection(related="owner_operator_application_id.transmission", readonly=True)
    oo_odometer_km = fields.Float(related="owner_operator_application_id.odometer_km", readonly=True)
    oo_truck_plate_jurisdiction = fields.Char(related="owner_operator_application_id.truck_plate_jurisdiction", readonly=True)
    oo_cargo_body_type = fields.Selection(related="owner_operator_application_id.cargo_body_type", readonly=True)
    oo_payload_lbs = fields.Float(related="owner_operator_application_id.payload_lbs", readonly=True)
    oo_pallet_capacity = fields.Integer(related="owner_operator_application_id.pallet_capacity", readonly=True)
    oo_box_length_ft = fields.Float(related="owner_operator_application_id.box_length_ft", readonly=True)
    oo_cargo_width_in = fields.Float(related="owner_operator_application_id.cargo_width_in", readonly=True)
    oo_cargo_height_in = fields.Float(related="owner_operator_application_id.cargo_height_in", readonly=True)
    oo_door_width_in = fields.Float(related="owner_operator_application_id.door_width_in", readonly=True)
    oo_door_clearance_in = fields.Float(related="owner_operator_application_id.door_clearance_in", readonly=True)
    oo_liftgate_platform_length_in = fields.Float(related="owner_operator_application_id.liftgate_platform_length_in", readonly=True)
    oo_liftgate_platform_width_in = fields.Float(related="owner_operator_application_id.liftgate_platform_width_in", readonly=True)
    oo_liftgate = fields.Selection(related="owner_operator_application_id.liftgate", readonly=True)
    oo_liftgate_capacity_lb = fields.Float(related="owner_operator_application_id.liftgate_capacity_lb", readonly=True)
    oo_dock_height_capable = fields.Selection(related="owner_operator_application_id.dock_height_capable", readonly=True)
    oo_air_ride = fields.Selection(related="owner_operator_application_id.air_ride", readonly=True)
    oo_reefer_temperature_range = fields.Char(related="owner_operator_application_id.reefer_temperature_range", readonly=True)
    oo_trailer_ownership = fields.Selection(related="owner_operator_application_id.trailer_ownership", readonly=True)
    oo_trailer_type = fields.Selection(related="owner_operator_application_id.trailer_type", readonly=True)
    oo_trailer_length_ft = fields.Float(related="owner_operator_application_id.trailer_length_ft", readonly=True)
    oo_trailer_unit = fields.Char(related="owner_operator_application_id.trailer_unit", readonly=True)
    oo_truck_make = fields.Char(
        related="owner_operator_application_id.truck_make", readonly=True)
    oo_truck_model = fields.Char(
        related="owner_operator_application_id.truck_model", readonly=True)
    oo_plate_province = fields.Selection(
        related="owner_operator_application_id.plate_province",
        readonly=True)
    oo_licence_province = fields.Selection(
        related="owner_operator_application_id.licence_province",
        readonly=True)
    oo_gvwr_lbs = fields.Float(
        related="owner_operator_application_id.gvwr_lbs", readonly=True)
    oo_trailer_year = fields.Char(
        related="owner_operator_application_id.trailer_year", readonly=True)
    oo_reefer_applicable = fields.Selection(
        related="owner_operator_application_id.reefer_applicable",
        readonly=True)
    oo_home_province_code = fields.Selection(
        related="owner_operator_application_id.home_province_code",
        readonly=True)
    oo_home_city_place_id = fields.Char(
        related="owner_operator_application_id.home_city_place_id",
        readonly=True)
    oo_home_latitude = fields.Float(
        related="owner_operator_application_id.home_latitude", readonly=True)
    oo_home_longitude = fields.Float(
        related="owner_operator_application_id.home_longitude", readonly=True)
    oo_home_location_verified = fields.Boolean(
        related="owner_operator_application_id.home_location_verified",
        readonly=True)
    oo_home_location_review = fields.Boolean(
        related="owner_operator_application_id.home_location_review",
        readonly=True)
    oo_home_location_review_reason = fields.Char(
        related="owner_operator_application_id.home_location_review_reason",
        readonly=True)
    oo_use_contact_phone_for_whatsapp = fields.Boolean(
        related="owner_operator_application_id.use_contact_phone_for_whatsapp",
        readonly=True)
    oo_preferred_service_area = fields.Selection(related="owner_operator_application_id.preferred_service_area", readonly=True)
    oo_preferred_service_areas = fields.Many2many(
        related="owner_operator_application_id.preferred_service_area_ids",
        readonly=True)
    oo_available_start_date = fields.Date(related="owner_operator_application_id.available_start_date", readonly=True)
    oo_work_preference = fields.Selection(related="owner_operator_application_id.work_preference", readonly=True)
    oo_work_preferences = fields.Many2many(
        related="owner_operator_application_id.work_preference_ids",
        readonly=True)
    oo_whatsapp_consent = fields.Selection(related="owner_operator_application_id.whatsapp_consent", readonly=True)
    oo_load_notification_channel = fields.Selection(
        related="owner_operator_application_id.load_notification_channel",
        readonly=True)
    oo_load_notification_email = fields.Char(
        related="owner_operator_application_id.load_notification_email",
        readonly=True)
    oo_whatsapp_number = fields.Char(
        related="owner_operator_application_id.whatsapp_number", readonly=True)
    oo_whatsapp_consent_granted = fields.Boolean(
        related="owner_operator_application_id.whatsapp_consent_granted",
        readonly=True)
    oo_whatsapp_consent_at = fields.Datetime(
        related="owner_operator_application_id.whatsapp_consent_at",
        readonly=True)
    oo_whatsapp_consent_version = fields.Char(
        related="owner_operator_application_id.whatsapp_consent_version",
        readonly=True)
    oo_load_notification_opt_out = fields.Boolean(
        related="owner_operator_application_id.load_notification_opt_out",
        readonly=True)
    oo_equipment_complete = fields.Boolean(
        related="owner_operator_application_id.equipment_complete",
        readonly=True)
    oo_equipment_missing = fields.Text(
        related="owner_operator_application_id.equipment_missing",
        readonly=True)
    oo_coi_request_sent_at = fields.Datetime(
        related="owner_operator_application_id.coi_request_sent_at",
        readonly=True)
    oo_coi_request_count = fields.Integer(
        related="owner_operator_application_id.coi_request_count",
        readonly=True)
    oo_verification_status = fields.Selection(
        related="owner_operator_application_id.verification_status",
        readonly=True)
    oo_verification_reasons = fields.Text(
        related="owner_operator_application_id.verification_reasons",
        readonly=True)
    oo_coi_email_count = fields.Integer(
        related="owner_operator_application_id.coi_email_count",
        readonly=True)
    oo_setup_email_sent_at = fields.Datetime(
        related="owner_operator_application_id.setup_email_sent_at",
        readonly=True)
    oo_home_base_tag_issue = fields.Text(
        related="owner_operator_application_id.home_base_tag_issue",
        readonly=True)
    oo_application_declaration = fields.Boolean(related="owner_operator_application_id.application_declaration", readonly=True)
    oo_additional_notes = fields.Html(related="owner_operator_application_id.additional_notes", readonly=True)
    oo_cvor_number = fields.Char(related="owner_operator_application_id.cvor_number", readonly=True)
    oo_cvor_verification_status = fields.Selection(related="owner_operator_application_id.cvor_verification_status", readonly=True)
    oo_cvor_extraction_status = fields.Selection(
        related="owner_operator_application_id.cvor_extraction_status",
        readonly=True)
    oo_cvor_proposed_number = fields.Char(
        related="owner_operator_application_id.cvor_proposed_number",
        readonly=True)
    oo_insurance_agent_name = fields.Char(related="owner_operator_application_id.insurance_agent_name", readonly=True)
    oo_insurance_agent_email = fields.Char(related="owner_operator_application_id.insurance_agent_email", readonly=True)
    oo_licence_current = fields.Boolean(related="owner_operator_application_id.licence_current", readonly=True)
    oo_missing_verification = fields.Text(related="owner_operator_application_id.missing_verification", readonly=True)
    oo_sign_request_id = fields.Many2one("sign.request", related="owner_operator_application_id.sign_request_id", readonly=True)
    oo_contract_signed_on = fields.Datetime(related="owner_operator_application_id.contract_signed_on", readonly=True)
    oo_operator_id = fields.Many2one("prema.owner.operator", related="owner_operator_application_id.operator_id", readonly=True)
    oo_fleet_vehicle_id = fields.Many2one("fleet.vehicle", related="owner_operator_application_id.fleet_vehicle_id", readonly=True)
    oo_approved_on = fields.Datetime(related="owner_operator_application_id.approved_on", readonly=True)

    @api.model
    def _owner_operator_group_job_id(self, domain):
        """The job selected by the current kanban action/context, if it is ours.

        Odoo's stock group expansion returns every stage with no job link in
        addition to the selected job's stages. That is correct for shared
        stages, but it leaves legacy columns on the owner-operator board. We
        identify the selected job from the action context (or the explicit
        job_id domain) and compare it with the configured owner job.
        """
        values = []

        def collect(value):
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)
            elif isinstance(value, str):
                values.extend(int(x) for x in re.findall(r"\d+", value))
            elif isinstance(value, int):
                values.append(value)

        ctx = self.env.context
        collect(ctx.get("default_job_id"))
        collect(ctx.get("search_default_job_id"))
        if ctx.get("active_model") == "hr.job":
            collect(ctx.get("active_id"))
        for term in domain or []:
            if len(term) >= 3 and term[0] == "job_id" \
                    and term[1] in ("=", "in"):
                collect(term[2])

        owner = self.env["ir.config_parameter"].sudo().get_param(
            "prema_owner_operator.job_id") or 5
        try:
            owner = int(owner)
        except (TypeError, ValueError):
            owner = 5
        return owner if owner in values else False

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        """Keep the owner-operator board to the stages actually linked to its job.

        Every other recruitment job keeps Odoo's normal group expansion.
        """
        job_id = self._owner_operator_group_job_id(domain)
        if job_id:
            return self.env["hr.recruitment.stage"].search(
                [("job_ids", "in", job_id)], order="sequence,id")
        return super()._read_group_stage_ids(stages, domain)

    @api.depends("owner_operator_application_id.service_province_ids")
    def _compute_oo_service_provinces(self):
        for applicant in self:
            applicant.oo_service_provinces = ", ".join(
                applicant.owner_operator_application_id.service_province_ids
                .mapped("name")) or False

    @api.depends("owner_operator_application_ids")
    def _compute_owner_operator_app(self):
        for applicant in self:
            applicant.owner_operator_application_id = \
                applicant.owner_operator_application_ids[:1]

    @api.model
    def _owner_operator_job(self):
        return self.env["prema.owner.operator.application"]._public_job()

    def write(self, vals):
        """Detect a deliberate move into Authority & Insurance Review."""
        old_stage = {}
        if "stage_id" in vals:
            old_stage = {applicant.id: applicant.stage_id for applicant in self}
        res = super().write(vals)
        if "stage_id" in vals and not self.env.context.get(
                "owner_operator_skip_stage_hook"):
            for applicant in self:
                app = applicant.owner_operator_application_id
                if app:
                    app._on_stage_change(
                        old_stage.get(applicant.id), applicant.stage_id)
        return res

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Inbound mail: tie the applicant to the owner-operator job.

        The job alias carries alias_defaults {'job_id': N}, but message_new
        does not apply them, so an emailed application landed with job_id
        empty and never got its onboarding record. The alias that received the
        message is in the context; if it is ours, set the job explicitly and
        let create() build the checklist.

        Verification mail must never create an applicant. If it reaches this
        model because the dedicated alias was missing, route it to the
        verification queue instead.
        """
        recipients = " ".join(str(msg_dict.get(key) or "") for key in (
            "to", "cc", "recipients")).lower()
        if "verification@premafirm.com" in recipients:
            return self.env[
                "prema.owner.operator.verification"].sudo().message_new(
                    msg_dict, custom_values)
        applicant = super().message_new(msg_dict, custom_values)
        job = self._owner_operator_job()
        if job and not applicant.job_id and self._addressed_to_job(msg_dict, job):
            applicant.sudo().write({"job_id": job.id})
            applicant.sudo()._ensure_owner_application()
        return applicant

    @api.model
    def _addressed_to_job(self, msg_dict, job):
        """Was this message addressed to the job's own alias?

        message_new does not receive the alias id in a stable place, so the
        recipient is read from the parsed headers. Deterministic: the alias
        address must appear in the To/Cc of THIS message.
        """
        alias = job.alias_id
        if not alias or not alias.alias_domain_id:
            return False
        address = ("%s@%s" % (alias.alias_name,
                              alias.alias_domain_id.name)).lower()
        hay = " ".join(str(msg_dict.get(k) or "") for k in ("to", "cc", "recipient"))
        raw = msg_dict.get("msg")
        if raw is not None and hasattr(raw, "get"):
            hay += " " + " ".join(str(raw.get(k) or "") for k in ("To", "Cc"))
        return address in hay.lower()

    @api.model_create_multi
    def create(self, vals_list):
        applicants = super().create(vals_list)
        job = self._owner_operator_job()
        if not job:
            return applicants
        for applicant in applicants:
            applicant._ensure_owner_application()
        return applicants

    def _ensure_owner_application(self):
        """Create the onboarding record for an applicant of the owner job.

        Idempotent: an applicant that already has one gets no second record.
        """
        self.ensure_one()
        job = self._owner_operator_job()
        if not job or self.job_id.id != job.id \
                or self.owner_operator_application_ids:
            return self.env["prema.owner.operator.application"]
        Application = self.env["prema.owner.operator.application"].sudo()
        app = Application.create({
            "hr_applicant_id": self.id,
            "partner_id": self.partner_id.id or False,
            "contact_name": self.partner_name,
            "email": self.email_from,
            "phone": self.partner_phone or False,
            "company_id": self.company_id.id or job.company_id.id,
        })
        app.name = "OO-%06d" % app.id
        app._ensure_checklist()
        app.message_post(
            body=_("Application created from the recruitment record."),
            subtype_xmlid="mail.mt_note")
        return app

    def action_open_coi_emails(self):
        self.ensure_one()
        app = self.owner_operator_application_id
        if not app:
            return False
        return app.action_open_coi_emails()

    def action_open_owner_operator_application(self):
        self.ensure_one()
        app = self.owner_operator_application_id
        return {
            "type": "ir.actions.act_window",
            "name": _("Owner-operator application"),
            "res_model": "prema.owner.operator.application",
            "res_id": app.id,
            "view_mode": "form",
            "target": "current",
        }

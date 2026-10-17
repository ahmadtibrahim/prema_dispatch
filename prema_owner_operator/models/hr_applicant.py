# -*- coding: utf-8 -*-
"""hr.applicant ↔ owner-operator application.

An application is created automatically for the Owner Operator job so that an
emailed CV (which threads into the standard applicant) still gets a checklist
and an acknowledgement. Creation is idempotent: an applicant that already has
an application is never given a second one.
"""
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
    oo_service_provinces = fields.Char(compute="_compute_oo_service_provinces")

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
    oo_door_clearance_in = fields.Float(related="owner_operator_application_id.door_clearance_in", readonly=True)
    oo_liftgate = fields.Selection(related="owner_operator_application_id.liftgate", readonly=True)
    oo_liftgate_capacity_lb = fields.Float(related="owner_operator_application_id.liftgate_capacity_lb", readonly=True)
    oo_dock_height_capable = fields.Selection(related="owner_operator_application_id.dock_height_capable", readonly=True)
    oo_air_ride = fields.Selection(related="owner_operator_application_id.air_ride", readonly=True)
    oo_reefer_temperature_range = fields.Char(related="owner_operator_application_id.reefer_temperature_range", readonly=True)
    oo_trailer_ownership = fields.Selection(related="owner_operator_application_id.trailer_ownership", readonly=True)
    oo_trailer_type = fields.Selection(related="owner_operator_application_id.trailer_type", readonly=True)
    oo_trailer_length_ft = fields.Float(related="owner_operator_application_id.trailer_length_ft", readonly=True)
    oo_trailer_unit = fields.Char(related="owner_operator_application_id.trailer_unit", readonly=True)
    oo_preferred_service_area = fields.Selection(related="owner_operator_application_id.preferred_service_area", readonly=True)
    oo_available_start_date = fields.Date(related="owner_operator_application_id.available_start_date", readonly=True)
    oo_work_preference = fields.Selection(related="owner_operator_application_id.work_preference", readonly=True)
    oo_whatsapp_consent = fields.Selection(related="owner_operator_application_id.whatsapp_consent", readonly=True)
    oo_application_declaration = fields.Boolean(related="owner_operator_application_id.application_declaration", readonly=True)
    oo_additional_notes = fields.Html(related="owner_operator_application_id.additional_notes", readonly=True)
    oo_cvor_number = fields.Char(related="owner_operator_application_id.cvor_number", readonly=True)
    oo_cvor_verification_status = fields.Selection(related="owner_operator_application_id.cvor_verification_status", readonly=True)
    oo_insurance_agent_name = fields.Char(related="owner_operator_application_id.insurance_agent_name", readonly=True)
    oo_insurance_agent_email = fields.Char(related="owner_operator_application_id.insurance_agent_email", readonly=True)
    oo_licence_current = fields.Boolean(related="owner_operator_application_id.licence_current", readonly=True)
    oo_missing_verification = fields.Text(related="owner_operator_application_id.missing_verification", readonly=True)
    oo_sign_request_id = fields.Many2one("sign.request", related="owner_operator_application_id.sign_request_id", readonly=True)
    oo_contract_signed_on = fields.Datetime(related="owner_operator_application_id.contract_signed_on", readonly=True)
    oo_operator_id = fields.Many2one("prema.owner.operator", related="owner_operator_application_id.operator_id", readonly=True)
    oo_fleet_vehicle_id = fields.Many2one("fleet.vehicle", related="owner_operator_application_id.fleet_vehicle_id", readonly=True)
    oo_approved_on = fields.Datetime(related="owner_operator_application_id.approved_on", readonly=True)

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

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Inbound mail: tie the applicant to the owner-operator job.

        The job alias carries alias_defaults {'job_id': N}, but message_new
        does not apply them, so an emailed application landed with job_id
        empty and never got its onboarding record. The alias that received the
        message is in the context; if it is ours, set the job explicitly and
        let create() build the checklist.
        """
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

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
        string="Owner-operator application")

    @api.depends("owner_operator_application_ids")
    def _compute_owner_operator_app(self):
        for applicant in self:
            applicant.owner_operator_application_id = \
                applicant.owner_operator_application_ids[:1]

    @api.model
    def _owner_operator_job(self):
        return self.env["prema.owner.operator.application"]._public_job()

    @api.model_create_multi
    def create(self, vals_list):
        applicants = super().create(vals_list)
        job = self._owner_operator_job()
        if not job:
            return applicants
        Application = self.env["prema.owner.operator.application"].sudo()
        for applicant in applicants:
            if applicant.job_id.id != job.id:
                continue
            if applicant.owner_operator_application_ids:
                continue
            app = Application.create({
                "hr_applicant_id": applicant.id,
                "partner_id": applicant.partner_id.id or False,
                "contact_name": applicant.partner_name,
                "email": applicant.email_from,
                "phone": applicant.partner_phone or False,
                "company_id": applicant.company_id.id or job.company_id.id,
                "home_city": False,
                "home_province": False,
                "equipment_type": False,
            })
            app.name = "OO-%06d" % app.id
            app._ensure_checklist()
            app.message_post(
                body=_("Application created from the recruitment record."),
                subtype_xmlid="mail.mt_note")
        return applicants

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

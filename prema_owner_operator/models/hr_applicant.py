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

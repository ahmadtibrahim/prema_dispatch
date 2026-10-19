# -*- coding: utf-8 -*-
"""Configuration data and review settings for owner-operator onboarding.

The service areas and work preferences are DATA: the public form and the
backend multi-selects both read these records, so adding a region does not
require a code change. The coverage thresholds and the trusted insurer
domains are editable in Owner-Operators ▸ Settings.
"""
from odoo import fields, models


class OwnerOperatorServiceArea(models.Model):
    _name = "prema.owner.operator.service.area"
    _description = "Owner-Operator Service Area"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_unique", "UNIQUE(code)",
         "Each service area code must be unique."),
    ]


class OwnerOperatorWorkPreference(models.Model):
    _name = "prema.owner.operator.work.preference"
    _description = "Owner-Operator Work Preference"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_unique", "UNIQUE(code)",
         "Each work preference code must be unique."),
    ]


class OwnerOperatorNotificationLog(models.Model):
    """Audit trail for load-notification preference changes.

    The application keeps the CURRENT preference; every deliberate change is
    appended here so a later opt-out or channel switch is provable.
    """
    _name = "prema.owner.operator.notification.log"
    _description = "Owner-Operator Notification Preference Change"
    _order = "create_date desc, id desc"

    application_id = fields.Many2one(
        "prema.owner.operator.application", required=True, index=True,
        ondelete="cascade")
    company_id = fields.Many2one(related="application_id.company_id",
                                 store=True, index=True)
    user_id = fields.Many2one("res.users", string="Changed by",
                              default=lambda self: self.env.user)
    channel = fields.Selection([
        ("whatsapp", "WhatsApp"),
        ("email", "Email"),
    ], string="Channel")
    whatsapp_number = fields.Char(string="WhatsApp number")
    email = fields.Char(string="Load-notification email")
    consent_given = fields.Boolean(string="WhatsApp consent given")
    consent_text = fields.Text(string="Consent wording")
    consent_version = fields.Char(string="Consent version")
    opt_out = fields.Boolean(string="Opted out")
    note = fields.Text()


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    oo_coi_min_auto_liability = fields.Float(
        string="Minimum automobile liability (CAD)",
        config_parameter="prema_owner_operator.coi_min_auto_liability",
        help="Editorial threshold shown to reviewers. Email receipt never "
             "auto-approves coverage; a reviewer compares the certificate.")
    oo_coi_min_cargo = fields.Float(
        string="Minimum cargo coverage (CAD)",
        config_parameter="prema_owner_operator.coi_min_cargo",
        help="Editorial cargo coverage threshold for the reviewer.")
    oo_coi_min_ai_confidence = fields.Float(
        string="Minimum AI extraction confidence (0-1)",
        config_parameter="prema_owner_operator.coi_min_ai_confidence",
        help="Automatic verification stays disabled while this is 0. Each "
             "required extracted value must reach this confidence.")
    oo_coi_certificate_holder_name = fields.Char(
        string="Certificate holder name",
        config_parameter="prema_owner_operator.coi_certificate_holder_name",
        default="PremaFirm Inc.")
    oo_coi_certificate_holder_address = fields.Char(
        string="Certificate holder address",
        config_parameter="prema_owner_operator.coi_certificate_holder_address",
        default="994 Westport Cres, Unit 7A, Mississauga, ON L5T 1G1, Canada")
    oo_trusted_insurer_domains = fields.Char(
        string="Trusted insurer/broker domains",
        config_parameter="prema_owner_operator.trusted_insurer_domains",
        help="Comma-separated sender domains. Only a matched email from one "
             "of these domains may attach a certificate to the insurer-direct "
             "checklist item automatically. The applicant always stays in "
             "Authority & Insurance Review for manual review. Leave empty to "
             "send every matched certificate to manual triage.")
    oo_verification_reviewer_id = fields.Many2one(
        "res.users", string="Verification reviewer",
        config_parameter="prema_owner_operator.verification_reviewer_id",
        help="User who receives the review activity for inbound verification "
             "mail. Defaults to the job recruiter when empty.")
    oo_whatsapp_consent_version = fields.Char(
        string="WhatsApp consent version",
        config_parameter="prema_owner_operator.whatsapp_consent_version",
        default="1")

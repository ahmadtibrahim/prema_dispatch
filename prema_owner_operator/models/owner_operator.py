# -*- coding: utf-8 -*-
"""prema.owner.operator — the operator profile.

Deliberately SEPARATE from res.partner and from fleet.vehicle:

* res.partner is the identity/vendor record the accounting and dispatch
  screens already read — it must not grow a second, parallel compliance
  schema;
* fleet.vehicle is the equipment, and a single operator can run more than
  one truck over time.

The profile is the place where carrier authority and insurance live, and it
is linked to the equipment (fleet.vehicle.operator_id) rather than copied
onto it. Nothing here writes a sales stage or an employee.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

AUTHORITY_STATES = [
    ("not_provided", "Not provided"),
    ("pending_manual", "Pending manual verification"),
    ("verified", "Verified"),
    ("rejected", "Rejected"),
]


class OwnerOperator(models.Model):
    _name = "prema.owner.operator"
    _description = "Owner-Operator Profile"
    _order = "name, id"

    name = fields.Char(
        string="Operator", compute="_compute_name", store=True, readonly=False)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True, index=True,
        default=lambda self: self.env.company)

    # ── identity ─────────────────────────────────────────────────────
    partner_id = fields.Many2one(
        "res.partner", string="Contact / Vendor", required=True, index=True,
        ondelete="restrict",
        help="The ONE partner record this operator owns. Created for the "
             "application and linked explicitly at approval — never "
             "re-matched to another partner by email address.")
    application_ids = fields.One2many(
        "prema.owner.operator.application", "operator_id",
        string="Applications")
    fleet_vehicle_ids = fields.One2many(
        "fleet.vehicle", "operator_id", string="Equipment")

    # ── business ────────────────────────────────────────────────────
    is_sole_proprietor = fields.Boolean(
        string="Sole proprietor",
        help="Drives the conditional document set: a sole proprietor has no "
             "corporate registration to upload.")
    business_number = fields.Char(string="Business / registration no.")
    home_city = fields.Char(string="Home city")
    home_province = fields.Char(
        string="Home province",
        help="Province/state only, as the brief requires — never a full "
             "address.")

    # ── carrier authority (manual until an API integration exists) ──
    authority_jurisdiction = fields.Char(string="Authority jurisdiction")
    mc_number = fields.Char(string="MC number")
    usdot_number = fields.Char(string="USDOT number")
    cvor_number = fields.Char(string="CVOR number")
    authority_state = fields.Selection(
        AUTHORITY_STATES, string="Authority status",
        default="not_provided", required=True,
        help="MC/USDOT/CVOR verification is manual for now: the record says "
             "so explicitly instead of implying a lookup that did not run.")
    authority_verified_on = fields.Datetime(string="Authority verified on")
    authority_verified_by = fields.Many2one("res.users", string="Verified by")
    authority_note = fields.Text(string="Authority note")

    # ── insurance (insurer-direct COI only) ─────────────────────────
    insurance_agent_name = fields.Char(string="Insurance agent")
    insurance_agent_email = fields.Char(string="Agent email")
    insurance_agent_phone = fields.Char(string="Agent phone")
    insurer_name = fields.Char(string="Insurer")
    policy_number = fields.Char(string="Policy number")
    insurance_expiry = fields.Date(string="Coverage expires")
    coi_document_id = fields.Many2one(
        "prema.owner.operator.document", string="COI on file",
        domain=[("document_type", "=", "coi_insurer")],
        help="Only the certificate received DIRECTLY from the insurer "
             "counts. An applicant-uploaded COI is a separate checklist row "
             "and never satisfies this one.")
    coi_verified_direct = fields.Boolean(
        string="COI verified with insurer", readonly=True, copy=False)
    coi_verified_on = fields.Datetime(string="COI verified on", readonly=True)

    # ── compliance roll-up (read by the Fleet availability predicate) ─
    compliance_ok = fields.Boolean(
        string="Compliance current", compute="_compute_compliance", store=True,
        help="Authority verified AND insurance coverage in date. "
             "Owner-operator equipment is only offered when this is true.")
    blocked_issue = fields.Boolean(
        string="Has blocking issue", compute="_compute_compliance", store=True)

    @api.depends("partner_id.name", "partner_id.display_name")
    def _compute_name(self):
        for op in self:
            op.name = op.partner_id.display_name or _("New operator")

    @api.depends("authority_state", "insurance_expiry", "coi_verified_direct",
                 "fleet_vehicle_ids.issue_cleared_on",
                 "fleet_vehicle_ids.state_id")
    def _compute_compliance(self):
        today = fields.Date.context_today(self)
        for op in self:
            authority_ok = op.authority_state == "verified"
            insurance_ok = bool(op.coi_verified_direct) and bool(
                op.insurance_expiry) and op.insurance_expiry >= today
            op.compliance_ok = authority_ok and insurance_ok
            op.blocked_issue = any(
                v.issue_blocking for v in op.fleet_vehicle_ids)

    # ── verification helpers (manual, auditable) ────────────────────
    def action_mark_authority_verified(self):
        self.write({
            "authority_state": "verified",
            "authority_verified_on": fields.Datetime.now(),
            "authority_verified_by": self.env.user.id,
        })
        return True

    def action_mark_authority_rejected(self):
        self.write({"authority_state": "rejected",
                    "authority_verified_on": fields.Datetime.now(),
                    "authority_verified_by": self.env.user.id})
        return True

    def action_verify_coi_with_insurer(self, document=None):
        """Record that the COI came from the insurer, not from the applicant.

        Kept as an explicit human action: the module must never infer
        insurer-direct verification from an uploaded file.
        """
        self.ensure_one()
        document = document or self.coi_document_id
        if not document or document.document_type != "coi_insurer" \
                or document.source != "insurer":
            raise ValidationError(_(
                "Insurer-direct verification requires a certificate recorded "
                "with source “insurer”."))
        if document.status != "received":
            raise ValidationError(_("That certificate has not been received."))
        self.write({
            "coi_document_id": document.id,
            "coi_verified_direct": True,
            "coi_verified_on": fields.Datetime.now(),
            "insurer_name": document.insurer_name or self.insurer_name,
            "policy_number": document.policy_number or self.policy_number,
            "insurance_expiry": document.expiry_date or self.insurance_expiry,
        })
        return True

    @api.constrains("partner_id")
    def _check_one_profile_per_partner(self):
        for op in self:
            others = self.search_count([
                ("partner_id", "=", op.partner_id.id), ("id", "!=", op.id)])
            if others:
                raise ValidationError(_(
                    "This contact already has an owner-operator profile."))

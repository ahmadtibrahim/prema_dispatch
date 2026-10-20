# -*- coding: utf-8 -*-
"""Repeatable operating authority / registration entries.

One application can hold several authorities (for example USDOT plus MC, or
an Ontario CVOR plus another province). The type controls the number format
and the issuing jurisdiction; a USDOT is never treated as MC authority.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..application_mapping import (
    AUTHORITY_REVIEW_STATES, AUTHORITY_TYPES, format_authority_number,
    _authority_digits as _digits)


class OwnerOperatorAuthority(models.Model):
    _name = "prema.owner.operator.authority"
    _description = "Owner-Operator Operating Authority"
    _order = "sequence, id"

    application_id = fields.Many2one(
        "prema.owner.operator.application", required=True, index=True,
        ondelete="cascade")
    company_id = fields.Many2one(
        related="application_id.company_id", store=True, index=True)
    sequence = fields.Integer(default=10)
    authority_type = fields.Selection(
        list(AUTHORITY_TYPES.items()), required=True,
        default="ontario_cvor", string="Authority / registration type")
    jurisdiction_country_id = fields.Many2one(
        "res.country", string="Issuing country")
    jurisdiction_state_id = fields.Many2one(
        "res.country.state", string="Issuing jurisdiction",
        domain="[('country_id', '=?', jurisdiction_country_id)]")
    jurisdiction_other = fields.Char(
        string="Other issuing jurisdiction",
        help="Only for type Other; do not invent a state or province.")
    number = fields.Char(string="Authority / registration number",
                         required=True)
    expiry_date = fields.Date(string="Expiry (optional)")
    attachment_id = fields.Many2one(
        "ir.attachment", string="Authority certificate",
        ondelete="restrict",
        help="Private supporting document. Never a public /web/content URL.")
    document_id = fields.Many2one(
        "prema.owner.operator.document", string="Checklist document",
        ondelete="set null", readonly=True,
        help="Legacy link to the single CVOR checklist row, when present.")
    review_status = fields.Selection(
        list(AUTHORITY_REVIEW_STATES.items()), default="pending",
        required=True, string="Review status")
    reviewer_id = fields.Many2one("res.users", string="Reviewed by",
                                  readonly=True, copy=False)
    reviewed_on = fields.Datetime(string="Reviewed on", readonly=True,
                                  copy=False)
    review_notes = fields.Text(string="Reviewer notes", copy=False)
    can_preview = fields.Boolean(
        string="View", compute="_compute_can_preview")
    file_name = fields.Char(related="attachment_id.name", readonly=True)
    mimetype = fields.Char(related="attachment_id.mimetype", readonly=True)
    jurisdiction_label = fields.Char(
        string="Jurisdiction", compute="_compute_jurisdiction_label")

    @api.depends("attachment_id")
    def _compute_can_preview(self):
        for authority in self:
            authority.can_preview = bool(authority.attachment_id)

    @api.depends("authority_type", "jurisdiction_country_id",
                 "jurisdiction_state_id", "jurisdiction_other")
    def _compute_jurisdiction_label(self):
        for authority in self:
            if authority.jurisdiction_state_id:
                country = authority.jurisdiction_state_id.country_id
                authority.jurisdiction_label = ", ".join(
                    p for p in (authority.jurisdiction_state_id.name,
                                country.name) if p)
            elif authority.jurisdiction_country_id:
                authority.jurisdiction_label = \
                    authority.jurisdiction_country_id.name
            else:
                authority.jurisdiction_label = \
                    authority.jurisdiction_other or False

    def _format_values(self, vals):
        authority_type = vals.get("authority_type") or self.authority_type
        code = (vals.get("jurisdiction_code") or "").strip()
        if code and "-" in code and not code.startswith("OTHER"):
            country_code, state_code = code.split("-", 1)
            country = self.env["res.country"].search(
                [("code", "=", country_code.upper())], limit=1)
            state = self.env["res.country.state"].search(
                [("country_id", "=", country.id),
                 ("code", "=", state_code.upper())], limit=1) \
                if country else self.env["res.country.state"]
            if country:
                vals["jurisdiction_country_id"] = country.id
            if state:
                vals["jurisdiction_state_id"] = state.id
        elif code == "US-FEDERAL":
            usa = self.env.ref("base.us", raise_if_not_found=False)
            if usa:
                vals["jurisdiction_country_id"] = usa.id
        number = vals.get("number", self.number)
        if number is not None:
            vals["number"] = format_authority_number(authority_type, number)
        if authority_type == "ontario_cvor":
            vals.setdefault(
                "jurisdiction_country_id",
                self.env.ref("base.ca").id if self.env.ref(
                    "base.ca", raise_if_not_found=False) else False)
            ontario = self.env["res.country.state"].search([
                ("country_id.code", "=", "CA"), ("code", "=", "ON")], limit=1)
            if ontario:
                vals.setdefault("jurisdiction_state_id", ontario.id)
        elif authority_type in ("usdot", "mc"):
            if not (vals.get("jurisdiction_country_id")
                    or self.jurisdiction_country_id):
                usa = self.env.ref(
                    "base.us", raise_if_not_found=False)
                if usa:
                    vals["jurisdiction_country_id"] = usa.id
        elif authority_type == "canadian_provincial":
            if not (vals.get("jurisdiction_country_id")
                    or self.jurisdiction_country_id):
                canada = self.env.ref(
                    "base.ca", raise_if_not_found=False)
                if canada:
                    vals["jurisdiction_country_id"] = canada.id
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._format_values(vals)
        return super().create(vals_list)

    def write(self, vals):
        vals = self._format_values(dict(vals))
        return super().write(vals)

    @api.constrains("authority_type", "number", "jurisdiction_country_id",
                    "jurisdiction_state_id", "jurisdiction_other")
    def _check_authority(self):
        for authority in self:
            raw = (authority.number or "").strip()
            if not raw:
                raise ValidationError(_(
                    "Enter the authority or registration number."))
            if authority.authority_type == "ontario_cvor" \
                    and len(_digits(raw)) != 9:
                raise ValidationError(_(
                    "An Ontario CVOR must contain exactly 9 digits."))
            if authority.authority_type in ("usdot", "mc") \
                    and not (1 <= len(_digits(raw)) <= 8):
                raise ValidationError(_(
                    "Enter a valid USDOT or MC number (1–8 digits)."))
            if authority.authority_type == "canadian_provincial" \
                    and not authority.jurisdiction_state_id:
                raise ValidationError(_(
                    "Choose the issuing province or territory for this "
                    "authority."))
            if authority.authority_type == "other" \
                    and not (authority.jurisdiction_other or "").strip():
                raise ValidationError(_(
                    "Describe the issuing jurisdiction for this authority."))

    def action_verify(self):
        self.write({
            "review_status": "verified",
            "reviewer_id": self.env.user.id,
            "reviewed_on": fields.Datetime.now(),
        })

    def action_reject(self):
        self.write({
            "review_status": "rejected",
            "reviewer_id": self.env.user.id,
            "reviewed_on": fields.Datetime.now(),
        })

    def action_request_review(self):
        self.write({"review_status": "review"})

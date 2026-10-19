# -*- coding: utf-8 -*-
"""Driver-contact fields kept distinct from applicant/reviewer values."""
from odoo import api, fields, models

from ..application_mapping import PROVINCE_CHOICES


class ResPartner(models.Model):
    _inherit = "res.partner"

    oo_is_driver = fields.Boolean(
        string="Owner-operator driver", index=True, copy=False)
    oo_application_id = fields.Many2one(
        "prema.owner.operator.application", string="Source application",
        index=True, ondelete="set null", copy=False)
    oo_document_ids = fields.Many2many(
        "documents.document", string="Owner-operator documents",
        compute="_compute_oo_document_ids")
    oo_licence_class = fields.Char(string="Licence class", copy=False)
    oo_licence_province = fields.Selection(
        PROVINCE_CHOICES, string="Licence province / territory", copy=False)
    oo_licence_expiry = fields.Date(string="Licence expiry", copy=False)
    oo_licence_number = fields.Char(string="Licence number", copy=False)
    oo_licence_verified = fields.Boolean(
        string="Licence verified", copy=False)
    oo_licence_verified_on = fields.Datetime(readonly=True, copy=False)
    oo_licence_verified_by = fields.Many2one(
        "res.users", readonly=True, copy=False)

    @api.depends("oo_is_driver")
    def _compute_oo_document_ids(self):
        Document = self.env["documents.document"].sudo()
        for partner in self:
            if not partner.oo_is_driver:
                partner.oo_document_ids = [(5, 0, 0)]
                continue
            partner.oo_document_ids = [(6, 0, Document.search([
                ("res_model", "=", "res.partner"),
                ("res_id", "=", partner.id),
                ("oo_role", "=", "driver"),
            ]).ids)]

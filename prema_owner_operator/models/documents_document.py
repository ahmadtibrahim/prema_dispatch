# -*- coding: utf-8 -*-
"""Trace links from an Odoo Documents record back to the application."""
from odoo import fields, models


class DocumentsDocument(models.Model):
    _inherit = "documents.document"

    oo_application_id = fields.Many2one(
        "prema.owner.operator.application", string="Source application",
        index=True, ondelete="set null", copy=False)
    oo_role = fields.Selection([
        ("driver", "Driver contact"),
        ("vehicle", "Fleet vehicle"),
    ], string="Owner-operator role", index=True, copy=False)
    oo_source_document_id = fields.Many2one(
        "prema.owner.operator.document", string="Checklist document",
        index=True, ondelete="set null", copy=False)
    oo_coi_version_id = fields.Many2one(
        "prema.owner.operator.coi.version", string="COI version",
        index=True, ondelete="set null", copy=False)
    oo_source_attachment_id = fields.Many2one(
        "ir.attachment", string="Source attachment",
        ondelete="set null", copy=False,
        help="The original private attachment this record's private copy was "
             "made from. The copy preserves bytes and checksum.")
    oo_source_checksum = fields.Char(string="Source SHA-256", copy=False)

    def action_open_owner_operator_document(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/owner_operator/documents/%d" % self.id,
            "target": "new",
        }

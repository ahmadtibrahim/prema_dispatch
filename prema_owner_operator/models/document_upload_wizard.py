# -*- coding: utf-8 -*-
"""Reviewer upload/replace for a private checklist document."""
import base64

from odoo import _, fields, models
from odoo.exceptions import UserError


class OwnerOperatorDocumentUpload(models.TransientModel):
    _name = "prema.owner.operator.document.upload"
    _description = "Upload Owner-Operator Document"

    document_id = fields.Many2one(
        "prema.owner.operator.document", required=True, ondelete="cascade")
    file = fields.Binary(string="File", required=True)
    filename = fields.Char(string="Filename", required=True)

    def action_upload(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Choose a file first."))
        payload = base64.b64decode(self.file)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": self.filename,
            "raw": payload,
            "mimetype": self.document_id.mimetype or False,
            "public": False,
        })
        self.document_id.sudo().write({
            "attachment_id": attachment.id,
            "status": "received",
            "received_on": fields.Datetime.now(),
        })
        if self.document_id.document_type == "cvor":
            self.document_id.application_id.sudo().action_extract_cvor(
                force=True)
        return {"type": "ir.actions.act_window_close"}

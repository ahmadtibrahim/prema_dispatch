# -*- coding: utf-8 -*-
"""The link from a warehouse job to the invoice it produced.

One field, deliberately: the job's own invoices are found by *this* link and by
nothing else. Matching them by the transportation booking, or by customer and
date, is how a warehouse charge ends up on a freight invoice — two documents
with different meanings and different terms, sharing one number.
"""
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    prema_pfl_job_id = fields.Many2one(
        "prema.pfl.warehouse.job", string="Warehouse Job",
        ondelete="set null", index=True, copy=False,
        help="Set on an invoice this module created for a warehouse job. Only "
             "drafts carrying this link are ever reused or extended; a posted "
             "invoice is never touched.")

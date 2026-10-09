# -*- coding: utf-8 -*-
"""Open a warehouse job straight from the accepted sales order.

The job is created from the commercial document rather than typed in beside
it, so the billing customer, the currency, the company and — most importantly
— the accepted rates all come from the thing the customer already agreed to.
A job opened by hand would have to be reconciled with the order later, and the
reconciliation is exactly where a rate gets copied wrong.
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    prema_pfl_job_ids = fields.One2many(
        "prema.pfl.warehouse.job", "sale_order_id", string="Warehouse Jobs")
    prema_pfl_job_count = fields.Integer(
        compute="_compute_prema_pfl_job_count")

    def _compute_prema_pfl_job_count(self):
        for order in self:
            order.prema_pfl_job_count = len(order.prema_pfl_job_ids)

    def action_pfl_create_job(self, service_mode="ltl"):
        """Create (or return) the warehouse job for this accepted order.

        Idempotent on purpose: pressing the button twice must not open a second
        job against one order and split the pallets across two ledgers.
        """
        self.ensure_one()
        if self.state not in ("sale",):
            raise UserError(_(
                "%s must be a confirmed sales order before warehouse work can "
                "be accepted against it.", self.name))
        existing = self.prema_pfl_job_ids.filtered(
            lambda j: j.state != "cancelled")[:1]
        if existing:
            return existing._pfl_open_job()
        job = self.env["prema.pfl.warehouse.job"].create({
            "partner_id": self.partner_id.id,
            "freight_owner_id": self.partner_id.id,
            "sale_order_id": self.id,
            "company_id": self.company_id.id,
            "service_mode": service_mode,
            "customer_reference": self.client_order_ref or False,
        })
        job.action_pfl_confirm()
        return job._pfl_open_job()

    def action_pfl_open_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Warehouse Jobs"),
            "res_model": "prema.pfl.warehouse.job",
            "view_mode": "list,form",
            "domain": [("sale_order_id", "=", self.id)],
            "context": {"default_sale_order_id": self.id,
                        "default_partner_id": self.partner_id.id},
        }

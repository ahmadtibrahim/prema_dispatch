# -*- coding: utf-8 -*-
"""Work that is recorded rather than derived: labour and agreed extras.

Handling, storage and wrapping all follow from the pallets themselves — how
many, for how long, released when. Labour and extras do not: nobody can derive
how many people carried a pallet, or what a customer agreed to pay for
something that is not in the catalogue. So they are entered, and the entry is
what the invoice reads.

Labour carries the minimum the business actually applies: one hour per person
per job. Recording it per work session (rather than per pallet) is what makes
that minimum mean what a customer expects.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PremaPflLabour(models.Model):
    _name = "prema.pfl.labour"
    _description = "PF Logistics Warehouse Labour"
    _order = "occurred_at, id"

    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", required=True, ondelete="cascade", index=True)
    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet", ondelete="set null",
        help="Optional. Set when the work was on one specific pallet.")
    occurred_at = fields.Datetime(
        required=True, default=fields.Datetime.now)
    user_id = fields.Many2one(
        "res.users", string="Recorded By", required=True,
        default=lambda self: self.env.user)
    persons = fields.Integer(
        string="People", required=True, default=1,
        help="How many people worked. The one-hour minimum applies to each of "
             "them, so the number of people is part of the charge.")
    minutes = fields.Float(
        string="Minutes", required=True,
        help="Actual minutes worked in this session.")
    note = fields.Char()
    billable_hours = fields.Float(
        string="Billable Person-Hours", compute="_compute_billable_hours",
        store=True, readonly=True,
        help="People × the greater of one hour and the actual time.")

    @api.depends("persons", "minutes")
    def _compute_billable_hours(self):
        for labour in self:
            hours = (labour.minutes or 0.0) / 60.0
            labour.billable_hours = (labour.persons or 0) * max(1.0, hours)

    @api.constrains("persons", "minutes")
    def _check_positive(self):
        for labour in self:
            if labour.persons < 1:
                raise UserError(_("Labour needs at least one person."))
            if labour.minutes < 0:
                raise UserError(_("Labour minutes cannot be negative."))


class PremaPflExtra(models.Model):
    _name = "prema.pfl.extra"
    _description = "PF Logistics Agreed Extra"
    _order = "job_id, id"

    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", required=True, ondelete="cascade", index=True)
    description = fields.Char(required=True)
    quantity = fields.Float(required=True, default=1.0)
    uom_id = fields.Many2one(
        "uom.uom", string="Unit of Measure", required=True,
        default=lambda self: self.env.ref("uom.product_uom_unit", False))
    unit_rate = fields.Float(string="Agreed Rate", required=True, digits="Product Price")
    amount = fields.Monetary(
        compute="_compute_amount", store=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="job_id.currency_id", store=True)

    #: An extra is billable only once the customer has agreed to it. Without
    #: that record the warehouse would be charging for work nobody asked for,
    #: which is the fastest way to lose a cross-dock customer.
    approved = fields.Boolean(
        string="Customer Approved", default=False,
        help="Set only once the customer has actually accepted this extra and "
             "its rate. Unapproved extras are described but never billed.")
    approved_by = fields.Many2one("res.users", string="Approved By", readonly=True)
    approved_at = fields.Datetime(string="Approved At", readonly=True)
    approval_reference = fields.Char(
        string="Approval Reference",
        help="Where the agreement is — an email subject, a call, a PO line.")

    @api.depends("quantity", "unit_rate")
    def _compute_amount(self):
        for extra in self:
            extra.amount = (extra.quantity or 0.0) * (extra.unit_rate or 0.0)

    def action_approve(self):
        now = fields.Datetime.now()
        for extra in self:
            extra.write({
                "approved": True,
                "approved_by": self.env.user.id,
                "approved_at": now,
            })
        return True

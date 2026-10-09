# -*- coding: utf-8 -*-
"""The charges a job actually earned, and the record of why.

Every charge is a row that can be read out loud to a customer: what service,
what quantity, at what agreed rate, on what basis, and — once it is billed —
which invoice line carries it. The `basis` text is not decoration; it is the
difference between an invoice the customer accepts and one they query.

Two properties make the ledger safe to recompute:

*Idempotent by key.* Every charge carries a deterministic `idempotency_key`
built from the job, the service and the thing being charged (a pallet's token,
a labour entry, a storage period). Recomputing a job therefore rewrites the
same rows instead of appending new ones, so clicking "Review Charges" twice
cannot bill twice.

*Closed once billed.* A charge that has reached an invoice is never recomputed
or deleted. That is what allows a later release to be invoiced on its own
without re-billing the first one.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..service_map import CHARGEABLE_SERVICES, SERVICE_LABELS


class PremaPflCharge(models.Model):
    _name = "prema.pfl.charge"
    _description = "PF Logistics Warehouse Charge"
    _order = "job_id, service, id"

    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", required=True, ondelete="cascade", index=True)
    service = fields.Selection(
        [(code, SERVICE_LABELS[code]) for code in CHARGEABLE_SERVICES],
        required=True, index=True)
    name = fields.Char(
        string="Description", required=True,
        help="What prints on the invoice line.")
    basis = fields.Char(
        string="Basis",
        help="How the quantity was arrived at — '4 pallets handled', "
             "'pallet PACK0000012, 30.0 h, 1 started period'. Read aloud to a "
             "customer, this is the answer to 'why this amount?'.")
    product_id = fields.Many2one(
        "product.product", string="Product", ondelete="restrict")
    quantity = fields.Float(digits="Product Unit of Measure")
    uom_id = fields.Many2one("uom.uom", string="Unit of Measure")
    unit_rate = fields.Float(string="Accepted Rate", digits="Product Price")
    amount = fields.Monetary(string="Amount", currency_field="currency_id")
    currency_id = fields.Many2one(related="job_id.currency_id", store=True)

    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet", ondelete="set null",
        help="Set when the charge belongs to one pallet (handling, wrapping, "
             "a storage period) rather than to the job as a whole.")
    idempotency_key = fields.Char(
        required=True, index=True,
        help="Deterministic identity of this charge. Recomputing the job "
             "matches on this key, so the same work cannot be charged twice.")

    invoiced = fields.Boolean(readonly=True, copy=False, index=True)
    invoice_line_id = fields.Many2one(
        "account.move.line", string="Invoice Line", readonly=True, copy=False,
        ondelete="set null")
    invoice_id = fields.Many2one(
        related="invoice_line_id.move_id", string="Invoice", store=True)

    _sql_constraints = [
        ("pfl_charge_key_uniq", "unique(idempotency_key)",
         "This charge already exists. Charges are identified by their "
         "idempotency key so a repeated action cannot bill the same work twice."),
    ]

    @api.constrains("amount", "unit_rate", "service")
    def _check_non_negative(self):
        for charge in self:
            if charge.unit_rate < 0 or charge.amount < 0:
                raise UserError(_(
                    "A warehouse charge cannot be negative (%(name)s). "
                    "Correct a billing mistake with a credit note, not by "
                    "hiding it in a negative line.", name=charge.name))

    @api.constrains("invoiced", "amount")
    def _check_invoiced_locked(self):
        """Once a charge is on an invoice it is closed to recomputation.

        A charge with nothing to bill is the one exception: a service the
        customer agreed to take free has no invoice line to point at, and
        closing it is how it stops reappearing in the uninvoiced total. The
        exception is keyed on the amount, so a *priced* charge can never reach
        the invoiced state without a line to prove it was billed.
        """
        for charge in self:
            if charge.invoiced and not charge.invoice_line_id and charge.amount:
                raise UserError(_(
                    "Charge %(name)s is marked invoiced for %(amount)s but "
                    "carries no invoice line. That state would hide the charge "
                    "from the next invoice without ever billing it.",
                    name=charge.name, amount=charge.amount))

    def unlink(self):
        if any(charge.invoiced for charge in self):
            raise UserError(_(
                "A charge that has been invoiced cannot be deleted. Issue a "
                "credit note if the invoice was wrong."))
        return super().unlink()

# -*- coding: utf-8 -*-
"""A customer's own pallet barcode — an alias, never the identity.

Freight arrives carrying the shipper's label all the time, and scanning that
label is the fastest thing an operator can do. The trap is treating it as an
identity. External barcodes are not unique and were never meant to be: a
customer reprints yesterday's label, two suppliers share a numbering scheme, a
roll of stickers is reused. If the first match wins, two different pallets
silently become one record — and the warehouse then has a phantom pallet, a
missing pallet, and a storage bill that is wrong in both directions.

Three rules follow, and they are the whole design:

1. An alias is scoped to one customer. The same digits from two customers are
   two different pallets, because they are.
2. Within one customer the code may not repeat. A second pallet claiming a code
   already in use is refused rather than merged, so the ambiguity surfaces at
   the moment it appears instead of at invoicing.
3. The alias is a lookup only. The pallet's identity is its `stock.quant.package`
   and its opaque QR token; nothing is ever *keyed* on a customer barcode.

Re-resolving a superseded code is therefore an explicit act — the operator
reassigns it, thereby recording that the shipper reused their own label.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PremaPflBarcodeAlias(models.Model):
    _name = "prema.pfl.barcode.alias"
    _description = "PF Logistics Customer Pallet Barcode"
    _order = "customer_id, barcode"

    barcode = fields.Char(string="Customer Barcode", required=True, index=True)
    customer_id = fields.Many2one(
        "res.partner", string="Customer", required=True, index=True,
        help="The alias is scoped to one customer. The same code belonging to "
             "a different customer is a different pallet.")
    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet", required=True,
        ondelete="cascade", index=True)
    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", string="Warehouse Job",
        related="package_id.pfl_job_id", store=True, index=True)
    active = fields.Boolean(default=True)
    note = fields.Char(
        help="Why this alias exists, or what it superseded.")

    _sql_constraints = [
        ("pfl_alias_customer_barcode_uniq", "unique(customer_id, barcode)",
         "This customer already has a pallet carrying that barcode. Two "
         "pallets must never share one code — either the pallet is already "
         "recorded, or the customer has reused their label and the old alias "
         "must be retired first."),
    ]

    @api.model
    def _pfl_resolve(self, barcode, customer=None):
        """Find the pallet(s) a scanned customer barcode points at.

        Returns a recordset. It may contain more than one entry when the code
        was scanned without a customer — in that case the caller must not
        guess, and this method says so rather than picking the first row.
        """
        barcode = (barcode or "").strip()
        if not barcode:
            return self.browse()
        domain = [("barcode", "=", barcode), ("active", "=", True)]
        if customer:
            domain.append(("customer_id", "=", customer.id))
        aliases = self.search(domain)
        return aliases.mapped("package_id")

    def action_retire(self):
        """Retire an alias for a label the shipper has reused.

        Kept as a record rather than a delete, so "this code used to mean that
        pallet" survives the reassignment.
        """
        for alias in self:
            alias.write({
                "active": False,
                "note": _("%(old)s (retired %(note)s)",
                          old=alias.note or "",
                          note=_("label reused")),
            })
        return True

    @api.constrains("barcode", "customer_id")
    def _check_not_blank(self):
        for alias in self:
            if not (alias.barcode or "").strip():
                raise UserError(_("A customer barcode cannot be blank."))

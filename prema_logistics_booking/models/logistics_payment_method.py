"""Payment methods accepted on PremaFirm freight documents (§7, D-B2).

A small configuration catalog — NOT a live payment provider.  A row only
describes HOW the customer may pay a Rate Confirmation / booking / invoice
(credit card, Interac e-Transfer, terms, ...), what fees apply and where
the customer finds the instructions.  Partners restrict which of these
methods they are allowed to use (res_partner_logistics); documents then
carry the selected method + terms RC → booking → invoice unchanged.

Display-only wiring: the secure card-payment URL is a config-parameter
placeholder (no live provider), rendered at document-build time.
"""
from odoo import fields, models


class LogisticsPaymentMethod(models.Model):
    _name = "logistics.payment.method"
    _description = "Logistics Payment Method"
    _order = "sequence, id"

    name = fields.Char(string="Method", required=True, translate=True)
    code = fields.Char(string="Code", required=True)
    method_type = fields.Selection([
        ("card", "Credit Card"),
        ("etransfer", "Interac e-Transfer"),
        ("other", "Terms / Other"),
    ], string="Type", required=True, default="other",
        help="Card and e-Transfer methods can carry a per-document "
             "selection and instructions; 'Terms / Other' methods are "
             "plain labels carried onto the customer documents.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    fee_notes = fields.Text(
        string="Applicable Fees",
        help="Fees the customer must be told about when choosing this "
             "method (shown on the Rate Confirmation and the invoice).")
    instructions = fields.Text(
        string="Instructions",
        help="Customer instructions shown when this method is selected. "
             "May reference %(card_link_url)s or %(etransfer_instructions)s "
             "placeholders, substituted from the system parameters / the "
             "customer profile at document-build time.")

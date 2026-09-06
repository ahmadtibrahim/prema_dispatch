from odoo import _, api, fields, models

PRICING_STATUS_SELECTION = [
    ("none", "None"),
    ("pending", "Pending Approval"),
    ("approved", "Approved"),
    ("blocked", "Blocked"),
]

BILLING_RELATIONSHIP_SELECTION = [
    ("direct", "Direct Shipper / Consignee"),
    ("interlining", "Interlining Carrier / Subcontract Customer"),
    ("manual_review", "Manual Review"),
]

TAX_TREATMENT_SELECTION = [
    ("automatic", "Automatic"),
    ("zero_rated_interlining", "Zero Rated Interlining"),
    ("manual_review", "Manual Review"),
]


class ResPartner(models.Model):
    _inherit = "res.partner"

    logistics_pricing_status = fields.Selection(
        PRICING_STATUS_SELECTION, default="none", tracking=True,
        help="Business approval state for private LTL/FTL pricing access. "
             "Flipped together with group_logistics_customer membership by "
             "action_approve_logistics_pricing() -- never edit one without the other.",
    )

    # ── Freight Tax Profile ─────────────────────────────────────────────
    x_freight_billing_relationship = fields.Selection(
        BILLING_RELATIONSHIP_SELECTION, string="Default Billing Relationship",
        default="direct", tracking=True,
        help="Direct Shipper/Consignee → destination-based tax applies.\n"
             "Interlining Carrier → zero-rated.\n"
             "Manual Review → booking held for tax review.",
    )
    x_freight_tax_treatment = fields.Selection(
        TAX_TREATMENT_SELECTION, string="Default Freight Tax Treatment",
        default="automatic", tracking=True,
        help="Automatic → decision engine chooses tax.\n"
             "Zero Rated Interlining → always zero-rated.\n"
             "Manual Review → always held for review.",
    )
    x_freight_tax_rules_apply = fields.Boolean(
        string="Tax Rules Apply", compute="_compute_x_freight_tax_rules_apply",
        help="Direct → Yes. Interlining → No. Manual Review → Review.",
    )
    x_freight_tax_rules_display = fields.Char(
        string="Tax Rules Apply", compute="_compute_x_freight_tax_rules_apply",
    )
    x_freight_accounting_notes = fields.Text(
        string="Accounting Notes",
        help="Visible only to Accounting or Logistics Managers.",
    )

    # ── Payment methods & QuickPay (§7, D-B2) ─────────────────────────
    # Partner-level payment configuration. All QuickPay fields default to
    # OFF: a customer only gets an early-payment discount when a booking
    # manager has explicitly enabled it here (eligibility flag + discount
    # % + deadline in days) and the document then opts in.
    x_logistics_allowed_payment_method_ids = fields.Many2many(
        "logistics.payment.method",
        "res_partner_logistics_payment_method_rel",
        "partner_id", "payment_method_id",
        string="Allowed Payment Methods",
        help="Payment methods this customer may use. Empty = all active "
             "methods are allowed.",
    )
    x_logistics_default_payment_method_id = fields.Many2one(
        "logistics.payment.method",
        string="Default Payment Method",
        domain="[('active', '=', True),"
               "('id', 'in', x_logistics_allowed_payment_method_ids)]",
        help="Payment method proposed on new Rate Confirmations for this "
             "customer (must be one of the allowed methods).",
    )
    x_logistics_etransfer_instructions = fields.Text(
        string="e-Transfer Instructions",
        help="Interac e-Transfer instructions (email address, security "
             "question hint) shown on the customer documents when "
             "e-Transfer is the selected payment method. Falls back to "
             "the method's own instructions when empty.",
    )
    x_logistics_quickpay_enabled = fields.Boolean(
        string="QuickPay Eligible", default=False, tracking=True,
        help="Customer-specific QuickPay early-payment discount: DISABLED "
             "by default. Enable only with explicit commercial sign-off.",
    )
    x_logistics_quickpay_discount_pct = fields.Float(
        string="QuickPay Discount %", tracking=True,
        help="Early-payment discount percentage offered to this customer.",
    )
    x_logistics_quickpay_deadline_days = fields.Integer(
        string="QuickPay Deadline (days)", default=7, tracking=True,
        help="Discount valid when paid within this many days of the "
             "invoice date.",
    )
    x_logistics_quickpay_stack_allowed = fields.Boolean(
        string="QuickPay Stacking Allowed", default=False, tracking=True,
        help="Allow QuickPay to stack with other discounts (e.g. a manual "
             "price adjustment) on the same document. Default OFF — "
             "discounts never stack silently.",
    )

    @api.depends("x_freight_billing_relationship")
    def _compute_x_freight_tax_rules_apply(self):
        for rec in self:
            if rec.x_freight_billing_relationship == "direct":
                rec.x_freight_tax_rules_apply = True
                rec.x_freight_tax_rules_display = "Yes"
            elif rec.x_freight_billing_relationship == "interlining":
                rec.x_freight_tax_rules_apply = False
                rec.x_freight_tax_rules_display = "No"
            else:
                rec.x_freight_tax_rules_apply = False
                rec.x_freight_tax_rules_display = "Review"

    def _logistics_payment_defaults(self):
        """Partner → Rate Confirmation payment defaults (§7).

        Returns payment-related vals for a new customer document: the
        partner's default method (when allowed) and QuickPay profile
        (OFF by design when the customer is not eligible)."""
        self.ensure_one()
        method = self.x_logistics_default_payment_method_id
        allowed = self.x_logistics_allowed_payment_method_ids
        if method and allowed and method.id not in allowed.ids:
            method = self.env["logistics.payment.method"].browse(False)
        vals = {
            "payment_method_id": method.id if method else False,
            "payment_term_id": self.property_payment_term_id.id or False,
        }
        if self.x_logistics_quickpay_enabled:
            vals.update({
                "quickpay_apply": True,
                "quickpay_discount_pct": self.x_logistics_quickpay_discount_pct,
                "quickpay_deadline_days": self.x_logistics_quickpay_deadline_days,
                "quickpay_stack_allowed": self.x_logistics_quickpay_stack_allowed,
            })
        return vals

    def action_request_logistics_pricing(self):
        for partner in self:
            if partner.logistics_pricing_status in ("none", False):
                partner.logistics_pricing_status = "pending"

    def action_approve_logistics_pricing(self):
        group = self.env.ref("prema_logistics_booking.group_logistics_customer")
        for partner in self:
            partner.logistics_pricing_status = "approved"
            users = self.env["res.users"].sudo().search([("partner_id", "=", partner.id)])
            users.sudo().write({"groups_id": [(4, group.id)]})

    def action_block_logistics_pricing(self):
        group = self.env.ref("prema_logistics_booking.group_logistics_customer")
        for partner in self:
            partner.logistics_pricing_status = "blocked"
            users = self.env["res.users"].sudo().search([("partner_id", "=", partner.id)])
            users.sudo().write({"groups_id": [(3, group.id)]})

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

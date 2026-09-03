from odoo import _, api, fields, models
from odoo.exceptions import UserError


class CrmLead(models.Model):
    """Bridge CRM opportunities to the canonical Dispatch quote workflow.

    This method intentionally opens a draft pricing wizard only.  It never
    prices with the AI knowledge base, sends email, confirms a sale order,
    creates a booking, or creates an invoice.
    """

    _inherit = "crm.lead"

    logistics_quote_ids = fields.One2many(
        "logistics.custom.quote",
        "crm_lead_id",
        string="Rate Confirmations",
        readonly=True,
    )
    logistics_quote_count = fields.Integer(
        string="Rate Confirmations",
        compute="_compute_logistics_quote_count",
    )

    @api.depends("logistics_quote_ids")
    def _compute_logistics_quote_count(self):
        grouped = self.env["logistics.custom.quote"]._read_group(
            [("crm_lead_id", "in", self.ids)],
            ["crm_lead_id"],
            ["__count"],
        ) if self.ids else []
        counts = {lead.id: count for lead, count in grouped}
        for lead in self:
            lead.logistics_quote_count = counts.get(lead.id, 0)

    def action_open_dispatch_rate_quote(self):
        """Open the staff quote wizard backed by Dispatch's pricing engine."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Select or create the Customer on this opportunity before "
                "calculating a dispatch rate."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Calculate Dispatch Rate"),
            "res_model": "logistics.phone.booking",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.partner_id.id,
                "default_crm_lead_id": self.id,
            },
        }

    def action_open_dispatch_rate_confirmations(self):
        """Open persistent draft/quoted rate records linked to this lead."""
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Rate Confirmations"),
            "res_model": "logistics.custom.quote",
            "view_mode": "list,form",
            "domain": [("crm_lead_id", "=", self.id)],
            "context": {
                "default_crm_lead_id": self.id,
                "default_partner_id": self.partner_id.id,
                "default_source": "internal",
            },
        }
        if self.logistics_quote_count == 1:
            action.update({
                "view_mode": "form",
                "res_id": self.logistics_quote_ids.id,
            })
        return action

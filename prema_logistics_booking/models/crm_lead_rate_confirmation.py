from odoo import _, api, fields, models


class CrmLead(models.Model):
    """The opportunity's history door to retired Rate Confirmations.

    `logistics.custom.quote` is no longer a workflow. The Sales quotation is
    the only commercial quotation (see ``crm_lead_quotation_bridge``), and
    nothing here creates, prices, sends or advances a Rate Confirmation.
    What remains is the read-only view of the ones already issued, because
    they are commercial records the customer may still cite.
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

    def action_open_dispatch_rate_confirmations(self):
        """Open the retired Rate Confirmations linked to this lead.

        History only: the records are read-only by ACL and carry no workflow
        buttons, so opening them can never start a second quotation.
        """
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Rate Confirmations (History)"),
            "res_model": "logistics.custom.quote",
            "view_mode": "list,form",
            "domain": [("crm_lead_id", "=", self.id)],
        }
        if self.logistics_quote_count == 1:
            action.update({
                "view_mode": "form",
                "res_id": self.logistics_quote_ids.id,
            })
        return action

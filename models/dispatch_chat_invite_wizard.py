from odoo import fields, models


class DispatchChatInviteWizard(models.TransientModel):
    """Lets a dispatcher/manager see who's in a driver's chat channel and
    invite or remove extra participants — the driver<->dispatch chat
    defaults to just those two sides (audit item 26)."""
    _name = "prema.dispatch.chat.invite.wizard"
    _description = "Manage Driver Chat Members"

    job_id = fields.Many2one("prema.dispatch.job", required=True)
    channel_name = fields.Char(readonly=True)
    current_member_ids = fields.Many2many(
        "res.partner", string="Current Members", readonly=True,
    )
    invite_partner_ids = fields.Many2many(
        "res.partner", relation="dispatch_chat_invite_wizard_partner_rel",
        string="Invite",
        domain="[('id', 'not in', current_member_ids)]",
    )
    remove_partner_ids = fields.Many2many(
        "res.partner", relation="dispatch_chat_remove_wizard_partner_rel",
        string="Remove",
        domain="[('id', 'in', current_member_ids)]",
        help="Pick current members to remove (the driver can't be removed from their own chat).",
    )

    def _refresh_members(self):
        self.ensure_one()
        info = self.job_id.get_driver_channel_info()
        self.write({
            "channel_name": info.get("channel_name") or "",
            "current_member_ids": [(6, 0, [m["id"] for m in info.get("members", [])])],
            "invite_partner_ids": [(5, 0, 0)],
            "remove_partner_ids": [(5, 0, 0)],
        })

    def _reopen(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "prema.dispatch.chat.invite.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_invite(self):
        self.ensure_one()
        if self.invite_partner_ids:
            self.job_id.invite_to_driver_channel(self.invite_partner_ids.ids)
        self._refresh_members()
        return self._reopen()

    def action_remove(self):
        self.ensure_one()
        for partner in self.remove_partner_ids:
            self.job_id.remove_from_driver_channel(partner.id)
        self._refresh_members()
        return self._reopen()

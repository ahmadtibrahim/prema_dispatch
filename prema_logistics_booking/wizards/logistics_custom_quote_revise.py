# ════════════════════════════════════════════════════════════════════
# "Revise & Resend" — the ONLY authorized unlock/revision path for a
# sent (locked) customer Rate Confirmation.
#
# A sent Rate Confirmation is immutable: its document fields may never be
# edited again and its email can never go out twice. When the customer
# needs different terms, a booking manager records WHY and this wizard
# branches revision N+1 (a new logistics.custom.quote row that keeps the
# same document number, inherits the shipment/commercial payload, and
# starts unlocked with its own send-attempt budget).
# ════════════════════════════════════════════════════════════════════
from odoo import _, fields, models
from odoo.exceptions import AccessError


class LogisticsCustomQuoteRevise(models.TransientModel):
    _name = "logistics.custom.quote.revise"
    _description = "Revise and Resend Rate Confirmation"

    quote_id = fields.Many2one(
        "logistics.custom.quote",
        string="Rate Confirmation",
        required=True,
        readonly=True,
    )
    quote_reference = fields.Char(
        string="Document", related="quote_id.name", readonly=True)
    current_revision = fields.Integer(
        string="Current Revision", related="quote_id.revision_no",
        readonly=True)
    reason = fields.Text(
        string="Reason for Revision", required=True,
        help="What changed for the customer and why. Recorded on the new "
             "revision with your name and the timestamp.")

    def action_revise(self):
        """Create revision N+1 of the sent quote and open it for editing."""
        self.ensure_one()
        if not self.env.user.has_group(
                "prema_logistics_booking.group_logistics_booking_manager"):
            raise AccessError(_(
                "Only booking managers may revise a sent Rate Confirmation."))
        new_quote = self.quote_id.action_revise(self.reason)
        return {
            "type": "ir.actions.act_window",
            "name": _("Rate Confirmation"),
            "res_model": "logistics.custom.quote",
            "res_id": new_quote.id,
            "view_mode": "form",
            "target": "current",
        }

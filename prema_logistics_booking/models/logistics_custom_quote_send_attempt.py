# ════════════════════════════════════════════════════════════════════
# Customer Rate Confirmation send attempts (logistics.custom.quote).
#
# Append-only, immutable record of every EXPLICIT send of a custom-quote
# Rate Confirmation document. One row per (quote, revision) — a revision
# may be sent at most once, and nothing in the module ever re-emails a
# previously sent revision (the only route to a new customer email is the
# authorized "Revise & Resend" path, which creates a NEW revision draft
# whose own send gets a fresh attempt row).
#
# Mirrors the engine's confirmation-email guard pattern (PR #96 /
# sale_order_extension): marker written BEFORE queueing, refuse second
# send under a FOR UPDATE row lock. This table is the durable history the
# engine pattern keeps in per-order marker fields.
# ════════════════════════════════════════════════════════════════════
from odoo import _, fields, models
from odoo.exceptions import UserError

_INTERNAL_UPDATE_CTX = "logistics_cq_send_attempt_internal_write"


class LogisticsCustomQuoteSendAttempt(models.Model):
    _name = "logistics.custom.quote.send.attempt"
    _description = "Rate Confirmation Send Attempt"
    _order = "id desc"

    cq_id = fields.Many2one(
        "logistics.custom.quote",
        string="Rate Confirmation",
        required=True,
        ondelete="cascade",
        index=True,
        auto_join=True,
    )
    state = fields.Selection([
        ("sent", "Sent (queued)"),
        ("failed", "Failed"),
    ], string="State", default="sent", readonly=True,
        help="'Sent' is written BEFORE the mail is queued — a repeated "
             "click/retry can never double-send (the marker and the row "
             "roll back together with the transaction if the send fails).")
    sent_at = fields.Datetime(
        string="Sent At", readonly=True, default=fields.Datetime.now)
    revision_no = fields.Integer(
        string="Revision", readonly=True,
        help="Snapshot of the quote's revision_no when this send happened. "
             "Each revision may be sent at most once.")
    template_ref = fields.Char(
        string="Template / Document",
        readonly=True,
        help="External id of the document (report) that was rendered and "
             "attached — which PDF revision the customer received.")
    mail_mail_id = fields.Many2one(
        "mail.mail", string="Queued Email", readonly=True,
        ondelete="set null")
    provider_message_id = fields.Char(
        string="Provider Message ID", readonly=True,
        help="Provider message id reported by the outgoing mail engine, "
             "when known (the mail.mail row is the queue record).")
    send_hash = fields.Char(
        string="Idempotency Hash", readonly=True, index=True,
        help="sha1 fingerprint of (quote, revision, document content "
             "fields) — recorded for duplicate detection.")

    # ── Append-only ───────────────────────────────────────────────────
    def write(self, vals):
        if not self.env.context.get(_INTERNAL_UPDATE_CTX):
            raise UserError(_(
                "Rate Confirmation send attempts are append-only history "
                "and may never be edited."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_(
            "Rate Confirmation send attempts are append-only history and "
            "may never be deleted."))

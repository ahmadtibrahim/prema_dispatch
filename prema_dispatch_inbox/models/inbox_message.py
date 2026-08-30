# -*- coding: utf-8 -*-
"""prema.inbox.message — one email/note per row.

Per-message, per-user read state lives on the m2m
prema_inbox_message_read_rel (message_id, user_id). Personal unread is
derived: incoming messages where the current user is not in the readers set.
The shared workflow state (open/waiting/completed) is a conversation field,
deliberately separate — reading is not completing.
"""
import uuid

from odoo import api, fields, models


class InboxMessage(models.Model):
    _name = "prema.inbox.message"
    _description = "Dispatch Inbox Message"
    # mail.thread.blacklist NOT inherited: it requires an 'email' field
    # (_primary_email) — the inbox keeps its own is_spam on the
    # conversation. mail.activity.mixin is enough here.
    _inherit = ["mail.activity.mixin"]
    _order = "date asc, id asc"
    _rec_name = "subject"

    conversation_id = fields.Many2one(
        "prema.inbox.conversation", string="Conversation",
        ondelete="cascade", required=True, index=True)
    direction = fields.Selection([
        ("incoming", "Incoming"),
        ("outgoing", "Outgoing"),
        ("note", "Internal note"),
    ], string="Direction", required=True, default="incoming", index=True)
    date = fields.Datetime(string="Date", required=True, index=True)
    author_id = fields.Many2one(
        "res.partner", string="Author",
        help="External sender (incoming) or internal user's partner (outgoing).")
    author_internal = fields.Many2one(
        "res.users", string="Internal author",
        help="Set only when the author is an internal user (outgoing/notes).")
    email_from = fields.Char(string="From")
    recipient_ids = fields.Many2many(
        "res.partner", "prema_inbox_message_recipient_rel",
        "message_id", "partner_id", string="To")
    cc_ids = fields.Many2many(
        "res.partner", "prema_inbox_message_cc_rel",
        "message_id", "partner_id", string="Cc")
    subject = fields.Char(string="Subject")
    body = fields.Html(string="Body")
    body_plain = fields.Text(string="Plain text body")
    message_id = fields.Char(
        string="Message-ID", index=True,
        help="RFC 5322 Message-ID — dedupe key. Unique per thread element.")
    references = fields.Char(string="References")
    in_reply_to = fields.Char(string="In-Reply-To")
    attachment_ids = fields.Many2many(
        "ir.attachment", "prema_inbox_message_attachment_rel",
        "message_id", "attachment_id", string="Attachments")
    is_load_board = fields.Boolean(string="Load board alert", default=False)
    outbound_state = fields.Selection([
        ("draft", "Draft"),
        ("pending", "Pending send"),
        ("sent", "Sent"),
        ("failed", "Send failed"),
        ("intercepted", "Intercepted (UAT)"),
    ], string="Outbound state", default=False)
    send_error = fields.Text(string="Send error")
    read_user_ids = fields.Many2many(
        "res.users", "prema_inbox_message_read_rel",
        "message_id", "user_id", string="Read by")
    is_read = fields.Boolean(
        string="Read by me", compute="_compute_is_read",
        help="Whether env.user has read this message (incoming only).")

    _sql_constraints = [
        ("message_id_unique", "UNIQUE(message_id)",
         "A message with this Message-ID already exists (duplicate)."),
    ]

    @api.depends("direction", "read_user_ids")
    def _compute_is_read(self):
        uid = self.env.uid
        for msg in self:
            msg.is_read = (
                msg.direction != "incoming"
                or uid in msg.read_user_ids.ids
            )

    # ------------------------------------------------------------------
    # read-state helpers
    # ------------------------------------------------------------------
    def mark_read(self, user=None):
        """Mark the *actually displayed* messages as read for a user.

        Called from the UI with the ids of the incoming messages that were
        rendered/loaded in the conversation — never with the whole mailbox.
        """
        user = user or self.env.user
        self.filtered(lambda m: m.direction == "incoming").write({
            "read_user_ids": [(4, user.id)],
        })
        self.env["prema.inbox.conversation"].browse(
            self.mapped("conversation_id").ids)._broadcast_read_change()
        return True

    def mark_unread(self, user=None):
        user = user or self.env.user
        self.write({"read_user_ids": [(3, user.id)]})
        self.env["prema.inbox.conversation"].browse(
            self.mapped("conversation_id").ids)._broadcast_read_change()
        return True

    @api.model
    def _unread_message_ids(self, user=None):
        """Raw SQL: ids of incoming messages the user has not read.

        Kept on purpose in SQL (NOT EXISTS on the read rel) so the badge
        count stays correct even with thousands of messages and is immune
        to record-rule/ORM overhead.
        """
        user = user or self.env.user
        self.env.cr.execute(
            """
            SELECT m.id
              FROM prema_inbox_message m
             WHERE m.direction = 'incoming'
               AND NOT EXISTS (
                    SELECT 1 FROM prema_inbox_message_read_rel r
                     WHERE r.message_id = m.id
                       AND r.user_id = %s)
            """, (user.id,))
        return [r[0] for r in self.env.cr.fetchall()]

    @api.model
    def _unread_counts(self, users=None):
        """{user_id: {'total': n, 'load_board': n, 'spam': n}} for the users."""
        users = users or self.env["res.users"].search([])
        res = {}
        for u in users:
            self.env.cr.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE c.is_spam = FALSE)             AS total,
                  COUNT(*) FILTER (WHERE c.is_spam = FALSE
                                   AND m.is_load_board = TRUE)          AS load_board,
                  COUNT(*) FILTER (WHERE c.is_spam = TRUE)              AS spam
                FROM prema_inbox_message m
                JOIN prema_inbox_conversation c ON c.id = m.conversation_id
                WHERE m.direction = 'incoming'
                  AND NOT EXISTS (
                    SELECT 1 FROM prema_inbox_message_read_rel r
                     WHERE r.message_id = m.id AND r.user_id = %s)
                """, (u.id,))
            row = self.env.cr.fetchone()
            res[u.id] = {
                "total": row[0] or 0,
                "load_board": row[1] or 0,
                "spam": row[2] or 0,
            }
        return res

    # ------------------------------------------------------------------
    # outbound (compose / reply / forward / note)
    # ------------------------------------------------------------------
    @api.model
    def _new_outbound(
            self, conversation, subject, body_html, author=None,
            to_partners=None, cc_partners=None, attachment_ids=None,
            parent=None, kind="compose"):
        """Create an outgoing record (never sent outside UAT interception).

        kind: compose | reply | reply_all | forward | note
        Outgoing carries its own fresh Message-ID; replies chain References
        so the fetch path can thread replies back into THIS conversation.
        """
        author = author or self.env.user.partner_id
        vals = {
            "conversation_id": conversation.id,
            "direction": "note" if kind == "note" else "outgoing",
            "date": fields.Datetime.now(),
            "author_id": author.id,
            "author_internal": self.env.user.id,
            "email_from": "dispatcher@logistics.premafirm.com",
            "subject": subject or conversation.name,
            "body": body_html,
            "message_id": "<%s@prema-inbox.premafirm.com>"
                          % uuid.uuid4().hex,
            "attachment_ids": [(6, 0, attachment_ids or [])],
            "outbound_state": "draft",
        }
        if to_partners:
            vals["recipient_ids"] = [(6, 0, to_partners)]
        if cc_partners:
            vals["cc_ids"] = [(6, 0, cc_partners)]
        if parent:
            # Thread chaining — never group by subject alone.
            refs = []
            if parent.references:
                refs.append(parent.references)
            if parent.message_id:
                refs.append(parent.message_id)
            vals["references"] = " ".join("<%s>" % r.strip("<>")
                                          for r in refs)
            vals["in_reply_to"] = "<%s>" % parent.message_id.strip("<>")
        return self.create(vals)

    def _set_outbound_state(self, state, error=None):
        self.write({"outbound_state": state,
                    "send_error": error or self.send_error})
        self.conversation_id._broadcast_read_change()
        return True

    def send(self):
        """Send via the production mail pipeline — or intercept in UAT.

        prema_inbox.intercept_outgoing = "1" (default, UAT) records the
        message as intercepted and never touches SMTP. "0" (production)
        builds a real mail.mail through the configured outgoing server,
        From dispatcher@logistics.premafirm.com with Reply-To pinned to
        the same address, preserving Message-ID / References so replies
        thread back into this conversation.

        Status is honest end to end: draft → pending (queued with the mail
        gateway) → sent | failed. A message is 'sent' only after the SMTP
        server accepted it; any exception maps to 'failed' with the reason.
        """
        Mail = self.env["mail.mail"]
        for msg in self:
            if msg.direction != "outgoing":
                continue
            if msg.outbound_state == "sent":
                # Idempotent — retrying a sent message never duplicates.
                continue
            intercept = self.env["ir.config_parameter"].sudo().get_param(
                "prema_inbox.intercept_outgoing", "1")
            if intercept == "1":
                msg._set_outbound_state("intercepted")
                continue
            # Honest pre-flight: a message with nobody to send to is a
            # failure, not a silent success.
            missing = msg.recipient_ids.filtered(
                lambda p: not (p.email or "").strip())
            if not msg.recipient_ids:
                msg._set_outbound_state(
                    "failed", error="No recipient set — nothing was sent")
                continue
            if missing:
                msg._set_outbound_state(
                    "failed",
                    error=("Recipient(s) without an email address: %s"
                           % ", ".join(missing.mapped("name"))))
                continue
            try:
                # Odoo 18 has no in_reply_to on mail.mail/mail.message —
                # References alone carries the thread chain in the SMTP
                # headers, and the reply-back path threads on it.
                mail = Mail.create({
                    "subject": msg.subject or "",
                    "body_html": msg.body or "",
                    "email_from": "dispatcher@logistics.premafirm.com",
                    "reply_to": "dispatcher@logistics.premafirm.com",
                    "recipient_ids": [(6, 0, msg.recipient_ids.ids)],
                    "references": msg.references or "",
                    "message_id": msg.message_id or "",
                    "model": self._name,
                    "res_id": msg.id,
                    "auto_delete": False,
                })
                # Queued with the gateway — the honest intermediate state.
                msg._set_outbound_state("pending")
                mail.send(raise_exception=True)
                if mail.state == "sent":
                    msg._set_outbound_state("sent")
                else:
                    # e.g. no deliverable recipient → mail left 'outgoing'
                    # or marked 'exception' by the gateway, no SMTP flight.
                    msg._set_outbound_state(
                        "failed",
                        error=mail.failure_reason
                        or ("mail.mail state %s — not delivered"
                            % mail.state))
            except Exception as exc:
                # MailDeliveryException (connect/send) or anything else:
                # the message was NOT delivered — say so, never 'sent'.
                msg._set_outbound_state("failed", error=str(exc)[:1024])
        return True


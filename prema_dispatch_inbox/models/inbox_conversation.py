# -*- coding: utf-8 -*-
"""prema.inbox.conversation — ONE canonical thread per customer-topic.

Dedupe by RFC 5322 Message-ID (unique constraint on the message table);
threading by References/In-Reply-To — never by subject alone. The
workflow_state (open/waiting/completed/archived) is shared, while personal
read state is per-message/per-user on prema.inbox.message.read — reading is
not completing.

Real-time: every user of the inbox group subscribes to the bus channel
prema_inbox:{uid}; new-message events and read-state changes are broadcast
post-commit only.
"""
import re

from odoo import api, fields, models

_EMAIL_ADDR_RE = re.compile(r"<([^<>]+@[^<>]+)>")

# Keyword → category guess at ingest. Deliberately conservative: anything
# that does not match stays "other" and the AI assistant / dispatcher can
# re-categorize. Never more than a hint — never a hard rule.
_CATEGORY_KEYWORDS = {
    "quote_request": [
        "quote", "quotation", "rate", "price", "pricing", "cost", "how much",
        "cotation", "tariff", "what would it cost",
    ],
    "load_opportunity": [
        "load available", "available load", "capacity", "freight available",
        "need a truck", "need truck", "looking for a truck", "shipment available",
        "cover this load", "can you cover",
    ],
    "active_shipment": [
        "tracking", "status", "delivery", "pod", "proof of delivery",
        "where is my", "when will it arrive", "arrival", "delivered",
    ],
    "invoice_question": [
        "invoice", "payment", "bill", "statement", "paid", "charge",
    ],
    "carrier_alert": [
        "alert", "rmis", "contact change", "address changed", "phone number",
        "email address was changed", "notification",
    ],
}

# Spam domains (ICP prema_inbox.spam_domains, comma-separated). The filter
# is OFF by default (prema_inbox.spam_filter_active) — conservative default.
_DEFAULT_SPAM_DOMAINS = ""


class InboxConversation(models.Model):
    _name = "prema.inbox.conversation"
    _description = "Dispatch Inbox Conversation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "last_message_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(string="Subject", required=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", index=True,
        help="External counterparty. One canonical partner per conversation.")
    category = fields.Selection([
        ("quote_request", "Quote Request"),
        ("load_opportunity", "Load Opportunity"),
        ("active_shipment", "Active Shipment"),
        ("waiting_reply", "Waiting for Reply"),
        ("invoice_question", "Invoice Question"),
        ("carrier_alert", "Carrier Alert"),
        ("needs_review", "Needs Review"),
        ("other", "Other"),
    ], string="Category", default="other", index=True)
    workflow_state = fields.Selection([
        ("open", "Open"),
        ("waiting", "Waiting"),
        ("completed", "Completed"),
        ("archived", "Archived"),
    ], string="State", default="open", index=True)
    priority = fields.Selection([
        ("normal", "Normal"),
        ("urgent", "Urgent"),
        ("emergency", "Emergency"),
    ], string="Priority", default="normal")
    assignee_id = fields.Many2one(
        "res.users", string="Assignee", index=True,
        help="Dispatcher responsible. Unassigned conversations show in the "
             "Unassigned queue.")
    last_message_date = fields.Datetime(
        string="Last message", index=True,
        help="Date of the most recent message (any direction). Stored so "
             "folder queues can sort without N+1 computes.")
    unread_count = fields.Integer(
        string="Unread for me", compute="_compute_unread_count",
        help="Incoming messages the CURRENT user has not read. Personal, "
             "per user — not shared.")
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="set null")
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Dispatch job", ondelete="set null")
    opportunity_id = fields.Many2one(
        "crm.lead", string="Opportunity", ondelete="set null",
        help="Optional explicit link — never auto-created per email.")
    invoice_id = fields.Many2one(
        "account.move", string="Invoice", ondelete="set null",
        help="Read-only link for invoice questions. No posting, no payments.")
    is_spam = fields.Boolean(string="Spam", default=False, index=True)
    muted_user_ids = fields.Many2many(
        "res.users", "prema_inbox_conversation_muted_rel",
        "conversation_id", "user_id", string="Muted for",
        help="Muted conversations keep their unread count but suppress "
             "toast/sound for these users.")
    ai_extraction = fields.Json(string="AI extraction")
    ai_status = fields.Selection([
        ("none", "Not processed"),
        ("processing", "Processing"),
        ("ready", "Extraction ready"),
        ("failed", "Extraction failed"),
    ], string="AI status", default="none")
    price_snapshot = fields.Json(
        string="Price snapshot",
        help="Immutable pricing result (price_lines + route_snapshot + "
             "calculated_price) from PricingService.calculate — the single "
             "pricing authority. Never AI-invented.")
    inbox_message_ids = fields.One2many(
        "prema.inbox.message", "conversation_id", string="Messages")
    is_load_board = fields.Boolean(
        string="Load board alerts only", compute="_compute_is_load_board",
        help="True when every incoming message is a load-board/carrier alert.")

    # ------------------------------------------------------------------
    # unread / folder helpers
    # ------------------------------------------------------------------
    @api.depends("inbox_message_ids", "inbox_message_ids.direction",
                 "inbox_message_ids.read_user_ids")
    def _compute_unread_count(self):
        uid = self.env.uid
        for conv in self:
            conv.unread_count = sum(
                1 for m in conv.inbox_message_ids
                if m.direction == "incoming" and uid not in m.read_user_ids.ids)

    @api.depends("inbox_message_ids.direction", "inbox_message_ids.is_load_board")
    def _compute_is_load_board(self):
        for conv in self:
            conv.is_load_board = bool(conv.inbox_message_ids) and all(
                m.is_load_board for m in conv.inbox_message_ids)

    @api.model
    def _unread_counts_for_user(self):
        """Badge reconcile RPC: {total, load_board, spam} for the caller."""
        return self.env["prema.inbox.message"]._unread_counts(
            [self.env.user])

    # ------------------------------------------------------------------
    # ingest — the canonical entry point for incoming email
    # ------------------------------------------------------------------
    @api.model
    def _ingest_email(
            self, email_from, to_addrs, subject, body_html, body_plain,
            message_id, references=None, in_reply_to=None,
            attachment_ids=None, is_load_board=False):
        """Create-or-thread an incoming message.

        Dedupe: message_id unique constraint on the message table (duplicate
        deliveries raise IntegrityError → caught here, treated as no-op).
        Threading: References/In-Reply-To are matched against existing
        inbox_message_ids — never by subject alone. Without a match a new
        conversation is created.

        Returns (message, conversation, created_bool). Broadcasts happen in
        the CALLER after commit (fetch-sim controller / future fetch path).
        """
        Message = self.env["prema.inbox.message"]

        # 1) dedupe by Message-ID (normalized, without angle brackets)
        mid = (message_id or "").strip("<> ").strip()
        if not mid:
            mid = _synthetic_message_id()
        dup = Message.search([("message_id", "=", mid)], limit=1)
        if dup:
            return dup, dup.conversation_id, False

        # 2) thread-match by References / In-Reply-To
        conversation = self._find_by_references(references, in_reply_to)

        # 3) partner resolution (display name + email)
        partner = self._resolve_partner(email_from)

        # 4) new conversation unless a thread matched
        created = not bool(conversation)
        if not conversation:
            conversation = self.create({
                "name": subject or "(no subject)",
                "partner_id": partner.id,
                "category": self._guess_category(subject or "", body_plain or ""),
                "is_spam": self._is_spam_email(partner),
            })

        # 5) the message itself
        message = Message.create({
            "conversation_id": conversation.id,
            "direction": "incoming",
            "date": fields.Datetime.now(),
            "author_id": partner.id,
            "email_from": (email_from or "").strip(),
            "recipient_ids": [(6, 0, [p.id for p in
                                      self._resolve_partners(to_addrs)])],
            "subject": subject,
            "body": body_html or "",
            "body_plain": body_plain or "",
            "message_id": mid,
            "references": references or "",
            "in_reply_to": in_reply_to or "",
            "attachment_ids": [(6, 0, attachment_ids or [])],
            "is_load_board": is_load_board,
        })
        conversation._touch()
        return message, conversation, created

    @api.model
    def _find_by_references(self, references=None, in_reply_to=None):
        """Conversation owning a message whose Message-ID appears in the
        reference chain. Returns empty recordset when nothing matches."""
        ids = set()
        for chunk in (references or "").replace(",", " ").split():
            ids.add(chunk.strip("<> "))
        for mid in (in_reply_to or "").replace(",", " ").split():
            ids.add(mid.strip("<> "))
        ids.discard("")
        if not ids:
            return self.env["prema.inbox.conversation"]
        msgs = self.env["prema.inbox.message"].search(
            [("message_id", "in", sorted(ids))], limit=1)
        return msgs.conversation_id

    @api.model
    def _resolve_partner(self, email_from):
        """Find or create the res.partner for an RFC 5322 address."""
        email = _email_of(email_from)
        if not email:
            return self.env.ref("base.public_partner")
        display = re.sub(r"<[^<>]*>", "", email_from or "").strip(" \"'")
        partner = self.env["res.partner"].search(
            [("email", "=ilike", email)], limit=1)
        if not partner:
            partner = self.env["res.partner"].create({
                "name": display or email,
                "email": email,
            })
        return partner

    @api.model
    def _resolve_partners(self, addrs):
        return [self._resolve_partner(a) for a in addrs or []]

    @api.model
    def _guess_category(self, subject, body_plain):
        hay = ("%s %s" % (subject or "", body_plain or "")).lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(k in hay for k in keywords):
                return category
        return "other"

    @api.model
    def _is_spam_email(self, partner):
        """Conservative spam check — OFF unless prema_inbox.spam_filter_active.

        Domains come from ICP prema_inbox.spam_domains (comma-separated).
        Nothing is auto-deleted: spam lands in the Spam/Quarantine folder.
        """
        active = self.env["ir.config_parameter"].sudo().get_param(
            "prema_inbox.spam_filter_active", "0")
        if active != "1":
            return False
        domains = [
            d.strip().lower() for d in
            self.env["ir.config_parameter"].sudo().get_param(
                "prema_inbox.spam_domains", _DEFAULT_SPAM_DOMAINS).split(",")
            if d.strip()
        ]
        email = (partner.email or "").lower()
        return any(email.endswith("@" + d) for d in domains)

    def _touch(self):
        """Refresh last_message_date from the newest message."""
        for conv in self:
            newest = conv.inbox_message_ids.sorted(key=lambda m: m.date, reverse=True)
            conv.last_message_date = newest[0].date if newest else fields.Datetime.now()
        return True

    # ------------------------------------------------------------------
    # realtime broadcast (post-commit, from the caller)
    # ------------------------------------------------------------------
    def _inbox_group_users(self):
        group = self.env.ref(
            "prema_dispatch_inbox.group_dispatch_inbox", raise_if_not_found=False)
        return group.users if group else self.env["res.users"]

    def _broadcast(self, event_type, extra=None):
        """Send one event per inbox-group user on prema_inbox:{uid}."""
        payload = dict(extra or {})
        payload.update({"type": event_type, "conversation_id": self.id})
        for user in self._inbox_group_users():
            counts = self.env["prema.inbox.message"]._unread_counts([user])
            payload["unread_total"] = counts[user.id]["total"]
            payload["muted"] = user.id in self.muted_user_ids.ids
            payload["is_spam"] = self.is_spam
            # Odoo 18 bus: _sendone(target, notification_type, message);
            # buffered in cr.precommit, flushed after commit.
            self.env["bus.bus"]._sendone(
                "prema_inbox:%d" % user.id, "prema_inbox", dict(payload))
        return True

    def _broadcast_new_message(self, message):
        self._broadcast("new_message", {
            "event_id": _uuid(),
            "message_id": message.id,
            "category": self.category,
            "from_email": message.email_from or "",
            "subject": self.name,
        })

    def _broadcast_read_change(self):
        self._broadcast("read_change", {"event_id": _uuid()})

    # ------------------------------------------------------------------
    # folder queues (computed domains — no table)
    # ------------------------------------------------------------------
    @api.model
    def _folder_domain(self, key):
        base = [("is_spam", "=", False)]
        if key == "inbox":
            return base + [("workflow_state", "=", "open")]
        if key == "needs_review":
            return base + [("workflow_state", "=", "open"),
                           ("category", "=", "needs_review")]
        if key == "quote_requests":
            return base + [("category", "=", "quote_request")]
        if key == "load_opportunities":
            return base + [("category", "=", "load_opportunity")]
        if key == "active_shipments":
            return base + [("category", "=", "active_shipment")]
        if key == "waiting_reply":
            return base + [("workflow_state", "=", "waiting")]
        if key == "archived":
            return [("workflow_state", "=", "archived")]
        if key == "spam":
            return [("is_spam", "=", True)]
        # unread / tasks / drafts / sent are computed sets — no domain
        return []

    @api.model
    def _folder_conversations(self, key):
        """Folder contents. 'unread'/'tasks'/'drafts'/'sent' are computed
        (personal unread can never be a stored domain)."""
        convs = self.search(
            self._folder_domain(key), order="last_message_date desc, id desc")
        if key == "unread":
            convs = convs.filtered(lambda c: c.unread_count > 0)
        elif key == "tasks":
            activities = self.env["mail.activity"].search([
                ("res_model", "=", "prema.inbox.conversation"),
            ])
            convs = convs & self.browse(
                activities.mapped("res_id"))
        elif key in ("drafts", "sent"):
            msg_states = ["draft"] if key == "drafts" else ["sent", "intercepted"]
            msgs = self.env["prema.inbox.message"].search([
                ("direction", "=", "outgoing"),
                ("outbound_state", "in", msg_states),
            ])
            convs = convs & self.browse(msgs.mapped("conversation_id").ids)
        return convs

    @api.model
    def inbox_folders(self):
        """[{key, label, count}] for the folder column — counts only."""
        labels = {
            "inbox": "Inbox", "unread": "Unread",
            "needs_review": "Needs Review",
            "quote_requests": "Quote Requests",
            "load_opportunities": "Load Opportunities",
            "active_shipments": "Active Shipments",
            "waiting_reply": "Waiting for Reply", "tasks": "Tasks",
            "drafts": "Drafts", "sent": "Sent", "archived": "Archived",
            "spam": "Spam / Quarantine",
        }
        return [{"key": k, "label": v, "count": len(self._folder_conversations(k))}
                for k, v in labels.items()]

    @api.model
    def inbox_conversations(self, folder, search=None, limit=100):
        """Conversation list rows for one folder (+ optional text search)."""
        convs = self._folder_conversations(folder)
        if search:
            q = search.strip()
            if q:
                matching_msgs = self.env["prema.inbox.message"].search([
                    "|",
                    ("subject", "ilike", q),
                    ("body_plain", "ilike", q),
                ])
                convs = convs.filtered(
                    lambda c: (q.lower() in (c.name or "").lower())
                    or (c.partner_id.name or "").lower().find(q.lower()) >= 0
                    or c.id in matching_msgs.conversation_id.ids)
        rows = []
        for conv in convs[:limit]:
            rows.append(self._conversation_row(conv))
        return rows

    @api.model
    def _conversation_row(self, conv):
        last = conv.inbox_message_ids.sorted(
            key=lambda m: m.date, reverse=True)[:1]
        return {
            "id": conv.id,
            "name": conv.name,
            "partner_id": conv.partner_id.id,
            "partner_name": conv.partner_id.name,
            "category": conv.category,
            "priority": conv.priority,
            "workflow_state": conv.workflow_state,
            "assignee_id": conv.assignee_id.id,
            "assignee_name": conv.assignee_id.name,
            "last_message_date": last[0].date.isoformat() if last else None,
            "unread_count": conv.unread_count,
            "is_spam": conv.is_spam,
            "is_load_board": conv.is_load_board,
            "has_attachment": bool(conv.inbox_message_ids.attachment_ids),
            "booking_id": conv.booking_id.id,
            "job_id": conv.job_id.id,
            "invoice_id": conv.invoice_id.id,
            "opportunity_id": conv.opportunity_id.id,
        }

    @api.model
    def inbox_conversation_detail(self, conversation_id):
        """One conversation with its full message history (the C3 pane)."""
        conv = self.browse(conversation_id)
        if not conv or not self.env.user.has_group(
                "prema_dispatch_inbox.group_dispatch_inbox"):
            return {}
        msgs = []
        for m in conv.inbox_message_ids.sorted(key=lambda m: m.date):
            msgs.append({
                "id": m.id,
                "direction": m.direction,
                "date": m.date.isoformat(),
                "author_name": m.author_id.name or m.email_from,
                "email_from": m.email_from,
                "subject": m.subject,
                "body": m.body or "",
                "body_plain": m.body_plain or "",
                "is_read": m.is_read,
                "outbound_state": m.outbound_state,
                "attachments": [{"id": a.id, "name": a.name,
                                 "mimetype": a.mimetype}
                                for a in m.attachment_ids],
            })
        return {
            "conversation": self._conversation_row(conv),
            "messages": msgs,
            "ai": {
                "status": conv.ai_status,
                "extraction": conv.ai_extraction,
            },
            "pricing": conv.price_snapshot,
        }

    @api.model
    def inbox_link_candidates(self, model, conversation_id, search=None):
        """Explicit business-link search: logistics.booking | prema.dispatch.job
        | account.move | crm.lead — read-only, never auto-created."""
        conv = self.browse(conversation_id)
        partner = conv.partner_id
        allowed = {
            "booking": "logistics.booking",
            "job": "prema.dispatch.job",
            "invoice": "account.move",
            "opportunity": "crm.lead",
        }
        model_name = allowed.get(model)
        if not model_name:
            return []
        domain = []
        if search:
            domain.append(("name", "ilike", search))
        if model == "opportunity":
            domain = [("partner_id", "=", partner.id)]
            if search:
                domain.append(("name", "ilike", search))
        else:
            domain.insert(0, ("partner_id", "=", partner.id))
        records = self.env[model_name].search(
            domain, limit=20,
            order="create_date desc" if model != "invoice" else "invoice_date desc nulls last")
        return [{"id": r.id, "name": r.name,
                 "state": getattr(r, "state", None)}
                for r in records]

    def action_link_record(self, model, record_id):
        field = {
            "booking": "booking_id", "job": "job_id",
            "invoice": "invoice_id", "opportunity": "opportunity_id",
        }.get(model)
        if not field:
            return False
        self.write({field: record_id})
        return True

    # ------------------------------------------------------------------
    # AI / pricing entry points (thin RPC wrappers for the frontend)
    # ------------------------------------------------------------------
    def inbox_ai_action(self, action, instruction=None):
        """summarize | draft_reply | extract | suggest_follow_up"""
        ai = self.env["prema.inbox.ai"]
        if action == "extract":
            return ai.extract_shipment(self)
        if action == "summarize":
            return {"text": ai.summarize(self)}
        if action == "draft_reply":
            return {"text": ai.draft_reply(self, instruction or "")}
        if action == "suggest_follow_up":
            return {"suggestion": ai.suggest_follow_up(self)}
        return {}

    def inbox_calculate_price(self):
        """Review & calculate quote — the AI panel's pricing card.

        One click = extract (if not done yet) + run the deterministic
        PricingService. Never invents a number: engine verdicts only.
        """
        if self.ai_status == "none":
            self.env["prema.inbox.ai"].extract_shipment(self)
        return self.env["prema.inbox.pricing"].calculate_price(self)

    def compose_and_send(self, subject, body, kind, send_now=False):
        """One RPC for the composer: create (+ optionally send) an outbound.

        kind: compose | reply | reply_all | forward | note
        send_now=False → draft (lands in the Drafts folder, autosaved).
        send_now=True → immediately runs the safe send (intercepted in UAT).
        """
        parent = self.inbox_message_ids.sorted(
            key=lambda m: m.date, reverse=True)[:1]
        msg = self.env["prema.inbox.message"]._new_outbound(
            self, subject=subject, body_html=body,
            parent=parent or None, kind=kind)
        if send_now:
            msg.send()
        return {"id": msg.id, "outbound_state": msg.outbound_state}

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------
    def action_complete(self):
        self.write({"workflow_state": "completed"})
        return True

    def action_reopen(self):
        self.write({"workflow_state": "open"})
        return True

    def action_archive_thread(self):
        self.write({"workflow_state": "archived"})
        return True

    def action_toggle_mute(self, user=None):
        user = user or self.env.user
        for conv in self:
            if user.id in conv.muted_user_ids.ids:
                conv.muted_user_ids = [(3, user.id)]
            else:
                conv.muted_user_ids = [(4, user.id)]
        return True



def _email_of(addr):
    m = _EMAIL_ADDR_RE.search(addr or "")
    if m:
        return m.group(1).strip().lower()
    return (addr or "").strip().lower()


def _synthetic_message_id():
    return "%s@prema-inbox" % _uuid()


def _uuid():
    import uuid
    return uuid.uuid4().hex

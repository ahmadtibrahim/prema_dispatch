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
import base64
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import email_normalize, email_split, html2plaintext
from odoo.tools.mail import html_sanitize

_EMAIL_ADDR_RE = re.compile(r"<([^<>]+@[^<>]+)>")

# RFC 5322 reply/forward subject prefixes (en/fr re, fwd, fw; de aw/antw;
# sv sv/vs; pl odp; it rif; pt enc; no/da sv). Stacked prefixes collapse to
# a single one — "Re: Re: quote" never grows "Re: Re: Re: …".
_THREAD_PREFIX_RE = re.compile(
    r"^\s*((re|fwd|fw|aw|sv|vs|antw|r|odp|rif|enc)\s*(\[[0-9]+\])?\s*:\s*)+",
    re.I)


def _normalize_thread_subject(subject):
    """Strip stacked reply/forward prefixes — "Re: Re: quote" → "quote".

    Used for reply subject defaults so repeated replies never stack
    "Re: Re: Re: …". Non-prefixed subjects pass through verbatim.
    """
    cleaned = _THREAD_PREFIX_RE.sub("", subject or "").strip()
    return cleaned or (subject or "").strip()

# PremaFirm-internal email suffixes. Recipient defaults (reply / reply all)
# and internal-address exclusion are decided on these — a PremaFirm address
# is never a reply recipient, and the dispatcher's own address never lands
# back in To/Cc.
_INTERNAL_EMAIL_SUFFIXES = ("@premafirm.com", "@logistics.premafirm.com")

# Remote-content strip: tracking pixels and remote images must not load
# from an email (mixed content / privacy). Anything not a data: URI is
# removed from the sanitized HTML.
_REMOTE_IMG_SRC = re.compile(r'src\s*=\s*("(?!(?:data:))[^"]*"|\'(?!(?:data:))[^\']*\')', re.I)
_REMOTE_BG_SRC = re.compile(r'background(-image)?\s*:\s*url\([^)]*\)', re.I)


def _sanitize_email_html(src):
    """Server-side sanitizer for UNTRUSTED incoming email HTML.

    html_sanitize removes scripts, event handlers (on*), javascript:/data:
    hrefs, <style> content and forms. On top of that, any img src that is
    not an embedded data: URI is stripped (remote tracking pixels must not
    load) and CSS background-image url() rules are dropped. The result is
    the ONLY form in which incoming HTML is stored or rendered.
    """
    if not src:
        return ""
    cleaned = html_sanitize(
        src, sanitize_attributes=True, strip_style=True, strip_classes=True)
    cleaned = _REMOTE_IMG_SRC.sub("", cleaned)
    cleaned = _REMOTE_BG_SRC.sub("", cleaned)
    return cleaned


def _html_to_plain(html):
    """Safe plain-text fallback for quoting/display when body_plain is
    missing — never echoes raw HTML."""
    return (html2plaintext(html or "") or "").strip()

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
        help="External counterparty. One canonical partner per conversation. "
             "When the customer is a company, this is the COMPANY "
             "(commercial partner); the individual person goes on "
             "contact_id — the same convention as crm.lead "
             "(partner_id / logistics_contact_id).")
    contact_id = fields.Many2one(
        "res.partner", string="Contact", index=True,
        help="The individual person when the customer is a company — the "
             "commercial partner of partner_id. Set from the sender's exact "
             "email match; never guessed.")
    partner_provisional = fields.Boolean(
        string="Customer not confirmed", default=False,
        help="True when the sender's email matches MULTIPLE records — no "
             "automatic association was made (a wrong customer is a "
             "high-severity error). The dispatcher must confirm.")
    partner_suggestions = fields.Json(
        string="Ambiguous sender matches",
        help="[{id, name, email, reason}] candidate partners when "
             "partner_provisional is True.")
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
    ai_summary = fields.Text(
        string="AI summary",
        help="Latest thread summary produced by the AI assistant. Stored on "
             "the conversation so the summary survives page reloads and is "
             "visible in the panel without re-running the model.")
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
            attachment_ids=None, is_load_board=False, date=None,
            reply_to=None):
        """Create-or-thread an incoming message.

        Dedupe: message_id unique constraint on the message table (duplicate
        deliveries raise IntegrityError → caught here, treated as no-op).
        Threading: References/In-Reply-To are matched against existing
        inbox_message_ids — never by subject alone. Without a match a new
        conversation is created.

        Partner: deterministic exact-email resolution (`_resolve_sender`) —
        never a domain/name/address/tag/AI guess; an ambiguous match leaves
        the conversation provisional for the dispatcher to confirm.

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

        # 3) partner resolution — deterministic exact-email chain (D-4):
        #    ambiguous → provisional, dispatcher confirms; never guessed.
        resolved = self._resolve_sender(email_from)
        partner = resolved["partner"]
        contact = resolved["contact"]

        # 4) new conversation unless a thread matched
        created = not bool(conversation)
        if not conversation:
            conversation = self.create({
                "name": subject or "(no subject)",
                "partner_id": partner.id if partner else False,
                "contact_id": contact.id if contact else False,
                "partner_provisional": resolved["provisional"],
                "partner_suggestions": resolved["suggestions"] or False,
                "category": self._guess_category(subject or "", body_plain or ""),
                "is_spam": bool(partner) and self._is_spam_email(partner),
            })
        elif not conversation.partner_id and partner:
            # a threaded reply into an as-yet-unidentified conversation can
            # now resolve it — same deterministic chain, no guessing
            conversation.write({
                "partner_id": partner.id,
                "contact_id": contact.id if contact else False,
                "partner_provisional": resolved["provisional"],
                "partner_suggestions": resolved["suggestions"] or False,
            })

        # 5) the message itself — body_html is UNTRUSTED email content and
        # is sanitized ONCE at ingest; only the sanitized form is ever
        # stored, served, or rendered (no scripts / on* / javascript: hrefs
        # / remote images / tracking pixels).
        message = Message.create({
            "conversation_id": conversation.id,
            "direction": "incoming",
            "date": date or fields.Datetime.now(),
            "author_id": partner.id if partner else False,
            "email_from": (email_from or "").strip(),
            "reply_to_header": (reply_to or "").strip(),
            # Reply-To identity is resolved HERE, at ingest, in the
            # fetchmail/admin context — never inside the read-only reply
            # composer RPC (a dispatcher without res.partner create rights
            # must still be able to open a thread).
            "reply_to_partner_id": (reply_to or "").strip()
            and self._resolve_partner(reply_to).id or False,
            "recipient_ids": [(6, 0, [p.id for p in
                                      self._resolve_partners(to_addrs)])],
            "subject": subject,
            "body": _sanitize_email_html(body_html or ""),
            "body_plain": body_plain or "",
            "message_id": mid,
            "references": references or "",
            "in_reply_to": in_reply_to or "",
            "attachment_ids": [(6, 0, attachment_ids or [])],
            "is_load_board": is_load_board,
        })
        conversation._touch()
        return message, conversation, created

    # ------------------------------------------------------------------
    # fetchmail gateway — message_process route (mail.alias + object_id)
    # ------------------------------------------------------------------
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Entry point for fetchmail (and the mail.alias route) ingestion.

        The mail gateway's message_process parses the raw RFC 822 into a
        msg_dict and routes here (message_route → _message_route_process).
        Everything delegates to _ingest_email — same dedupe by Message-ID,
        same threading by References, same categorization — so the fetch
        path and the UAT fetch-sim produce identical inbox rows.

        The bus broadcast is issued inside this transaction (bus._sendone
        buffers in cr.precommit); the fetchmail cron commits per message,
        which flushes the bus rows — mirrors the sim controller's
        broadcast-then-commit pattern. Returns the conversation so
        _message_route_process can post the ledger mail.message.
        """
        custom_values = custom_values or {}
        subject = msg_dict.get("subject") or ""
        body = msg_dict.get("body") or ""
        message_id = msg_dict.get("message_id") or ""
        attachments = self._attachments_from_msgdict(msg_dict)
        # Reply-To: Odoo 18's gateway msg_dict has NO reply_to key (verified
        # mail_thread.py); the raw RFC 822 object survives as msg_dict["msg"]
        # — parse the header there. The fetch-sim / tests may pass reply_to
        # directly.
        reply_to = msg_dict.get("reply_to") or ""
        if not reply_to:
            raw_msg = msg_dict.get("msg")
            if raw_msg is not None and hasattr(raw_msg, "get"):
                reply_to = raw_msg.get("Reply-To") or ""
        # A message_id we already know → a duplicate delivery: ingest will
        # no-op, and we must NOT announce it again. Anything else (new
        # thread OR reply into an existing thread) is a real new message.
        mid = (message_id or "").strip("<> ").strip()
        known = self.env["prema.inbox.message"].search(
            [("message_id", "=", mid)], limit=1)
        # body_plain: the gateway's own text/plain part wins — it is what
        # the MTA actually received (a message with NO html part carries
        # only that plain text). When absent, derive the fallback from the
        # SANITIZED html, never the raw body: the plain view must not echo
        # script text that the sanitizer stripped from the html view.
        plain = msg_dict.get("body_plain") or ""
        if not plain.strip():
            plain = html2plaintext(_sanitize_email_html(body))
        msg, conv, created = self._ingest_email(
            email_from=msg_dict.get("email_from") or "",
            to_addrs=email_split(msg_dict.get("to") or ""),
            subject=subject,
            body_html=body,
            body_plain=plain,
            message_id=message_id,
            references=msg_dict.get("references") or "",
            in_reply_to=msg_dict.get("in_reply_to") or "",
            attachment_ids=[a.id for a in attachments],
            is_load_board=self._looks_like_load_board(subject, body),
            date=msg_dict.get("date") or None,
            reply_to=reply_to,
        )
        # Files are created before the message row (res_id unknown); now
        # that the message exists, bind them to it.
        if attachments:
            attachments.write({"res_model": "prema.inbox.message",
                               "res_id": msg.id})
        if not known:
            # New message — announce it (new thread or reply into an
            # existing one). Dedupe re-deliveries never rebroadcast.
            conv._broadcast_new_message(msg)
        return conv

    @api.model
    def message_update(self, msg_dict, update_vals=None):
        """Message threading a known thread: same ingest, same dedupe.

        The message_route machinery already matched the References chain to
        a ledger mail.message of this model; _ingest_email finds the same
        conversation via _find_by_references (or dedupes an exact
        re-delivery). Returns the conversation record.
        """
        return self.message_new(msg_dict, update_vals)

    @api.model
    def _attachments_from_msgdict(self, msg_dict):
        """Materialize msg_dict attachments as ir.attachment rows.

        The real gateway (mail.message_process → _message_parse_extract_
        payload) delivers namedtuples (fname, content, info) — three
        elements, content possibly bytes — while the fetch-sim and tests
        may deliver plain (name, content) pairs; both shapes are accepted.
        Rows are created without a res_id and rebound to the inbox message
        after it exists.

        Two content shapes need care:
          * ``original_email.eml`` — appended by _message_parse_extract_
            payload when the server has save_original set: its content is
            the raw RFC822 source as a PLAIN STRING, not base64. Storing it
            verbatim as ``datas`` crashes the attachment base64 round-trip
            (binascii "Incorrect padding") and the ledger mail.message
            already carries it, so it is skipped here.
          * plain-text str content (fixtures/sim) — base64-encoded so it
            survives the ``datas`` round-trip; a str that IS valid base64
            (the fetch-sim shape) is stored as-is.
        """
        vals_list = []
        for item in msg_dict.get("attachments") or []:
            name, content = item[0], item[1]
            if not name or not content:
                continue
            if name == "original_email.eml":
                continue  # core bookkeeping; see docstring
            data = content
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("utf-8")
            elif not _looks_base64_text(data):
                data = base64.b64encode(data.encode("utf-8")).decode("utf-8")
            vals_list.append({
                "name": name,
                "datas": data,
                "res_model": False,
                "res_id": False,
            })
        return self.env["ir.attachment"].create(vals_list)

    @api.model
    def _looks_like_load_board(self, subject, body):
        """Conservative load-board heuristic (mirrors the fetch-sim).

        Load-board alerts are machine traffic — the badge keeps them in
        their own bucket, never in the personal unread count.
        """
        hay = ("%s %s" % (subject or "", body or "")).upper()
        return "RMIS" in hay

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
        # Match BOTH storage forms: incoming message_ids are stored bare,
        # outgoing ones historically kept the angle brackets (fixed at
        # _new_outbound) — replies to a sent message must still thread.
        bare = sorted(ids)
        bracketed = ["<%s>" % i for i in bare]
        msgs = self.env["prema.inbox.message"].search(
            ["|", ("message_id", "in", bare),
             ("message_id", "in", bracketed)], limit=1)
        return msgs.conversation_id

    @api.model
    def _find_partner_by_email(self, email, limit=None):
        """Exact lookup on the NORMALIZED email — the canonical pattern used
        across premafirm modules (prema_mail_tracking, crm_bulk_email).
        Deterministic: the normalized address equals the partner's stored
        normalized email. Never a domain / name / address similarity match."""
        norm = email_normalize(email or "")
        if not norm:
            return self.env["res.partner"]
        return self.env["res.partner"].search(
            [("email_normalized", "=", norm)], limit=limit or 1)

    @api.model
    def _resolve_partner(self, email_from):
        """Find-or-create the res.partner for a TYPED address (composer
        To/Cc, message recipient list). Exact normalized-email match first,
        else a NEW partner from the address itself — derived from the
        address alone, never a domain/name/address/tag/AI guess."""
        email = _email_of(email_from)
        if not email:
            return self.env.ref("base.public_partner")
        display = re.sub(r"<[^<>]*>", "", email_from or "").strip(" \"'")
        partner = self._find_partner_by_email(email)
        if not partner:
            partner = self.env["res.partner"].create({
                "name": display or email,
                "email": email,
            })
        return partner

    @api.model
    def _partner_candidates_from_records(self, email):
        """Deterministic partner evidence from raw-email records: prior
        conversations and CRM leads carrying this EXACT email address.

        No domain-only / name-similarity / address-similarity / tag / AI
        evidence is ever considered — a wrong customer association is a
        high-severity error.
        """
        candidates = self.env["res.partner"]
        for m in self.env["prema.inbox.message"].search(
                [("email_from", "ilike", email)], limit=200):
            if _email_of(m.email_from or "") != email:
                continue
            if m.conversation_id.partner_id:
                candidates |= m.conversation_id.partner_id
        if "crm.lead" in self.env:
            for lead in self.env["crm.lead"].search(
                    [("email_from", "ilike", email)], limit=100):
                if _email_of(lead.email_from or "") != email:
                    continue
                p = lead.partner_id or lead.logistics_contact_id
                if p:
                    candidates |= p
        return candidates

    @api.model
    def _resolve_sender(self, email_from):
        """Deterministic sender resolution for INCOMING mail (D-4).

        Evidence chain — every step is an exact-email match on records that
        carry this very address, in priority order:
          1. a res.partner with the exact normalized email. A contact at a
             company resolves to company + contact — the crm.lead
             convention: partner_id = COMPANY, contact_id = the person.
          2. prior inbox conversations whose messages carry this exact
             email_from → their partner (the same person wrote before).
          3. CRM leads whose email_from is exactly this address → their
             partner / logistics contact.
          4. nothing at all → a NEW partner from the sender's own display
             name + email (derived from the message itself).

        Multiple DISTINCT candidates → NO automatic association: partner
        False + provisional True + suggestions for the dispatcher to
        confirm (`action_confirm_partner`).

        Returns {"partner", "contact", "provisional", "suggestions"}.
        """
        empty = {"partner": False, "contact": False,
                 "provisional": False, "suggestions": False}
        email = _email_of(email_from)
        if not email:
            return empty
        display = re.sub(r"<[^<>]*>", "", email_from or "").strip(" \"'")
        candidates = self._find_partner_by_email(email, limit=50)
        if not candidates:
            candidates = self._partner_candidates_from_records(email)
        if len(candidates) == 1:
            p = candidates
            company = p.commercial_partner_id or p
            return {
                "partner": company,
                "contact": p if company != p else False,
                "provisional": False,
                "suggestions": False,
            }
        if len(candidates) > 1:
            return {
                "partner": False,
                "contact": False,
                "provisional": True,
                "suggestions": [{
                    "id": p.id,
                    "name": p.name or p.email,
                    "email": p.email or email,
                    "reason": ("Contact at %s" % p.parent_id.name)
                              if p.parent_id and p.parent_id.name
                              else "Partner",
                } for p in candidates[:10]],
            }
        partner = self.env["res.partner"].create({
            "name": display or email,
            "email": email,
        })
        return {"partner": partner, "contact": False,
                "provisional": False, "suggestions": False}

    @api.model
    def _resolve_partners(self, addrs):
        return [self._resolve_partner(a) for a in addrs or []]

    def action_confirm_partner(self, partner_id=None):
        """Dispatcher resolves an ambiguous sender match (provisional).

        partner_id False → "leave unassigned": clears the flag WITHOUT any
        association. Otherwise sets partner (the company / commercial
        partner) + contact (the confirmed record when it is a child
        contact)."""
        if partner_id:
            partner = self.env["res.partner"].browse(int(partner_id))
            if not partner.exists():
                raise ValidationError(_("This partner no longer exists."))
            company = partner.commercial_partner_id or partner
            self.write({
                "partner_id": company.id,
                "contact_id": partner.id if company != partner else False,
                "partner_provisional": False,
                "partner_suggestions": False,
            })
        else:
            self.write({
                "partner_provisional": False,
                "partner_suggestions": False,
            })
        self._broadcast_read_change()
        return True

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
        """Send one event per inbox-group user on prema_inbox:{uid}.

        Recordset-safe: a multi-conversation read (e.g. mark_read over
        messages from several conversations) emits one event per
        conversation instead of crashing on a singleton read.
        """
        for conv in self:
            payload = dict(extra or {})
            payload.update({"type": event_type, "conversation_id": conv.id})
            for user in self._inbox_group_users():
                counts = self.env["prema.inbox.message"]._unread_counts(
                    [user])
                payload["unread_total"] = counts[user.id]["total"]
                payload["muted"] = user.id in conv.muted_user_ids.ids
                payload["is_spam"] = conv.is_spam
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
    def _safe_link_name(self, record):
        """Name of a linked business record, or "" when the caller lacks
        read access to that model.

        Odoo 18 raises the model-level ACL check even when reading the name
        of a NULL link, so an unreadable link target would otherwise take
        the WHOLE conversation list down with it ("Could not load
        conversations"). The list must degrade per-link, never 500.
        """
        try:
            return record.name or ""
        except AccessError:
            return ""

    @api.model
    def _conversation_row(self, conv):
        last = conv.inbox_message_ids.sorted(
            key=lambda m: m.date, reverse=True)[:1]
        return {
            "id": conv.id,
            "name": conv.name,
            "partner_id": conv.partner_id.id,
            "partner_name": conv.partner_id.name,
            "partner_email": conv.partner_id.email or "",
            "contact_id": conv.contact_id.id,
            "contact_name": conv.contact_id.name or "",
            "partner_provisional": conv.partner_provisional,
            "partner_suggestions": conv.partner_suggestions or False,
            "customer_label": (
                "%s / %s" % (conv.partner_id.name, conv.contact_id.name)
                if conv.partner_id and conv.contact_id
                else (conv.partner_id.name or "")),
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
            "booking_name": self._safe_link_name(conv.booking_id),
            "job_id": conv.job_id.id,
            "job_name": self._safe_link_name(conv.job_id),
            "invoice_id": conv.invoice_id.id,
            "invoice_name": self._safe_link_name(conv.invoice_id),
            "opportunity_id": conv.opportunity_id.id,
            "opportunity_name": self._safe_link_name(conv.opportunity_id),
        }

    @api.model
    def inbox_conversation_detail(self, conversation_id):
        """One conversation with its full message history (the C3 pane).

        Returns {} when the conversation no longer exists (or the caller is
        not in the inbox group) — the frontend shows "Conversation no longer
        exists." instead of crashing.
        """
        conv = self.browse(conversation_id)
        if not conv.exists() or not self.env.user.has_group(
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
                "send_error": m.send_error or "",
                "to": [{"id": p.id, "email": p.email or p.name}
                       for p in m.recipient_ids],
                "cc": [{"id": p.id, "email": p.email or p.name}
                       for p in m.cc_ids],
                "attachments": [{"id": a.id, "name": a.name,
                                 "mimetype": a.mimetype}
                                for a in m.attachment_ids],
            })
        drafts = conv.inbox_message_ids.filtered(
            lambda m: m.direction == "outgoing"
            and m.outbound_state == "draft").sorted(key=lambda m: m.date)
        # defaults are computed on the CONVERSATION (not the empty model
        # recordset — self here is @api.model)
        to_default, cc_default = conv._default_reply_recipients("reply")
        to_default_all, cc_default_all = conv._default_reply_recipients(
            "reply_all")
        return {
            "conversation": self._conversation_row(conv),
            "messages": msgs,
            "drafts": [{
                "id": d.id,
                "kind": d.kind or "compose",
                "subject": d.subject or "",
                "body": d.body or "",
                "body_plain": d.body_plain or "",
                "to": [{"id": p.id, "email": p.email or p.name}
                       for p in d.recipient_ids],
                "cc": [{"id": p.id, "email": p.email or p.name}
                       for p in d.cc_ids],
                "attachments": [{"id": a.id, "name": a.name,
                                 "mimetype": a.mimetype}
                                for a in d.attachment_ids],
                "date": d.date.isoformat(),
            } for d in drafts],
            "reply_defaults": {
                "to": [{"id": p.id, "email": p.email or p.name}
                       for p in conv.env["res.partner"].browse(to_default)],
                "cc": [],
                "subject": "Re: %s" % _normalize_thread_subject(conv.name),
            },
            "reply_all_defaults": {
                "to": [{"id": p.id, "email": p.email or p.name}
                       for p in conv.env["res.partner"].browse(to_default_all)],
                "cc": [{"id": p.id, "email": p.email or p.name}
                       for p in conv.env["res.partner"].browse(cc_default_all)],
                "subject": "Re: %s" % _normalize_thread_subject(conv.name),
            },
            "muted": self.env.user.id in conv.muted_user_ids.ids,
            "ai": {
                "status": conv.ai_status,
                "summary": conv.ai_summary or "",
                "extraction": conv.ai_extraction,
            },
            "pricing": conv.price_snapshot,
        }

    @api.model
    def _link_authorized_models(self):
        return {
            "booking": "logistics.booking",
            "job": "prema.dispatch.job",
            "invoice": "account.move",
            "opportunity": "crm.lead",
        }

    @api.model
    def _link_field_map(self):
        return {
            "booking": "booking_id", "job": "job_id",
            "invoice": "invoice_id", "opportunity": "opportunity_id",
        }

    @api.model
    def _link_legitimate_partner_ids(self, conv):
        """Partner ids the conversation is legitimately about: the
        conversation partner itself PLUS its commercial partner / parent
        company (a contact at a company links to records of the company,
        and vice versa)."""
        ids = set()
        partner = conv.partner_id
        if partner:
            ids.add(partner.id)
            if partner.commercial_partner_id:
                ids.add(partner.commercial_partner_id.id)
        return ids

    @api.model
    def inbox_link_candidates(
            self, model, conversation_id, search=None, manual=False):
        """Business-link candidates for the picker.

        Automatic scope: records whose partner (or commercial partner /
        parent company) is the conversation's partner hierarchy — a contact
        at a company sees the company's bookings, invoices, jobs and
        opportunities. No domain auto-association: the inbox never guesses
        records for a partner the customer did not identify as theirs.

        manual=True (checkbox, dispatcher-authorized): free text search
        across record names AND customer names — the explicit escape hatch
        for genuinely cross-customer lookups. The picker shows
        "No records found for this contact/company." when both come up
        empty.
        """
        conv = self.browse(conversation_id)
        if not conv.exists():
            return []
        model_name = self._link_authorized_models().get(model)
        if not model_name:
            return []
        q = (search or "").strip()
        if manual and q:
            # dispatcher-authorized manual search: record name OR any
            # customer whose name contains the text
            customers = self.env["res.partner"].search(
                [("name", "ilike", q)])
            domain = ["|", "|",
                      ("name", "ilike", q),
                      ("partner_id", "in", customers.ids),
                      ("partner_id.commercial_partner_id", "in",
                       customers.ids)]
        else:
            partner_ids = self._link_legitimate_partner_ids(conv)
            if not partner_ids:
                return []
            domain = ["|",
                      ("partner_id", "in", list(partner_ids)),
                      ("partner_id.commercial_partner_id", "in",
                       list(partner_ids))]
            if q:
                domain = [domain, ("name", "ilike", q)]
        records = self.env[model_name].search(
            domain, limit=20,
            order="create_date desc" if model != "invoice"
            else "invoice_date desc nulls last")
        return [{
            "id": r.id, "name": r.name or "%s #%s" % (model, r.id),
            "partner_id": r.partner_id.id,
            "partner_name": r.partner_id.name or "",
            "state": getattr(r, "state", None),
        } for r in records]

    def action_link_record(self, model, record_id, search=None):
        """Link a record to the conversation — authorized server-side.

        The record must (a) be one of the whitelisted link models, (b)
        still exist, (c) be readable by the caller under record rules, and
        (d) belong to the conversation's partner hierarchy OR have been
        surfaced by an authorized manual search that actually matches it.
        There is no path for arbitrary RPC linking.
        """
        field = self._link_field_map().get(model)
        model_name = self._link_authorized_models().get(model)
        if not field or not model_name:
            raise ValidationError(_("Unsupported link type: %s") % model)
        rec = self.env[model_name].browse(int(record_id))
        if not rec.exists():
            raise ValidationError(_("The record no longer exists."))
        try:
            rec.check_access_rights("read")
            rec.check_access_rule("read")
        except Exception:
            raise ValidationError(
                _("You do not have access to this record."))
        conv = self[0] if self else self.env["prema.inbox.conversation"]
        partner_ids = self._link_legitimate_partner_ids(conv)
        rec_partner_ids = {rec.partner_id.id}
        if rec.partner_id.commercial_partner_id:
            rec_partner_ids.add(rec.partner_id.commercial_partner_id.id)
        legitimate = bool(partner_ids & rec_partner_ids)
        if not legitimate:
            q = (search or "").strip()
            # Mirror the picker's manual-search domain (inbox_link_candidates
            # matches record name OR partner / commercial partner name) —
            # otherwise a cross-customer record surfaced by CUSTOMER name
            # (e.g. "Other Produce" → booking B-UAT-OTHER) could be shown
            # in the picker but rejected on link. The query must still
            # actually match the record: no blind linking.
            ql = (q or "").lower()
            names = ((rec.name or ""), (rec.partner_id.name or ""),
                     (rec.partner_id.commercial_partner_id.name or ""))
            if not ql or not any(ql in n.lower() for n in names):
                raise ValidationError(
                    _("Not a valid candidate for this conversation — use "
                      "the picker search to find records first."))
        self.write({field: rec.id})
        self._broadcast_read_change()
        return True

    def action_unlink_record(self, model):
        """Remove a business link (the X on the chip)."""
        field = self._link_field_map().get(model)
        if not field:
            return False
        self.write({field: False})
        self._broadcast_read_change()
        return True

    # ------------------------------------------------------------------
    # AI / pricing entry points (thin RPC wrappers for the frontend)
    # ------------------------------------------------------------------
    def inbox_ai_action(self, action, instruction=None):
        """summarize | draft_reply | extract | suggest_follow_up

        AI NEVER sends: summarize persists the text to the conversation
        (visible in the panel), draft_reply returns text the dispatcher
        pastes/edits in the composer, extract persists normalized fields.
        """
        ai = self.env["prema.inbox.ai"]
        if action == "extract":
            return ai.extract_shipment(self)
        if action == "summarize":
            text = ai.summarize(self)
            self.write({"ai_summary": text})
            return {"text": text}
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

    # ------------------------------------------------------------------
    # composer — the SINGLE RPC behind every composer action
    # ------------------------------------------------------------------
    @api.model
    def _is_internal_recipient(self, partner):
        """True for PremaFirm addresses and for partners without an email.

        Reply recipients default to the external counterparty — internal
        addresses and empty partners are never reply defaults."""
        email = (partner.email or "").strip().lower()
        if not email:
            return True
        return email.endswith(_INTERNAL_EMAIL_SUFFIXES)

    def _latest_incoming(self):
        """The newest incoming message of this thread (the thing a Reply
        answers). Same-second messages tie on date — id breaks the tie, so
        a reply answers the truly last message."""
        return self.inbox_message_ids.filtered(
            lambda m: m.direction == "incoming"
        ).sorted(key=lambda m: (m.date, m.id), reverse=True)[:1]

    def _default_reply_recipients(self, kind):
        """(to_ids, cc_ids) for reply / reply_all — the D-3 safety chain.

        Reply recipient resolution, in order:
          1. the latest incoming message's Reply-To header — the sender's
             EXPLICIT redirect — when the address is external;
          2. the latest incoming message's external From author;
          3. the conversation partner / contact — ONLY when that record has
             an external email AND the relationship is unambiguous (the
             sender is internal/unknown — a colleague forwarding on the
             customer's behalf — or the sender's email IS this record).

        If nothing resolves, [] is returned and compose_and_send refuses
        with a clear error: the inbox NEVER guesses a recipient merely to
        avoid an empty-recipient error.

        reply_all: the resolved sender + the message's external To/Cc
        recipients (internal PremaFirm addresses excluded, deduped).
        """
        to_ids, cc_ids = [], []
        last = self._latest_incoming()

        sender_id = False
        # 1) Reply-To header — the sender's explicit redirect. Identity was
        #    resolved at ingest (reply_to_partner_id); for messages ingested
        #    before that field existed, resolve EXISTING partners only —
        #    this RPC is read-only and must never create one.
        if last and (last.reply_to_header or "").strip():
            rt = last.reply_to_partner_id or self._find_partner_by_email(
                _email_of(last.reply_to_header))
            if rt.id and not self._is_internal_recipient(rt):
                sender_id = rt.id
        # 2) external From author
        if not sender_id and last and last.author_id \
                and not self._is_internal_recipient(last.author_id):
            sender_id = last.author_id.id
        # 3) conversation partner/contact — external email AND unambiguous
        #    relationship only (contact first: the person, then the company).
        #    An empty thread has no sender to be ambiguous about — the
        #    dispatcher's own association is the relationship.
        if not sender_id:
            candidate = self.contact_id or self.partner_id
            sender_is_external = bool(
                last and last.author_id
                and not self._is_internal_recipient(last.author_id))
            if candidate.id \
                    and not self._is_internal_recipient(candidate) \
                    and (not sender_is_external
                         or last.author_id.id == candidate.id):
                sender_id = candidate.id

        if sender_id:
            to_ids.append(sender_id)
        if kind == "reply_all":
            seen = set(to_ids)
            for p in last.recipient_ids:
                if p.id and p.id not in seen \
                        and not self._is_internal_recipient(p):
                    to_ids.append(p.id)
                    seen.add(p.id)
            for p in last.cc_ids:
                if p.id and p.id not in seen \
                        and not self._is_internal_recipient(p):
                    cc_ids.append(p.id)
        return to_ids, cc_ids

    def compose_and_send(
            self, subject, body, kind, send_now=False,
            to_partner_ids=None, cc_partner_ids=None, attachment_ids=None,
            draft_id=None):
        """One RPC for the whole composer: reply / reply-all / forward /
        compose (new email) / internal note, as a draft or sent immediately.

        kind: compose | reply | reply_all | forward | note

        * REPLY / REPLY ALL: recipients default to the sender (+ external
          To/Cc) of the latest incoming email — internal PremaFirm
          addresses are excluded. The caller may override with
          to_partner_ids / cc_partner_ids (partner ids OR email strings).
        * COMPOSE (New email): always creates its OWN conversation — it is
          never silently attached to the currently selected thread.
        * INTERNAL NOTE: direction=note, zero mail.mail, zero SMTP.
        * DRAFT: pass draft_id to resume/update the SAME message — editing
          a draft never creates a second message.
        * SEND: rejects an empty recipient with a clear ValidationError
          (client and server both validate).

        Returns {id, conversation_id, outbound_state} for the created/
        updated message.
        """
        if kind not in ("compose", "reply", "reply_all", "forward", "note"):
            raise ValidationError(_("Unknown composer action: %s") % kind)

        # ---- resume vs fresh ----------------------------------------
        message = self.env["prema.inbox.message"]
        if draft_id:
            message = message.browse(int(draft_id))
            if not message.exists():
                raise ValidationError(_("Draft no longer exists."))
            if message.direction != "outgoing" \
                    or message.outbound_state != "draft":
                raise ValidationError(
                    _("This message is no longer a draft."))
            if message.author_internal.id != self.env.user.id:
                raise ValidationError(_("Only the author can edit this draft."))
        else:
            message = self.env["prema.inbox.message"]

        # ---- internal note: immediate, never emailed -----------------
        if kind == "note":
            if not (body or "").strip():
                raise ValidationError(_("The note is empty."))
            if message:
                message.write({"body": body or "", "outbound_state": "note"})
            else:
                message = self.env["prema.inbox.message"]._new_outbound(
                    self, subject="", body_html=body,
                    parent=None, kind="note")
                # _new_outbound always creates a draft; a note is its own
                # terminal state (never drafted, never sent)
                message.outbound_state = "note"
            self._touch()
            self._broadcast_read_change()
            return {"id": message.id, "conversation_id": self.id,
                    "outbound_state": message.outbound_state}

        # ---- recipients (ids or email strings, client-validated too) -
        to_ids = self._normalize_recipient_list(to_partner_ids or [])
        cc_ids = self._normalize_recipient_list(cc_partner_ids or [])
        if kind in ("reply", "reply_all") and not to_ids:
            to_ids, cc_ids = self._default_reply_recipients(kind)
        if send_now and not to_ids:
            if kind in ("reply", "reply_all"):
                raise ValidationError(_(
                    "No reply recipient — the sender has no resolvable "
                    "external email address. Add the customer's email "
                    "manually before sending."))
            raise ValidationError(
                _("No recipient — add the customer's email address before sending."))

        # ---- subject defaults (thread prefixes never stack) ---------
        subject = (subject or "").strip()
        if not subject and kind in ("reply", "reply_all"):
            subject = "Re: %s" % _normalize_thread_subject(self.name)
        elif not subject and kind == "forward":
            subject = "Fwd: %s" % self.name

        # ---- quoted body (only when not already quoted) --------------
        body = (body or "").rstrip()
        last_in = self._latest_incoming()
        if kind in ("reply", "reply_all", "forward") and last_in \
                and "wrote:" not in body:
            body = "%s\n\nOn %s, %s wrote:\n%s" % (
                body, last_in.date.strftime("%a, %b %d, %Y %H:%M")
                if last_in.date else "a previous message",
                last_in.author_id.name or last_in.email_from,
                last_in.body_plain or _html_to_plain(last_in.body))

        # ---- conversation: compose makes its OWN thread --------------
        # New email NEVER attaches to the currently selected conversation —
        # it always gets a fresh thread (resuming a draft keeps the draft's
        # own conversation via `message`).
        if kind == "compose" and not message:
            conv = self.create({
                "name": subject or "(no subject)",
                "partner_id": to_ids[0] if to_ids else False,
            })
        else:
            conv = self

        # ---- save (update-in-place for drafts) or create -------------
        if message:
            message.write({
                "subject": subject,
                "body": body,
                # the thread renders body_plain — without this, editing a
                # resumed draft shows the ORIGINAL text after send (the
                # stored body_plain is never refreshed)
                "body_plain": _html_to_plain(body),
                "recipient_ids": [(6, 0, to_ids)],
                "cc_ids": [(6, 0, cc_ids)],
                "attachment_ids": [(6, 0, attachment_ids or [])],
                "outbound_state": "draft",
            })
            message._rebind_attachments()
        else:
            parent = conv._latest_incoming() if kind in ("reply", "reply_all") \
                else None
            message = self.env["prema.inbox.message"]._new_outbound(
                conv, subject=subject, body_html=body,
                to_partners=to_ids, cc_partners=cc_ids,
                attachment_ids=attachment_ids, parent=parent, kind=kind)

        if send_now:
            message.send()
        conv._touch()
        return {"id": message.id, "conversation_id": conv.id,
                "outbound_state": message.outbound_state}

    @api.model
    def _normalize_recipient_list(self, values):
        """Accept partner ids (int) or email strings; resolve emails via the
        canonical partner resolver. Returns partner ids."""
        ids = []
        for value in values or []:
            if isinstance(value, int):
                ids.append(value)
                continue
            email = _email_of(str(value))
            if not email:
                continue
            ids.append(self._resolve_partner(email).id)
        return [i for i in dict.fromkeys(ids) if i]

    def discard_draft(self, message_id):
        """Delete a draft message (author only). Returns True."""
        msg = self.env["prema.inbox.message"].browse(int(message_id))
        if not msg.exists() or msg.direction != "outgoing" \
                or msg.outbound_state != "draft":
            return False
        if msg.author_internal.id != self.env.user.id:
            raise ValidationError(_("Only the author can discard this draft."))
        conv = msg.conversation_id
        msg.unlink()
        if conv and not conv.inbox_message_ids:
            conv.unlink()  # an empty shell has nothing to follow up
        return True

    def retry_send(self, message_id):
        """Re-run the safe send for one failed message — idempotent
        (a message already sent is never re-sent)."""
        msg = self.env["prema.inbox.message"].browse(int(message_id))
        if not msg.exists() or msg.direction != "outgoing":
            raise ValidationError(_("Message no longer exists."))
        if msg.outbound_state in ("sent", "intercepted"):
            return {"id": msg.id, "outbound_state": msg.outbound_state}
        if not msg.recipient_ids:
            raise ValidationError(
                _("No recipient — add the customer's email address before sending."))
        msg.send()
        return {"id": msg.id, "outbound_state": msg.outbound_state,
                "send_error": msg.send_error or ""}

    # ------------------------------------------------------------------
    # assignment — inbox responsibility only (never crm.lead.user_id)
    # ------------------------------------------------------------------
    @api.model
    def inbox_assign_candidates(self):
        """Dispatchers who may own inbox responsibility: members of the
        inbox group first, internal users as fallback. Never a full
        directory dump."""
        group = self.env.ref(
            "prema_dispatch_inbox.group_dispatch_inbox", raise_if_not_found=False)
        users = group.users if group else self.env["res.users"]
        if not users:
            users = self.env["res.users"].search([("share", "=", False)])
        return [{
            "id": u.id,
            "name": u.name or u.login,
            "login": u.login,
        } for u in users.sorted(key=lambda u: (u.name or u.login).lower())]

    def action_assign(self, user_id=None):
        """Set/clear the inbox assignee. ONLY the conversation's
        assignee_id changes — crm.lead.user_id is never touched. Every
        change is audited as an internal note on the thread."""
        user_id = int(user_id) if user_id else False
        assignee = self.env["res.users"].browse(user_id).exists() \
            if user_id else self.env["res.users"]
        for conv in self:
            changed = conv.assignee_id.id != (assignee.id if assignee else False)
            conv.write({"assignee_id": assignee.id if assignee else False})
            if changed:
                conv.message_post(
                    body=_("Inbox assignee → <b>%s</b> (set by %s).") % (
                        assignee.name or "unassigned",
                        self.env.user.name),
                    subtype_xmlid="mail.mt_note")
        self._broadcast_read_change()
        return True

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


def _looks_base64_text(s):
    """True when s is canonical base64 (decode → re-encode round-trips).

    Attachment content from the fetch-sim is base64 text; raw RFC822 /
    plain-text content is not — the distinction decides whether the value
    can be stored verbatim as ``datas`` or must be encoded first.
    """
    if not s or len(s) % 4:
        return False
    try:
        return (base64.b64encode(base64.b64decode(s, validate=True))
                .decode("utf-8") == s)
    except Exception:
        return False


def _synthetic_message_id():
    return "%s@prema-inbox" % _uuid()


def _uuid():
    import uuid
    return uuid.uuid4().hex

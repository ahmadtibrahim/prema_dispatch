"""Workflow cleanup §7/§8/§9 — the opportunity sees the whole conversation.

A quotation is ONE outbound document. It is sent from the Sales quotation
(native *Send by Email*, with the quotation PDF), and the opportunity is
where the conversation lives — so the opportunity must show both the send
and whatever comes back, without a second email ever leaving the building.

The rules this module enforces:

* **One outbound email only.** Everything mirrored onto the opportunity is
  an internal `mail.mt_note` — it notifies nobody and mails nobody. The
  customer-facing `mail.mail` the Sales composer queued stays the single
  message the customer receives (§8). Nothing here calls a send. This is
  why the mirror is a NOTE and not a re-post: `_get_recipient_data` is what
  decides who hears about a message, and the internal subtype is the only
  thing in it that keeps a `partner_share` follower — the customer — out.
* **Both directions, told apart.** `_crm_mirror_direction` decides which
  way a message travelled; the send and the reply are filed with different
  headings, different authors and different `message_type`s, because the
  AI engine reads those to decide who owes whom (§8).
* **No duplicated binaries.** The quotation PDF and every attachment are
  re-linked, not re-uploaded: the copy carries the same checksum, so
  Odoo's filestore serves the SAME stored file for both records (the
  mechanism `_copy_evidence_to_invoice` already relies on). CRM
  attachment visibility therefore costs no second copy of the bytes (§9).

The mirror is idempotent per source message: a synthetic Message-ID naming
the source message is the dedupe key, so a re-post, a retry or a double
hook can never show the opportunity the same message twice. Being
synthetic also means it can never collide with a real Message-ID — which
matters because core dedupes inbound mail on that field, and a mirror that
reused the customer's own id would make Odoo drop a genuine re-delivery.
"""

import logging

from markupsafe import Markup

from odoo import _, models

_logger = logging.getLogger(__name__)

# The dedupe key is the note's Message-ID header — a real indexed column,
# invisible in the chatter, and it survives having no attachments at all
# (which an attachment-description stamp would not). An HTML comment in the
# body was tried first and rejected: the composer's sanitizer escapes it, so
# the marker showed up as visible text in the opportunity's chatter.
_MIRROR_STAMP = "<crm-mirror-%s@premafirm>"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _message_post_after_hook(self, message, msg_values):
        result = super()._message_post_after_hook(message, msg_values)
        try:
            self._mirror_quotation_send_to_crm(message)
        except Exception:  # noqa: BLE001 — a mirror must never fail the send
            _logger.exception(
                "CRM mirror of quotation message %s failed; the quotation "
                "itself was sent and is unaffected.", message.id)
        return result

    def _mirror_quotation_send_to_crm(self, message):
        """Mirror one quotation-thread message onto the opportunity (§8)."""
        for order in self:
            lead = getattr(order, "opportunity_id", False)
            if not lead:
                continue
            direction = self._crm_mirror_direction(order, message)
            if not direction:
                continue
            if self._crm_mirror_done(order, message):
                continue
            self._post_crm_mirror_note(order, lead, message, direction)

    def _crm_mirror_direction(self, order, message):
        """``"out"``, ``"in"`` or None — which way this message travelled.

        Both directions matter and they must never be confused. A send the
        customer never received must not be shown as one (the salesperson
        would stop chasing), and a customer's reply must not be filed as
        something we sent — so the wording, the audience and the truth all
        hang off this answer.

        * An internal note is neither: `mail.mt_note` is what every mirror
          and every human log note in this codebase uses, and it must stay
          out of the customer's record. This check also stops the mirror
          from mirroring itself — the note posted below is an mt_note, so
          the hook it re-triggers finds no direction and returns.
        * A `partner_share` author means somebody outside the company wrote
          it. This is checked FIRST because a reply also carries recipients
          (us), and the recipient test alone would misfile it as a send.
          `partner_share` is the AI engine's own inbound discriminator (its
          reply-status detector reads a comment with a share author as
          engagement), and it is deliberately not "the author is this
          order's customer": the customer's colleague replying from another
          address is still the customer replying.
        * Otherwise, recipients (`partner_ids`) mean it went to somebody —
          the composer posts the quotation with the customer in the
          audience, and `message_type` is deliberately NOT used to confirm
          it: the transport decides that value, so a send is recorded as
          'comment' whenever no mail server is reachable, and CRM
          visibility would then depend on the mail server's health.
        """
        note_subtype = self.env.ref("mail.mt_note", raise_if_not_found=False)
        if note_subtype and message.subtype_id == note_subtype:
            return None
        author = message.author_id
        if author and author.partner_share:
            return "in"
        if message.partner_ids:
            return "out"
        return None

    def _recipients(self, message):
        """The addresses the quotation actually went to, for the note.

        The message's `partner_ids` are the notification audience, which
        also carries our own alias (notifications@…) — the real addressees
        are on the `mail.mail` row the message was delivered through."""
        mail = self.env["mail.mail"].sudo().search(
            [("mail_message_id", "=", message.id)], limit=1)
        if mail and mail.email_to:
            return mail.email_to
        return ", ".join(p.email for p in message.partner_ids if p.email)

    def _crm_mirror_done(self, order, message):
        """Already mirrored this exact message onto this opportunity?"""
        lead = order.opportunity_id
        return bool(self.env["mail.message"].sudo().search_count([
            ("model", "=", "crm.lead"), ("res_id", "=", lead.id),
            ("message_id", "=", _MIRROR_STAMP % message.id),
        ]))

    def _post_crm_mirror_note(self, order, lead, message, direction):
        """The opportunity's record of one side of the conversation.

        §7 asks the opportunity to show what the customer received; §8 asks
        it to show what the customer sent back. Both land here, and the
        heading, the author and the audience differ by direction — a send
        filed as a reply, or the reverse, would tell the salesperson the
        wrong thing about who owes whom.
        """
        if direction == "in":
            self._post_crm_reply_note(order, lead, message)
        else:
            self._post_crm_send_note(order, lead, message)

    def _post_crm_send_note(self, order, lead, message):
        """The opportunity's record of the send — internal, never emailed.

        Everything §7 asks the opportunity to show about the send lives on
        this one note: when it went, to whom, its subject, the body the
        customer actually read, the number of the document it carried, and
        (via `_mirror_attachments`) the same PDF and attachments.

        The heading names the ORDER rather than calling every mirrored
        email a quotation: this hook fires for any outbound message on the
        order — an invoice email included — and a note that called an
        invoice a quotation would be a lie in the customer's own record.

        The `message_type` is left at `message_post`'s own default (a
        notification) on purpose: the AI engine's reply-status detector
        reads an email-like message as OUTBOUND and would stamp the lead's
        outreach clock from this internal note, and its ML layer would
        learn an outreach example from it. The real send already stamped
        the order; this note exists to be read.
        """
        recipients = self._recipients(message)
        subject = (message.subject or "").strip()
        summary = _(
            "<b>%(name)s — email sent to the customer</b><br/>"
            "Date: %(date)s<br/>"
            "To: %(to)s<br/>"
            "Subject: %(subject)s<br/>"
            "Document: %(name)s (%(state)s)<br/>"
            "The customer's reply will land on this opportunity. "
            "One outbound email was sent; the message and its attachments "
            "are below.",
        )
        note = lead.message_post(
            # Markup is REQUIRED, not decorative: message_post escapes a
            # plain str body, and whether `_() % …` yields str or Markup
            # depends on the operand's shape — a difference nobody reading
            # this file could be expected to keep straight. Markup also
            # escapes the interpolated values, so a subject containing
            # angle brackets can never inject markup into the chatter.
            body=Markup(summary) % {
                "name": order.name,
                "state": order.state,
                "date": message.date.strftime("%Y-%m-%d %H:%M")
                if message.date else "—",
                "to": recipients or "—",
                "subject": subject or "(no subject)",
            } + Markup(message.body or ""),
            subtype_xmlid="mail.mt_note",
            message_id=_MIRROR_STAMP % message.id,
        )
        self._mirror_attachments(lead, message, note)

    def _post_crm_reply_note(self, order, lead, message):
        """The customer's reply, filed on the opportunity as theirs (§8).

        Posted as `message_type='comment'` under `mail.mt_note`, authored by
        the customer, which is deliberately the same shape the AI engine
        itself uses when a human threads an imported reply onto a lead. It
        is also the shape that engine's reply-status detector recognises as
        inbound engagement (`author.partner_share` on a comment), so a reply
        that arrived against the quotation stamps `last_inbound_at` /
        `normal_reply` and answers the waiting follow-up on the opportunity
        exactly as one that arrived against the lead would. That is what
        makes the reply actionable rather than merely visible.

        `mail.mt_note` is what keeps it silent — the internal subtype is the
        only thing that excludes the customer from `_get_recipient_data`, so
        this posts with no notification and no `mail.mail` row. The
        `message_type` is `comment` and NOT `email` on purpose: `email`
        would make the ML layer learn a synthetic `incoming_email` example
        from a message the customer never sent here, and it would make the
        reply-status detector test the note for outbound-ness instead.
        """
        author = message.author_id
        sender = message.email_from or (author.email if author else "")
        subject = (message.subject or "").strip()
        summary = _(
            "<b>%(name)s — reply received from the customer</b><br/>"
            "Date: %(date)s<br/>"
            "From: %(from)s<br/>"
            "Subject: %(subject)s<br/>"
            "The customer wrote back about this quotation; their message is "
            "below. The reply itself is on the Sales quotation.",
        )
        note = lead.message_post(
            body=Markup(summary) % {
                "name": order.name,
                "date": message.date.strftime("%Y-%m-%d %H:%M")
                if message.date else "—",
                "from": sender or "—",
                "subject": subject or "(no subject)",
            } + Markup(message.body or ""),
            subject=subject or False,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
            author_id=author.id if author else False,
            email_from=sender or False,
            message_id=_MIRROR_STAMP % message.id,
        )
        self._mirror_attachments(lead, message, note)

    def _mirror_attachments(self, lead, message, note):
        """Re-link the documents that actually travelled with the message.

        `message.attachment_ids` is the AUTHORITY, and it has to be: the
        Sales composer attaches the quotation PDF by creating the file
        against the COMPOSER and letting `message_post` transfer it to the
        record, so an `ir.attachment` search on the message finds nothing at
        all on a real Send by Email. The tempting fallback — "take the
        newest file off the order" — is worse than nothing (§9): it would
        hang whatever internal document happened to be newest off a
        customer's note, and on a reply it would put our own paperwork in
        the customer's mouth. The lookup below is a defensive union only;
        the relation is what the chatter itself displays.

        `copy()` keeps the checksum, so the filestore serves ONE file for
        both records — the opportunity gets the same PDF and the same
        attachments visible without a second copy of the bytes (§7, §9)."""
        attachments = message.attachment_ids | self.env["ir.attachment"].sudo(
        ).search([
            ("res_model", "=", "mail.message"), ("res_id", "=", message.id),
        ])
        for attachment in attachments:
            attachment.copy({
                "res_model": "mail.message",
                "res_id": note.id,
                "description": _MIRROR_STAMP % message.id,
            })


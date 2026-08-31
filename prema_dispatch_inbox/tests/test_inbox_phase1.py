# -*- coding: utf-8 -*-
"""Phase 1 regression suite — D-3 reply recipients/threading, D-2 safe
formatted HTML default, D-4 deterministic partner resolution.

Every scenario in the approved Phase 1 spec is covered here; the browser
UAT (resolution matrix, reply/reply-all flows, HTML security regression,
partner-match ambiguity) runs separately on the inbox_uat clone.
"""
from odoo.exceptions import ValidationError

from .common import InboxTestCase
from odoo.addons.prema_dispatch_inbox.models.inbox_conversation import (
    _normalize_thread_subject)


# ----------------------------------------------------------------------
# D-3 — Reply recipient safety chain + threading
# ----------------------------------------------------------------------
class TestReplyRecipientsD3(InboxTestCase):

    def test_reply_to_normal_external_sender(self):
        """Plain external sender → reply defaults to the author."""
        _, conv, _ = self.ingest()
        to_ids, cc_ids = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["bob@demo-toronto-produce.test"])
        self.assertEqual(cc_ids, [])

    def test_reply_to_header_wins_over_from(self):
        """Reply-To ≠ From → the reply defaults to the EXPLICIT Reply-To
        address (the sender's redirect), not the From author."""
        _, conv, _ = self.ingest(
            reply_to="Dispatch Desk <desk@forwarder.test>")
        to_ids, _ = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["desk@forwarder.test"])

    def test_reply_to_header_stored_on_message(self):
        """The Reply-To header survives ingestion on the message row."""
        msg, _, _ = self.ingest(reply_to="Desk <desk@forwarder.test>")
        self.assertEqual(msg.reply_to_header,
                         "Desk <desk@forwarder.test>")
        msg2, _, _ = self.ingest(message_id="<no-reply-to@x>")
        self.assertFalse(msg2.reply_to_header)

    def test_internal_forward_falls_back_to_conversation_partner(self):
        """Sender internal (PremaFirm) → step 3: the conversation partner
        with an external email is the reply recipient (a colleague
        forwarding on the customer's behalf)."""
        m1, conv, _ = self.ingest(subject="Quote")
        _, conv2, created = self.ingest(
            email_from="Colleague <colleague@premafirm.com>",
            subject="FW: Quote", message_id="<fwd-1@x>",
            references=m1.message_id)
        self.assertEqual(conv2.id, conv.id)
        self.assertFalse(created)
        to_ids, cc_ids = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["bob@demo-toronto-produce.test"])
        self.assertEqual(cc_ids, [])

    def test_reply_all_dedupes_and_excludes_internal(self):
        """reply_all = sender + external To/Cc; the dispatcher's own
        internal address is never a recipient, duplicates collapse."""
        _, conv, _ = self.ingest(subject="Rate quote")
        last = conv._latest_incoming()
        ext = self.env["res.partner"].create({
            "name": "Ext", "email": "ext@partner.test"})
        last.recipient_ids += ext
        to_ids, cc_ids = conv._default_reply_recipients("reply_all")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["bob@demo-toronto-produce.test", "ext@partner.test"])
        self.assertEqual(cc_ids, [])
        # internal To/Cc excluded, deduped against the sender
        internal = self.env["res.partner"].create({
            "name": "Dispatcher", "email": "dispatcher@logistics.premafirm.com"})
        last.recipient_ids += internal
        last.cc_ids += internal
        to_ids2, cc_ids2 = conv._default_reply_recipients("reply_all")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids2).mapped("email"),
            ["bob@demo-toronto-produce.test", "ext@partner.test"])
        self.assertEqual(cc_ids2, [])

    def test_catchall_alias_sender_gets_recipient(self):
        """Unknown external sender (catch-all) → a partner is created from
        the message itself and becomes the reply recipient."""
        _, conv, _ = self.ingest(
            email_from="Fred <fred@forwarder.test>", subject="Load board")
        to_ids, _ = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["fred@forwarder.test"])

    def test_quoted_display_name(self):
        """RFC 5322 display names containing commas parse correctly."""
        _, conv, _ = self.ingest(
            email_from="Doe, John <john@x.test>", subject="Quote")
        to_ids, _ = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["john@x.test"])

    def test_reply_uses_latest_incoming(self):
        """A reply answers the NEWEST incoming message of the thread."""
        m1, conv, _ = self.ingest(message_id="<a@x>", subject="Rate quote")
        _, conv2, _ = self.ingest(
            email_from="Carol <carol@second.test>", message_id="<b@x>",
            references="<a@x>", subject="Re: Rate quote")
        self.assertEqual(conv2.id, conv.id)
        to_ids, _ = conv._default_reply_recipients("reply")
        self.assertEqual(
            self.env["res.partner"].browse(to_ids).mapped("email"),
            ["carol@second.test"])

    def test_reply_subject_prefixes_never_stack(self):
        """Repeated replies never build "Re: Re: Re: …" — the thread
        subject is normalized for both detail defaults and compose."""
        _, conv, _ = self.ingest(subject="Re: quote", message_id="<s1@x>")
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertEqual(detail["reply_defaults"]["subject"], "Re: quote")
        self.assertEqual(detail["reply_all_defaults"]["subject"], "Re: quote")
        res = conv.compose_and_send("", "body", "reply", False)
        self.assertEqual(self.Message.browse(res["id"]).subject, "Re: quote")
        self.assertEqual(_normalize_thread_subject("Fw: Re: Re: quote"),
                         "quote")
        self.assertEqual(_normalize_thread_subject("Revenue: report"),
                         "Revenue: report")

    def test_no_resolvable_recipient_refused_with_clear_error(self):
        """Sender internal AND conversation partner without an email →
        nothing may be guessed: the server refuses with a clear message
        naming manual resolution; a draft is still allowed."""
        m1, conv, _ = self.ingest(subject="Quote")
        nobody = self.env["res.partner"].create({
            "name": "Nobody", "email": ""})
        conv.write({"partner_id": nobody.id})
        self.ingest(email_from="Colleague <colleague@premafirm.com>",
                    subject="FW: Quote", message_id="<fwd-2@x>",
                    references=m1.message_id)
        to_ids, _ = conv._default_reply_recipients("reply")
        self.assertEqual(to_ids, [])
        with self.assertRaises(ValidationError) as cm:
            conv.compose_and_send("Re: x", "body", "reply", True)
        self.assertIn("No reply recipient", str(cm.exception))
        self.assertIn("manually", str(cm.exception))
        res = conv.compose_and_send("Re: x", "body", "reply", False)
        self.assertEqual(self.Message.browse(res["id"]).outbound_state,
                         "draft")

    def test_thread_detail_read_never_creates_partner(self):
        """Opening a thread is a READ: resolving an unknown Reply-To must
        not create a partner inside the detail RPC (identity is resolved
        once, at ingest). A dispatcher without partner-create rights must
        still be able to open the thread — before the fix the detail RPC
        raised AccessError and the whole thread pane failed to load."""
        _, conv, _ = self.ingest(
            reply_to="Dispatch Desk <desk@forwarder.test>")
        restricted = self.make_user("ro.reply.viewer", group=True)
        restricted.write({"groups_id": [
            (4, self.env.ref("base.group_user").id)]})
        env_r = self.env(user=restricted.id)
        # precondition: the restricted user really cannot create partners
        with self.assertRaises(Exception):
            env_r["res.partner"].create({"name": "must not exist"})
        before = self.env["res.partner"].search_count(
            [("email", "=", "desk@forwarder.test")])
        detail = env_r["prema.inbox.conversation"].inbox_conversation_detail(
            conv.id)
        after = self.env["res.partner"].search_count(
            [("email", "=", "desk@forwarder.test")])
        self.assertEqual(after, before)  # read-only: no partner created
        self.assertEqual(detail["reply_defaults"]["to"][0]["email"],
                         "desk@forwarder.test")

    def test_reply_preserves_thread_headers(self):
        """Outbound replies carry their own Message-ID and chain
        References/In-Reply-To, so the customer's answer threads back into
        the SAME conversation (never a stray thread)."""
        m1, conv, _ = self.ingest(message_id="<orig-1@x>", subject="Quote")
        res = conv.compose_and_send("Re: Quote", "OK", "reply", True)
        out = self.Message.browse(res["id"])
        self.assertNotEqual(out.message_id, m1.message_id)
        self.assertIn("<orig-1@x>", out.references)
        self.assertEqual(out.in_reply_to, "<orig-1@x>")
        _, conv2, created = self.ingest(
            message_id="<back-1@x>", references=out.message_id,
            subject="Re: Quote")
        self.assertEqual(conv2.id, conv.id)
        self.assertFalse(created)


# ----------------------------------------------------------------------
# D-2 — sanitized HTML is the default body view (display flip only;
# the sanitizer itself is untouched)
# ----------------------------------------------------------------------
class TestHtmlDisplayD2(InboxTestCase):

    def test_sanitized_html_kept_and_served(self):
        """The sanitizer keeps tables/lists/links, strips scripts, event
        handlers, javascript: hrefs and remote images — and the stored,
        served body is exactly that sanitized form (display default)."""
        html = ('<table><tr><td>Lane</td><td>Rate</td></tr>'
                '<tr><td>TOR-BEL</td><td><b>$542</b></td></tr></table>'
                '<script>alert(1)</script>'
                '<p onclick="evil()">hi</p>'
                '<a href="javascript:alert(1)">bad</a>'
                '<a href="https://example.test/terms">terms</a>'
                '<img src="http://evil.example/px.png"/>')
        msg, conv, _ = self.ingest(subject="Rate quote: 6 pallets",
                                   body_html=html)
        body = msg.body or ""
        self.assertIn("table", body)
        self.assertIn("$542", body)
        self.assertIn("https://example.test/terms", body)
        self.assertNotIn("script", body.lower())
        self.assertNotIn("onclick", body.lower())
        self.assertNotIn("javascript:", body.lower())
        self.assertNotIn("evil.example", body.lower())
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        served = detail["messages"][0]["body"]
        self.assertEqual(served, msg.body)
        # the plain-text fallback is always available
        self.assertTrue(detail["messages"][0]["body_plain"])

    def test_html_body_without_content_falls_back_to_plain(self):
        """Incoming messages with an empty body (some systems send none)
        still render — body_plain is served, never raw HTML."""
        msg, conv, _ = self.ingest(body_html="", body="Just a note")
        self.assertFalse(msg.body)
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertEqual(detail["messages"][0]["body"], "")
        self.assertEqual(detail["messages"][0]["body_plain"], "Just a note")

    def test_gateway_plain_part_wins_and_fallback_is_sanitized(self):
        """message_new (the fetchmail gateway entry): the gateway's own
        text/plain part is stored as body_plain — a message with NO html
        part keeps its plain body. When no plain part exists, the fallback
        derives from the SANITIZED html, so raw script text never reaches
        the plain view."""
        # gateway delivers ONLY a plain text part (no html at all)
        self.Conversation.message_new({
            "email_from": "Bob <bob@plain.test>",
            "to": "dispatcher@logistics.premafirm.com",
            "subject": "Plain only",
            "body": "",
            "body_plain": "Just a note, no html at all.",
            "message_id": "<plain-part@x>",
        })
        # stored message_ids are bracket-stripped by the ingest path
        msg = self.Message.search([("message_id", "=", "plain-part@x")])
        self.assertEqual(msg.body_plain, "Just a note, no html at all.")
        # gateway delivers html with a script but no plain part → the
        # fallback is computed from the sanitized html (no script text)
        self.Conversation.message_new({
            "email_from": "Bob <bob@plain.test>",
            "to": "dispatcher@logistics.premafirm.com",
            "subject": "Html only",
            "body": "<p>hi</p><script>alert(1)</script>",
            "message_id": "<html-part@x>",
        })
        msg2 = self.Message.search([("message_id", "=", "html-part@x")])
        self.assertNotIn("alert", msg2.body_plain)
        self.assertEqual(msg2.body_plain.strip(), "hi")


# ----------------------------------------------------------------------
# D-4 — deterministic partner/contact resolution (no guessing ever)
# ----------------------------------------------------------------------
class TestPartnerResolutionD4(InboxTestCase):

    def _partner_count(self):
        return self.env["res.partner"].search_count([])

    def test_exact_email_match_uses_existing_partner(self):
        # NOTE: the demo sender's own address is NOT usable here — the UAT
        # clone's persistent data already owns bob@demo-toronto-produce.test
        # (partner 1913), which would make the test-created record a second
        # candidate and force the provisional path. Use an address the
        # persistent DB does not own.
        existing = self.env["res.partner"].create({
            "name": "Carol M", "email": "carol@existing.test"})
        before = self._partner_count()
        _, conv, _ = self.ingest(
            email_from="Carol M <carol@existing.test>", subject="Quote")
        self.assertEqual(conv.partner_id.id, existing.id)
        self.assertEqual(self._partner_count(), before)

    def test_child_contact_resolves_company_plus_contact(self):
        """A contact at a company resolves to company + contact — the
        crm.lead convention (partner_id = COMPANY, contact_id = person)."""
        company = self.env["res.partner"].create({
            "name": "Acme Logistics", "email": "ops@acme.test"})
        contact = self.env["res.partner"].create({
            "name": "Sandra", "email": "sandra@acme.test",
            "parent_id": company.id})
        _, conv, _ = self.ingest(
            email_from="Sandra <sandra@acme.test>", subject="Quote")
        self.assertEqual(conv.partner_id.id, company.id)
        self.assertEqual(conv.contact_id.id, contact.id)
        self.assertEqual(conv._conversation_row(conv)["customer_label"],
                         "Acme Logistics / Sandra")

    def test_prior_conversation_evidence(self):
        """The same exact sender email on a PRIOR conversation resolves the
        partner — no duplicate partner is ever created."""
        before = self._partner_count()
        _, conv1, _ = self.ingest(
            email_from="Sam <sam@repeat.test>", subject="First")
        _, conv2, _ = self.ingest(
            email_from="Sam <sam@repeat.test>", subject="Second",
            message_id="<rep-2@x>")
        self.assertEqual(conv1.partner_id.id, conv2.partner_id.id)
        self.assertEqual(self._partner_count(), before + 1)

    def test_crm_lead_evidence(self):
        """A CRM lead carrying this EXACT email is deterministic evidence
        for the partner (raw-email records, never a domain guess)."""
        p = self.env["res.partner"].create({"name": "Sara Ltd"})
        self.env["crm.lead"].create({
            "name": "Sara lead", "email_from": "sara@lead.test",
            "partner_id": p.id})
        before = self._partner_count()
        _, conv, _ = self.ingest(
            email_from="Sara <sara@lead.test>", subject="Quote")
        self.assertEqual(conv.partner_id.id, p.id)
        self.assertEqual(self._partner_count(), before)

    def test_ambiguous_email_no_auto_association(self):
        """Two records with the same email → NO automatic association:
        partner stays empty, provisional True, suggestions for the
        dispatcher. A wrong customer is a high-severity error."""
        a = self.env["res.partner"].create({
            "name": "Alpha", "email": "dup@ambiguous.test"})
        b = self.env["res.partner"].create({
            "name": "Beta", "email": "dup@ambiguous.test"})
        _, conv, _ = self.ingest(
            email_from="Someone <dup@ambiguous.test>", subject="Quote")
        self.assertFalse(conv.partner_id)
        self.assertTrue(conv.partner_provisional)
        suggestions = conv.partner_suggestions or []
        self.assertEqual(len(suggestions), 2)
        self.assertEqual({s["id"] for s in suggestions}, {a.id, b.id})
        conv.action_confirm_partner(a.id)
        self.assertEqual(conv.partner_id.id, a.id)
        self.assertFalse(conv.partner_provisional)
        self.assertFalse(conv.partner_suggestions)

    def test_confirm_child_pick_sets_company_and_contact(self):
        company = self.env["res.partner"].create({
            "name": "Shared Ltd", "email": "shared@conflict.test"})
        contact = self.env["res.partner"].create({
            "name": "Kim", "email": "shared@conflict.test",
            "parent_id": company.id})
        _, conv, _ = self.ingest(
            email_from="Kim <shared@conflict.test>", subject="Quote")
        self.assertTrue(conv.partner_provisional)
        conv.action_confirm_partner(contact.id)
        self.assertEqual(conv.partner_id.id, company.id)
        self.assertEqual(conv.contact_id.id, contact.id)
        self.assertFalse(conv.partner_provisional)

    def test_leave_unassigned_never_guesses(self):
        a = self.env["res.partner"].create({
            "name": "Alpha", "email": "dup2@ambiguous.test"})
        self.env["res.partner"].create({
            "name": "Beta", "email": "dup2@ambiguous.test"})
        _, conv, _ = self.ingest(
            email_from="Someone <dup2@ambiguous.test>", subject="Quote")
        conv.action_confirm_partner(False)
        self.assertFalse(conv.partner_id)
        self.assertFalse(conv.partner_provisional)
        self.assertFalse(conv.partner_suggestions)
        self.assertNotEqual(conv.partner_id.id, a.id)

    def test_no_domain_only_association(self):
        """A sender sharing the customer's DOMAIN but not their exact email
        is never associated — a new partner is created from the message."""
        acme = self.env["res.partner"].create({
            "name": "Acme Inc", "email": "acme@acme.test"})
        before = self._partner_count()
        _, conv, _ = self.ingest(
            email_from="Joe <joe@acme.test>", subject="Quote")
        self.assertNotEqual(conv.partner_id.id, acme.id)
        self.assertEqual(conv.partner_id.email, "joe@acme.test")
        self.assertEqual(self._partner_count(), before + 1)

    def test_no_name_similarity_association(self):
        """A display name matching a company name is not evidence — only
        the exact email is."""
        acme = self.env["res.partner"].create({
            "name": "ACME Trucking", "email": "acme@truck.test"})
        _, conv, _ = self.ingest(
            email_from="ACME Trucking <joe@else.test>", subject="Quote")
        self.assertNotEqual(conv.partner_id.id, acme.id)
        self.assertEqual(conv.partner_id.email, "joe@else.test")

    def test_unidentified_sender_created_from_message_itself(self):
        """Step 4: a brand-new sender yields a partner derived from the
        message's own display name + email — deterministic, no guessing."""
        before = self._partner_count()
        _, conv, _ = self.ingest(
            email_from="New Client <first@contact.test>", subject="Hi")
        self.assertEqual(conv.partner_id.email, "first@contact.test")
        self.assertEqual(conv.partner_id.name, "New Client")
        self.assertEqual(self._partner_count(), before + 1)


# ----------------------------------------------------------------------
# Link-target ACL resilience — the conversation list must NEVER 500
# ("Could not load conversations") because a row's booking/job/invoice/
# opportunity link points at a model the caller cannot read. Odoo 18
# raises the model-level ACL check even when reading the name of a NULL
# link, so this is not hypothetical: any inbox user without logistics
# groups lost the whole list as soon as a folder row was built.
# ----------------------------------------------------------------------
class TestListLinkAclResilience(InboxTestCase):

    def test_list_loads_with_unreadable_booking_link(self):
        """group_dispatch_inbox alone (no logistics groups) must still load
        the folder list — booking/job names degrade to "", nothing raises."""
        restricted = self.make_user("restricted.reader", group=True)
        env_r = self.env(user=restricted.id)
        _, conv, _ = self.ingest(subject="Quote")
        booking = self.env["logistics.booking"].search([], limit=1)
        if booking:
            conv.write({"booking_id": booking.id})
        # the whole call must succeed — before the read grants + the
        # per-link guard this raised AccessError on logistics.booking
        rows = env_r["prema.inbox.conversation"].inbox_conversations(
            "inbox")
        row = next(r for r in rows if r["id"] == conv.id)
        self.assertIn("booking_name", row)
        if booking:
            # the inbox group now has read on the link model → real name
            self.assertEqual(row["booking_name"], booking.name)
            # a user with no read at all still gets "" via _safe_link_name
            nobody = self.make_user("no.acl.at.all", group=False)
            self.assertEqual(
                env_r["prema.inbox.conversation"]._safe_link_name(
                    booking.with_user(nobody.id)),
                "")
        # NULL links are equally safe
        _, conv2, _ = self.ingest(subject="Quote two",
                                  message_id="<null-link@x>")
        rows2 = env_r["prema.inbox.conversation"].inbox_conversations(
            "inbox")
        row2 = next(r for r in rows2 if r["id"] == conv2.id)
        self.assertEqual(row2["booking_name"], "")
        self.assertEqual(row2["job_name"], "")

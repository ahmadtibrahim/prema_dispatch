# -*- coding: utf-8 -*-
"""Audit-and-harden regression suite (task AH — one test per audited fix).

Covered here, matching the A–AK findings:
  * composer RPC wiring — inbox_app.js calls compose_and_send on
    prema.inbox.conversation (never the old prema.inbox.message path)
  * reply / reply-all recipients default from the latest incoming, with
    internal PremaFirm addresses excluded
  * New email (compose) ALWAYS creates its own conversation — never
    silently attaches to the selected thread
  * internal notes: direction=note, zero mail.mail, zero SMTP
  * draft lifecycle: save → resume (draft_id) → send as ONE message;
    discard removes message + empty shell; author-only edits
  * retry_send idempotency + missing-recipient rejection
  * incoming HTML sanitized ONCE at ingest (scripts / on* / javascript:
    hrefs / remote images+pixels stripped, data: URIs kept, formatting
    kept); thread renders body_plain with t-esc + pre-wrap, never raw
  * attachments served ONLY through the authenticated inbox-group route,
    bound to an inbox message (no public endpoint, no cross-app files)
  * business links: partner hierarchy (contact → company records),
    manual-search escape hatch, foreign records rejected without it,
    unlink round-trip
  * assignment: inbox-only (never crm.lead.user_id), group-filtered
    candidates, audit note on change, mute toggle
  * AI summary persisted on the conversation; draft_reply returns text
    and NEVER auto-sends; fsa_unresolved pricing explains which side
  * folder membership follows category/workflow writes; conversation-missing
    resilience; systray badge regression guard
"""
import os
import re
import uuid

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import HttpCase

from .common import InboxTestCase, DEMO_SENDER

_MODULE = os.path.join(os.path.dirname(__file__), "..")


def _src(relpath):
    with open(os.path.join(_MODULE, relpath)) as fh:
        return fh.read()


def _mid():
    return "<fix-%s@prema-inbox>" % uuid.uuid4().hex


# ----------------------------------------------------------------------
# source-level wiring guards (the audit's own findings, fixed in code)
# ----------------------------------------------------------------------
class TestWiringGuards(InboxTestCase):

    def test_composer_rpc_uses_conversation_model(self):
        """The JS regression that started this audit: inbox_app.js called
        prema.inbox.message.compose_and_send but the method lives on
        prema.inbox.conversation. The canonical RPC must be used and the
        stale path must never return."""
        js = _src("static/src/js/inbox_app.js")
        self.assertIn('"prema.inbox.conversation", "compose_and_send"', js)
        self.assertNotIn('"prema.inbox.message", "compose_and_send"', js)
        for method in ("discard_draft", "retry_send",
                       "inbox_assign_candidates", "inbox_link_candidates"):
            self.assertIn('"prema.inbox.conversation", "%s"' % method, js)

    def test_category_state_selects_use_t_model(self):
        """t-att-value on a select never sets the selection (OWL gotcha) —
        the category/state selects must bind with t-model so the folder
        membership actually persists."""
        xml = _src("static/src/xml/inbox_app.xml")
        self.assertIn('t-model="state.detail.conversation.category"', xml)
        self.assertIn('t-model="state.detail.conversation.workflow_state"', xml)

    def test_no_entity_name_loop_variables(self):
        """t-as="lt" broke template compilation: the OWL 2 expression
        compiler decodes the identifier lt as the HTML entity <, turning
        every expression using the variable into garbage ('model===<?').
        This crashed the WHOLE app in the browser — no template renders,
        even though every Python test passed. Guard the whole template:
        no t-as may be a bare HTML entity name."""
        xml = _src("static/src/xml/inbox_app.xml")
        for match in re.finditer(r'\bt-as="([^"]+)"', xml):
            var = match.group(1)
            self.assertNotIn(
                var, {"lt", "gt", "amp", "quot", "apos"},
                "t-as=%r collides with an HTML entity name — the OWL "
                "compiler decodes it to a raw character" % var)
        self.assertNotIn('t-att-value="state.detail', xml)

    def test_thread_renders_plain_text_never_raw_html(self):
        """body_plain is rendered with t-esc (escaped) + pre-wrap; the
        sanitized HTML appears only behind the explicit View formatted
        toggle. A plain part containing markup must stay literal text."""
        msg, conv, _ = self.Conversation._ingest_email(
            email_from=DEMO_SENDER,
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="Plain part", body_html="<p>trusted</p>",
            body_plain="<script>alert(1)</script> pickup M5V",
            message_id=_mid())
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertIn("<script>alert(1)</script>",
                      detail["messages"][0]["body_plain"])
        xml = _src("static/src/xml/inbox_app.xml")
        self.assertIn('t-esc="m.body_plain"', xml)
        self.assertNotIn('t-raw="m.body_plain"', xml)

    def test_badge_still_registers_in_systray(self):
        """No systray regression: the envelope widget must still register
        in registry.category('systray') and the app must not patch the
        navbar/topbar DOM."""
        badge = _src("static/src/js/inbox_badge.js")
        self.assertIn('registry.category("systray")', badge)
        self.assertIn("/prema_inbox/unread_counts", badge)
        app_js = _src("static/src/js/inbox_app.js")
        self.assertNotIn("o_menu_systray", app_js)
        self.assertNotIn("o_topbar", app_js)


# ----------------------------------------------------------------------
# composer contract (sections B–F)
# ----------------------------------------------------------------------
class TestComposerContract(InboxTestCase):

    def test_reply_defaults_to_latest_incoming_sender(self):
        msg, conv, _ = self.ingest(subject="Rate quote")
        res = conv.compose_and_send("Re: quote", "Body", "reply", True)
        out = self.Message.browse(res["id"])
        self.assertEqual(out.recipient_ids.mapped("email"),
                         ["bob@demo-toronto-produce.test"])
        self.assertFalse(out.cc_ids)
        self.assertEqual(out.kind, "reply")
        self.assertEqual(out.outbound_state, "intercepted")

    def test_reply_all_includes_external_cc_excludes_internal(self):
        """reply_all: sender + external To/Cc; PremaFirm addresses are
        never reply recipients."""
        msg, conv, _ = self.ingest(subject="Rate quote")
        alice = self.env["res.partner"].create({
            "name": "Alice", "email": "alice@partner.test"})
        ext = self.env["res.partner"].create({
            "name": "Ext", "email": "ext@partner.test"})
        msg.recipient_ids += ext          # external To on the incoming
        msg.write({"cc_ids": [(4, alice.id)]})
        res = conv.compose_and_send("Re: quote", "Body", "reply_all", True)
        out = self.Message.browse(res["id"])
        self.assertEqual(sorted(out.recipient_ids.mapped("email")),
                         ["bob@demo-toronto-produce.test", "ext@partner.test"])
        self.assertEqual(out.cc_ids.mapped("email"), ["alice@partner.test"])

    def test_reply_send_without_any_recipient_rejected(self):
        """A conversation with no incoming mail has no reply defaults —
        sending must be refused server-side with a clear message (the
        client validates too, but the server is the boundary)."""
        partner = self.env["res.partner"].create({
            "name": "Nobody", "email": "nobody@test.local"})
        conv = self.Conversation.create({
            "name": "Empty thread", "partner_id": partner.id})
        with self.assertRaises(ValidationError) as cm:
            conv.compose_and_send("Re: x", "body", "reply", True)
        self.assertIn("No recipient", str(cm.exception))
        # ...but saving as a draft is allowed (fill in recipients later)
        res = conv.compose_and_send("Re: x", "body", "reply", False)
        self.assertEqual(self.Message.browse(res["id"]).outbound_state,
                         "draft")

    def test_compose_email_always_creates_own_conversation(self):
        """New email must NEVER attach to the selected thread — even when
        the recipient matches the open conversation's partner."""
        _, conv, _ = self.ingest(subject="Rate quote")
        res = conv.compose_and_send(
            "Fresh quote", "Body", "compose", True,
            to_partner_ids=["bob@demo-toronto-produce.test"])
        out = self.Message.browse(res["id"])
        self.assertNotEqual(out.conversation_id.id, conv.id)
        self.assertEqual(out.conversation_id.name, "Fresh quote")
        self.assertEqual(out.conversation_id.partner_id.email,
                         "bob@demo-toronto-produce.test")
        self.assertEqual(out.recipient_ids.mapped("email"),
                         ["bob@demo-toronto-produce.test"])
        # the original thread is untouched
        self.assertEqual(conv.name, "Rate quote")

    def test_forward_stays_in_thread_with_independent_chain(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        res = conv.compose_and_send(
            "", "Forwarded", "forward", True,
            to_partner_ids=["fwd@other.test"])
        out = self.Message.browse(res["id"])
        self.assertEqual(out.conversation_id.id, conv.id)
        self.assertEqual(out.subject, "Fwd: Rate quote: 6 pallets")
        self.assertEqual(out.recipient_ids.mapped("email"),
                         ["fwd@other.test"])
        self.assertFalse(out.references)  # new chain, no threading back
        self.assertEqual(out.kind, "forward")
        self.assertIn("wrote:", out.body)  # quoted origin

    def test_note_creates_zero_email_zero_recipients(self):
        msg, conv, _ = self.ingest()
        before = self.env["mail.mail"].search_count([])
        res = conv.compose_and_send("", "This is an internal note",
                                    "note", True)
        note = self.Message.browse(res["id"])
        self.assertEqual(note.direction, "note")
        self.assertEqual(note.outbound_state, "note")
        self.assertFalse(note.recipient_ids)
        self.assertFalse(note.cc_ids)
        self.assertEqual(self.env["mail.mail"].search_count([]), before)
        with self.assertRaises(ValidationError):
            conv.compose_and_send("", "   ", "note")  # empty note refused


# ----------------------------------------------------------------------
# draft lifecycle + retry (sections P/Q/W/X/Y)
# ----------------------------------------------------------------------
class TestDraftLifecycle(InboxTestCase):

    def test_save_resume_send_is_one_message(self):
        msg, conv, _ = self.ingest()
        res1 = conv.compose_and_send("Re: quote", "v1", "reply", False)
        draft = self.Message.browse(res1["id"])
        self.assertEqual(draft.outbound_state, "draft")
        # resume edits the SAME message — never a duplicate
        res2 = conv.compose_and_send("Re: quote v2", "v2 body", "reply",
                                     True, draft_id=res1["id"])
        self.assertEqual(res2["id"], res1["id"])
        out = self.Message.browse(res2["id"])
        self.assertEqual(out.subject, "Re: quote v2")
        self.assertEqual(out.outbound_state, "intercepted")
        self.assertEqual(len(conv.inbox_message_ids), 2)  # in + ONE out

    def test_draft_author_only(self):
        _, conv, _ = self.ingest()
        res = conv.compose_and_send("Re: quote", "v1", "reply", False)
        other = self.make_user("fix.other")
        with self.assertRaises(ValidationError) as cm:
            conv.with_user(other).compose_and_send(
                "Re: quote", "hijack", "reply", False, draft_id=res["id"])
        self.assertIn("Only the author", str(cm.exception))
        draft = self.Message.browse(res["id"])
        self.assertIn("v1", draft.body)          # original content untouched
        self.assertNotIn("hijack", draft.body)

    def test_discard_draft_removes_message_and_empty_shell(self):
        _, conv, _ = self.ingest()
        res = conv.compose_and_send("Re: quote", "v1", "reply", False)
        self.assertTrue(conv.discard_draft(res["id"]))
        self.assertFalse(self.Message.browse(res["id"]).exists())
        self.assertEqual(len(conv.inbox_message_ids), 1)
        # a draft-only shell conversation is deleted with its last message
        res2 = conv.compose_and_send("Draft only", "x", "compose", False)
        new_conv = self.Conversation.browse(res2["conversation_id"])
        self.assertTrue(new_conv.exists())
        self.assertTrue(conv.discard_draft(res2["id"]))
        self.assertFalse(self.Conversation.browse(
            res2["conversation_id"]).exists())

    def test_retry_send_idempotent_and_guarded(self):
        _, conv, _ = self.ingest()
        res = conv.compose_and_send("Re: quote", "Body", "reply", False)
        msg = self.Message.browse(res["id"])
        msg._set_outbound_state("failed", error="SMTP boom")
        out = conv.retry_send(res["id"])
        self.assertEqual(out["outbound_state"], "intercepted")
        # already-sent messages are never re-sent
        msg._set_outbound_state("sent")
        out2 = conv.retry_send(res["id"])
        self.assertEqual(out2["outbound_state"], "sent")
        # a failed message with no recipients is refused, not silently sent
        res3 = conv.compose_and_send("", "nobody", "compose", False)
        lost = self.Message.browse(res3["id"])
        lost._set_outbound_state("failed", error="x")
        with self.assertRaises(ValidationError) as cm:
            conv.retry_send(res3["id"])
        self.assertIn("No recipient", str(cm.exception))


# ----------------------------------------------------------------------
# HTML safety + attachments (sections G/H/AB/AD)
# ----------------------------------------------------------------------
class TestHtmlSafety(InboxTestCase):

    def test_ingest_strips_scripts_handlers_javascript_and_remote_images(self):
        html = ('<script>alert(1)</script>'
                '<p onclick="evil()" '
                'style="background-image:url(http://evil.example/bg.png)">'
                'Hi</p>'
                '<a href="javascript:alert(2)">link</a>'
                '<img src="http://evil.example/pixel.png">'
                '<img src="https://evil.example/t.gif">'
                '<img src="data:image/png;base64,AAAA">')
        msg, conv, _ = self.Conversation._ingest_email(
            email_from=DEMO_SENDER,
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="Evil mail", body_html=html, body_plain="",
            message_id=_mid())
        body = msg.body
        self.assertNotIn("<script", body)
        self.assertNotIn("onclick", body)
        self.assertNotIn("javascript:", body)
        self.assertNotIn("evil.example", body)     # no remote pixels
        self.assertNotIn("background-image", body)
        self.assertIn("data:image", body)          # inline data kept
        self.assertIn("Hi", body)                  # content survives

    def test_ingest_preserves_legitimate_formatting(self):
        msg, conv, _ = self.Conversation._ingest_email(
            email_from=DEMO_SENDER,
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="Formatted", body_html="<p>Pickup <b>Toronto</b> M5V</p>",
            body_plain="Pickup Toronto M5V", message_id=_mid())
        self.assertIn("<p>", msg.body)
        self.assertIn("<b>", msg.body)
        self.assertNotIn("<p>", msg.body_plain)
        self.assertNotIn("<b>", msg.body_plain)
        self.assertIn("Toronto", msg.body_plain)

    def test_detail_exposes_plain_body_without_literal_tags(self):
        msg, conv, _ = self.Conversation._ingest_email(
            email_from=DEMO_SENDER,
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="Detail", body_html="<p>6 pallets <i>dry</i></p>",
            body_plain="6 pallets dry", message_id=_mid())
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        m = detail["messages"][0]
        self.assertEqual(m["body_plain"], "6 pallets dry")
        self.assertFalse("<" in m["body_plain"])


class TestAttachmentRoute(HttpCase):
    """Authorized serving only: inbox-group member + attachment bound to an
    inbox message. No public endpoint (section AB/AD)."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.uat_mode", "1")
        self.member = self._mk_user("att.member@test.local", "AttTest1!", True)
        self.outsider = self._mk_user("att.outsider@test.local",
                                      "AttTest2!", False)
        partner = self.env["res.partner"].create({
            "name": "Att Tester", "email": "att@test.local"})
        self.att = self.env["ir.attachment"].create({
            "name": "POD.pdf", "mimetype": "application/pdf",
            "raw": b"%PDF-1.4 fake pdf content",
            "res_model": "prema.inbox.message"})
        self.unbound = self.env["ir.attachment"].create({
            "name": "secret.xlsx",
            "raw": b"PK\x03\x04 not really",
            "mimetype": "application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet"})
        self.env["prema.inbox.conversation"]._ingest_email(
            email_from="Att Tester <att@test.local>",
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="POD attached", body_html="<p>see attachment</p>",
            body_plain="see attachment",
            attachment_ids=[self.att.id], message_id=_mid())

    def _mk_user(self, login, password, with_group):
        partner = self.env["res.partner"].create({"name": login})
        groups = [(6, 0, [self.env.ref("base.group_user").id])]
        if with_group:
            groups[0][2].append(
                self.env.ref("prema_dispatch_inbox.group_dispatch_inbox").id)
        return self.env["res.users"].create({
            "name": login, "login": login, "password": password,
            "partner_id": partner.id, "groups_id": groups})

    def test_preview_inline_and_download_authorized(self):
        self.authenticate("att.member@test.local", "AttTest1!")
        r = self.url_open("/prema_inbox/attachment/%d/POD.pdf" % self.att.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "application/pdf")
        self.assertIn("inline", r.headers["Content-Disposition"])
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.content, b"%PDF-1.4 fake pdf content")
        r2 = self.url_open(
            "/prema_inbox/attachment/%d/POD.pdf?download=1" % self.att.id)
        self.assertIn("attachment", r2.headers["Content-Disposition"])

    def test_unbound_attachment_never_served(self):
        """The rel-table check is the boundary: any ir.attachment not bound
        to an inbox message is 404 — the route is not a file server."""
        self.authenticate("att.member@test.local", "AttTest1!")
        r = self.url_open(
            "/prema_inbox/attachment/%d/secret.xlsx" % self.unbound.id)
        self.assertEqual(r.status_code, 404)

    def test_outsider_cannot_fetch_attachment(self):
        self.authenticate("att.outsider@test.local", "AttTest2!")
        r = self.url_open("/prema_inbox/attachment/%d/POD.pdf" % self.att.id)
        self.assertNotEqual(r.status_code, 200)
        self.assertIn(r.status_code, (403, 404))


# ----------------------------------------------------------------------
# business links (sections I–L + AE)
# ----------------------------------------------------------------------
class TestBusinessLinks(InboxTestCase):

    def _company_conv(self):
        company = self.env["res.partner"].create({
            "name": "Acme Transport Inc"})
        contact = self.env["res.partner"].create({
            "name": "Bob Contact", "email": "bob@acme.test",
            "parent_id": company.id})
        booking = self.env["logistics.booking"].create({
            "partner_id": company.id, "name": "B-9001",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 6, "weight_lbs": 1000})
        msg, conv, _ = self.Conversation._ingest_email(
            email_from="Bob Contact <bob@acme.test>",
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="quote", body_plain="2 pallets",
            body_html="<p>2 pallets</p>", message_id=_mid())
        conv.partner_id = contact.id
        return conv, booking, company, contact

    def test_contact_sees_company_records(self):
        """Partner hierarchy: a conversation with a CONTACT must surface
        the COMPANY's bookings/invoices/jobs as link candidates."""
        conv, booking, company, contact = self._company_conv()
        cands = conv.inbox_link_candidates("booking", conv.id, "")
        self.assertTrue(any(c["id"] == booking.id for c in cands))

    def test_foreign_record_rejected_without_manual_search(self):
        conv, booking, company, contact = self._company_conv()
        other = self.env["res.partner"].create({"name": "Other Co"})
        other_booking = self.env["logistics.booking"].create({
            "partner_id": other.id, "name": "PF-77-QUOTE",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "weight_lbs": 500})
        with self.assertRaises(ValidationError) as cm:
            conv.action_link_record("booking", other_booking.id)
        self.assertIn("Not a valid candidate", str(cm.exception))
        self.assertFalse(conv.booking_id)
        # not in the automatic candidates either
        cands = conv.inbox_link_candidates("booking", conv.id, "")
        self.assertFalse(any(c["id"] == other_booking.id for c in cands))

    def test_manual_search_authorizes_cross_customer_record(self):
        conv, booking, company, contact = self._company_conv()
        other = self.env["res.partner"].create({"name": "Other Co"})
        other_booking = self.env["logistics.booking"].create({
            "partner_id": other.id, "name": "PF-77-QUOTE",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "weight_lbs": 500})
        # NOTE: logistics.booking.name is computed (booking_number or
        # "Booking N") — the given name is NOT what the search sees.
        # Manual search finds it by CUSTOMER name...
        cands = conv.inbox_link_candidates(
            "booking", conv.id, "Other", manual=True)
        self.assertTrue(any(c["id"] == other_booking.id for c in cands))
        # ...and by the computed RECORD name
        cands2 = conv.inbox_link_candidates(
            "booking", conv.id, "Booking", manual=True)
        self.assertTrue(any(c["id"] == other_booking.id for c in cands2))
        # manual link requires the search text to actually match the
        # computed record name
        with self.assertRaises(ValidationError):
            conv.action_link_record("booking", other_booking.id, "unrelated")
        conv.action_link_record(
            "booking", other_booking.id, "Booking %d" % other_booking.id)
        self.assertEqual(conv.booking_id.id, other_booking.id)

    def test_unknown_model_and_missing_record_rejected(self):
        conv, booking, company, contact = self._company_conv()
        with self.assertRaises(ValidationError):
            conv.action_link_record("payment", 1)
        with self.assertRaises(ValidationError):
            conv.action_link_record("booking", 999999)

    def test_unlink_record_round_trip(self):
        conv, booking, company, contact = self._company_conv()
        conv.action_link_record("booking", booking.id)
        self.assertEqual(conv.booking_id.id, booking.id)
        self.assertTrue(conv.action_unlink_record("booking"))
        self.assertFalse(conv.booking_id)
        self.assertFalse(conv.action_unlink_record("payment"))


# ----------------------------------------------------------------------
# assignment + audit + mute (sections M/N/O/AA)
# ----------------------------------------------------------------------
class TestAssignment(InboxTestCase):

    def test_assign_candidates_group_filtered(self):
        """Candidates = inbox group members (internal fallback) — never a
        full directory dump."""
        self.make_user("fix.member")
        self.make_user("fix.outsider", group=False)
        cands = self.Conversation.inbox_assign_candidates()
        logins = {c["login"] for c in cands}
        self.assertIn("admin", logins)         # admin id 2 has the group
        self.assertIn("fix.member", logins)
        self.assertNotIn("fix.outsider", logins)

    def test_assign_audits_and_never_touches_lead(self):
        partner = self.env["res.partner"].create({
            "name": "Lead Co", "email": "lead@test.local"})
        _, conv, _ = self.ingest()
        conv.partner_id = partner.id
        lead = self.env["crm.lead"].create({
            "name": "Opp 1", "partner_id": partner.id,
            "user_id": self.admin.id})
        conv.action_link_record("opportunity", lead.id)
        assignee = self.make_user("fix.assignee")
        before = len(conv.message_ids)
        conv.action_assign(assignee.id)
        self.assertEqual(conv.assignee_id.id, assignee.id)
        # the crm.lead salesperson is a DIFFERENT domain — untouched
        self.assertEqual(lead.user_id.id, self.admin.id)
        # the change is audited as an internal note on the thread
        self.assertEqual(len(conv.message_ids), before + 1)
        self.assertIn("Inbox assignee", conv.message_ids[0].body)

    def test_unassign_and_mute(self):
        _, conv, _ = self.ingest()
        assignee = self.make_user("fix.assignee")
        conv.action_assign(assignee.id)
        conv.action_assign(False)
        self.assertFalse(conv.assignee_id)
        # mute toggles per user and is exposed in the detail payload
        self.assertFalse(self.Conversation.inbox_conversation_detail(
            conv.id)["muted"])
        conv.action_toggle_mute()              # as the acting admin user
        self.assertTrue(self.Conversation.inbox_conversation_detail(
            conv.id)["muted"])
        conv.action_toggle_mute()
        self.assertFalse(self.Conversation.inbox_conversation_detail(
            conv.id)["muted"])


# ----------------------------------------------------------------------
# AI + pricing UX (sections S/T/U)
# ----------------------------------------------------------------------
class TestAiAndPricing(InboxTestCase):

    def test_summarize_persists_to_conversation(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        res = conv.inbox_ai_action("summarize")
        self.assertTrue(res.get("text"))
        self.assertEqual(conv.ai_summary, res["text"])
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertEqual(detail["ai"]["summary"], res["text"])

    def test_draft_reply_returns_text_never_sends(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        before = len(conv.inbox_message_ids)
        res = conv.inbox_ai_action("draft_reply")
        self.assertTrue(res.get("text"))
        self.assertEqual(len(conv.inbox_message_ids), before)  # no outbound
        self.assertFalse(conv.ai_summary)  # nothing persisted

    def test_fsa_unresolved_pricing_explains_which_side(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        conv.write({
            "ai_status": "ready",
            "ai_extraction": {
                "fields": {"pickup": {"city": "Toronto"},
                           "delivery": {"city": "Belleville"}},
                "sources": {}, "missing": [],
                "conflicting": [],
            }})
        res = conv.inbox_calculate_price()
        self.assertFalse(res["available"])
        self.assertEqual(res["reason"], "fsa_unresolved")
        self.assertTrue(res["reason_text"])
        self.assertIn("pickup location", res["reason_text"])
        self.assertEqual(res["missing_sides"],
                         {"pickup": True, "delivery": True})
        # The verdict must be PERSISTED: the panel's warn branch renders
        # from state.detail.pricing (the stored snapshot), so a toast-only
        # result would leave the pricing card dead after reconcile.
        self.assertTrue(res["snapshot_saved"])
        self.assertTrue(conv.price_snapshot)
        self.assertEqual(conv.price_snapshot["reason"], "fsa_unresolved")
        self.assertEqual(conv.price_snapshot["reason_text"], res["reason_text"])


# ----------------------------------------------------------------------
# folders + resilience (sections V/W/AC)
# ----------------------------------------------------------------------
class TestFoldersAndResilience(InboxTestCase):

    def test_category_and_workflow_drive_folder_membership(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")

        def ids(folder):
            return {c["id"] for c in
                    self.Conversation.inbox_conversations(folder)}

        self.assertIn(conv.id, ids("inbox"))
        self.assertIn(conv.id, ids("quote_requests"))
        conv.write({"category": "active_shipment"})
        self.assertIn(conv.id, ids("active_shipments"))
        self.assertNotIn(conv.id, ids("quote_requests"))
        conv.write({"category": "needs_review", "workflow_state": "open"})
        self.assertIn(conv.id, ids("needs_review"))
        conv.write({"workflow_state": "waiting"})
        self.assertIn(conv.id, ids("waiting_reply"))
        self.assertNotIn(conv.id, ids("inbox"))
        conv.write({"workflow_state": "archived"})
        self.assertIn(conv.id, ids("archived"))
        self.assertNotIn(conv.id, ids("waiting_reply"))
        conv.write({"is_spam": True})
        self.assertIn(conv.id, ids("spam"))
        # the archived folder is state-based (no is_spam filter) — a spam
        # conversation that was archived before stays visible there too
        self.assertIn(conv.id, ids("archived"))

    def test_detail_missing_conversation_returns_empty(self):
        """Stale selection / deleted thread → {} → the UI shows
        'Conversation no longer exists.' instead of crashing."""
        self.assertEqual(self.Conversation.inbox_conversation_detail(
            999999), {})

    def test_detail_exposes_drafts_and_reply_defaults(self):
        _, conv, _ = self.ingest(subject="Rate quote")
        res = conv.compose_and_send("Re: quote", "v1", "reply", False)
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertEqual([d["id"] for d in detail["drafts"]], [res["id"]])
        self.assertEqual(detail["drafts"][0]["kind"], "reply")
        # reply_defaults["to"] must be {id, email} objects — the composer
        # does (arr || []).map((p) => p.email) to prefill the To field, so
        # bare partner ids render an empty To (regressions guarded: defaults
        # computed on the @api.model empty self → always empty list; and
        # bare ids → empty To in the UI)
        bob = self.env["res.partner"].search(
            [("email", "=", "bob@demo-toronto-produce.test")], limit=1)
        self.assertEqual(detail["reply_defaults"]["to"],
                         [{"id": bob.id, "email": bob.email}])
        self.assertEqual(detail["reply_defaults"]["cc"], [])
        # reply_all must carry the external To recipients too, not only the
        # sender (to_default_all was computed and dropped before this fix)
        self.assertEqual(detail["reply_all_defaults"]["to"],
                         [{"id": bob.id, "email": bob.email}])
        self.assertIn("muted", detail)
        self.assertIn("summary", detail["ai"])

    def test_resumed_draft_send_refreshes_body_plain(self):
        """Editing a resumed draft must update body_plain too — the thread
        renders plain text, so a stale body_plain shows the ORIGINAL text
        after the user edits and sends (browser UAT caught this)."""
        _, conv, _ = self.ingest(subject="Rate quote")
        draft = conv.compose_and_send(
            "Re: quote", "DRAFT v1", "reply", False)["id"]
        conv.compose_and_send(
            "Re: quote", "DRAFT v1 EDITED", "reply", True,
            draft_id=draft)
        sent = self.Message.browse(draft)
        self.assertIn(sent.outbound_state, ("sent", "intercepted"))
        self.assertIn("DRAFT v1 EDITED", sent.body)
        self.assertIn("DRAFT v1 EDITED", sent.body_plain)
        self.assertNotIn("DRAFT v1\n", sent.body_plain.split("EDITED")[0])
        # and the detail payload serves the refreshed plain body
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        msg = next(m for m in detail["messages"] if m["id"] == draft)
        self.assertIn("DRAFT v1 EDITED", msg["body_plain"])

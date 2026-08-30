# -*- coding: utf-8 -*-
"""Production gateway tests: fetchmail message_new/message_update route and
the real outbound mail.mail pipeline (honest draft/pending/sent/failed).

Design §11(c): "set inbox model message_new/message_update with message_id
dedupe + thread-by-References". §4: "Remove send interception only for the
production inbox configuration... Show actual queued/sent/failed status.
Never label an intercepted or merely queued message as successfully
delivered."

The mail.mail SMTP flight is stubbed in every test — nothing here may
touch a real outgoing server. Stubbing replaces the class method with a
plain function (a function on a class IS a descriptor → self binds), which
avoids unittest.mock's autospec introspection over api-decorated methods.
"""
import base64
from contextlib import contextmanager

from odoo.addons.mail.models.mail_mail import MailDeliveryException

from .common import InboxTestCase

# The exact shape mail.message_process hands to message_new (Odoo 18).
MSG = {
    "message_id": "<gateway-1@client.example>",
    "email_from": "Janet Carrier <janet@carrier.example>",
    "to": "dispatcher@logistics.premafirm.com",
    "subject": "Rate quote: 8 pallets reefer",
    "body": "<p>Pickup Toronto M5V, delivery Ottawa K1A, 8 pallets.</p>",
    "references": "",
    "in_reply_to": "",
    "attachments": [],
}


class TestGatewayIngest(InboxTestCase):

    def test_message_new_creates_conversation(self):
        conv = self.Conversation.message_new(dict(MSG))
        msg = conv.inbox_message_ids
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.direction, "incoming")
        self.assertEqual(msg.message_id, "gateway-1@client.example")
        self.assertEqual(msg.email_from,
                         "Janet Carrier <janet@carrier.example>")
        self.assertEqual(conv.category, "quote_request")
        # announcement broadcasted into the bus buffer
        self.assertTrue(self.bus_rows("prema_inbox"))

    def test_message_new_dedupe_same_message_id(self):
        conv1 = self.Conversation.message_new(dict(MSG))
        before = self.Message.search_count([])
        conv2 = self.Conversation.message_new(dict(MSG))  # duplicate delivery
        self.assertEqual(conv1.id, conv2.id)
        self.assertEqual(self.Message.search_count([]), before)
        self.assertEqual(len(conv1.inbox_message_ids), 1)

    def test_message_new_threads_by_references(self):
        conv1 = self.Conversation.message_new(dict(MSG))
        conv2 = self.Conversation.message_new({
            "message_id": "<gateway-2@client.example>",
            "email_from": "Janet Carrier <janet@carrier.example>",
            "to": "dispatcher@logistics.premafirm.com",
            "subject": "Re: Rate quote: 8 pallets reefer",
            "body": "<p>Also add 2 more pallets.</p>",
            "references": "<gateway-1@client.example>",
            "in_reply_to": "<gateway-1@client.example>",
            "attachments": [],
        })
        self.assertEqual(conv2.id, conv1.id)  # one conversation, two messages
        self.assertEqual(len(conv1.inbox_message_ids), 2)
        self.assertTrue(self.bus_rows("prema_inbox"))  # reply announced too

    def test_message_new_attachments_bound(self):
        # Real gateway shape: namedtuple (fname, content, info) — three
        # elements, content base64 or bytes (Odoo 18 mail_thread._Attachment)
        payload = base64.b64encode(b"hello world").decode()
        conv = self.Conversation.message_new(dict(
            MSG, message_id="<gateway-att@client.example>",
            attachments=[("rates.pdf", payload, {"mimetype": "application/pdf"})]))
        msg = conv.inbox_message_ids
        self.assertEqual(len(msg.attachment_ids), 1)
        att = msg.attachment_ids[0]
        self.assertEqual(att.name, "rates.pdf")
        self.assertEqual(att.res_model, "prema.inbox.message")
        self.assertEqual(att.res_id, msg.id)
        self.assertEqual(base64.b64decode(att.datas), b"hello world")

    def test_message_new_attachment_bytes_three_tuple(self):
        # fetchmail delivers raw bytes content in the info-position tuple;
        # bytes content must round-trip into a decodable attachment.
        conv = self.Conversation.message_new(dict(
            MSG, message_id="<gateway-att2@client.example>",
            attachments=[("manifest.xlsx", b"PK\x03\x04binary", {})]))
        att = conv.inbox_message_ids.attachment_ids
        self.assertEqual(len(att), 1)
        self.assertEqual(att.name, "manifest.xlsx")
        self.assertEqual(base64.b64decode(att.datas), b"PK\x03\x04binary")

    def test_message_new_load_board_detection(self):
        conv = self.Conversation.message_new(dict(
            MSG, message_id="<rmis-1@rmis.example>",
            subject="RMIS load board — Mascouche to Belleville"))
        self.assertTrue(conv.inbox_message_ids.is_load_board)

    def test_message_new_threads_reply_to_outbound(self):
        # A customer reply whose References points at a SENT outbound
        # message (stored with angle brackets on prod before the fix)
        # must thread into the SAME conversation — never a new one.
        conv1 = self.Conversation.message_new(dict(MSG))
        out = self.Message._new_outbound(
            conv1, subject="Re: Rate quote", body_html="<p>Sent</p>",
            parent=conv1.inbox_message_ids, kind="reply")
        self.assertEqual(out.outbound_state, "draft")
        # bracketed form = what an RFC 5322 reply actually carries
        conv2 = self.Conversation.message_new(dict(
            MSG, message_id="<gateway-reply@client.example>",
            subject="Re: Rate quote", body="<p>Thanks!</p>",
            references="<7780523eb4684b2bac9bb16dd7902404@prema-inbox.premafirm.com>"
                       " <%s>" % out.message_id))
        self.assertEqual(conv2.id, conv1.id)
        self.assertEqual(len(conv1.inbox_message_ids), 3)

    def test_message_new_threads_reply_to_outbound_bare_form(self):
        # post-fix storage is bare; a reply referencing the bare form
        # must thread too (and bare vs bracketed never double-matches)
        conv1 = self.Conversation.message_new(dict(MSG))
        out = self.Message._new_outbound(
            conv1, subject="Re: Rate quote", body_html="<p>Sent</p>",
            parent=conv1.inbox_message_ids, kind="reply")
        self.assertNotIn("<", out.message_id)  # stored bare after fix
        conv2 = self.Conversation.message_new(dict(
            MSG, message_id="<gateway-reply2@client.example>",
            subject="Re: Rate quote", body="<p>Thanks!</p>",
            references="<%s>" % out.message_id))
        self.assertEqual(conv2.id, conv1.id)
        self.assertEqual(len(conv1.inbox_message_ids), 3)

    def test_message_update_same_thread(self):
        conv1 = self.Conversation.message_new(dict(MSG))
        conv2 = self.Conversation.message_update(dict(
            MSG, message_id="<gateway-3@client.example>",
            subject="Re: Rate quote", body="<p>Confirmed.</p>",
            references="<gateway-1@client.example>"))
        self.assertEqual(conv2.id, conv1.id)
        self.assertEqual(len(conv1.inbox_message_ids), 2)

    def test_gateway_end_to_end_raw_rfc822(self):
        """The REAL fetchmail path: raw RFC 822 bytes → message_process →
        message_route → message_new. Odoo 18 parses attachments into
        namedtuples (fname, content, info) — this test mirrors the actual
        gateway shape (regression: 2-tuple unpack crash on prod).

        message_route consults mail.alias BEFORE the model fallback, so to
        exercise the production route the dispatcher alias is pointed at
        the inbox model here (clone keeps polling off; the transaction
        rollback restores the alias)."""
        alias = self.env["mail.alias"].search([
            ("alias_name", "=", "dispatcher"),
            ("alias_domain_id.name", "=", "logistics.premafirm.com")],
            limit=1)
        self.assertTrue(alias, "dispatcher alias must exist (clone)")
        alias.write({"alias_model_id": self.env["ir.model"].search(
            [("model", "=", "prema.inbox.conversation")], limit=1).id})
        raw = (b"From: Janet Carrier <janet@carrier.example>\r\n"
               b"To: dispatcher@logistics.premafirm.com\r\n"
               b"Subject: Rate quote: 8 pallets reefer\r\n"
               b"Message-ID: <gateway-e2e@client.example>\r\n"
               b"Date: Sat, 30 Aug 2026 04:20:00 -0400\r\n"
               b"MIME-Version: 1.0\r\n"
               b"Content-Type: multipart/mixed; boundary=bb\r\n"
               b"\r\n"
               b"--bb\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n"
               b"\r\n"
               b"Pickup Toronto M5V, delivery Ottawa K1A, 8 pallets.\r\n"
               b"--bb\r\n"
               b"Content-Type: application/pdf; name=rates.pdf\r\n"
               b"Content-Disposition: attachment; filename=rates.pdf\r\n"
               b"Content-Transfer-Encoding: base64\r\n"
               b"\r\n"
               b"aGVsbG8gd29ybGQ=\r\n"
               b"--bb--\r\n")
        MailThread = self.env["mail.thread"]
        # (this fork keeps with_context on recordsets, not on Environment)
        res = MailThread.with_context(
            fetchmail_cron_running=True,
            default_fetchmail_server_id=10,
        ).message_process("prema.inbox.conversation", raw,
                          save_original=True, strip_attachments=False)
        self.assertTrue(res, "message_process must return the thread id")
        conv = self.Conversation.browse(res)
        self.assertEqual(len(conv.inbox_message_ids), 1)
        msg = conv.inbox_message_ids
        self.assertEqual(msg.direction, "incoming")
        names = msg.attachment_ids.mapped("name")
        self.assertEqual(names, ["rates.pdf"])  # original_email.eml skipped
        self.assertEqual(base64.b64decode(msg.attachment_ids.datas),
                         b"hello world")

    def test_message_new_plain_text_attachment_encoded(self):
        # Regression: save_original appends 'original_email.eml' whose
        # content is the raw RFC822 as a PLAIN STRING — storing it verbatim
        # as datas crashed ir.attachment's base64 round-trip on prod
        # (binascii "Incorrect padding"). Plain text (non-base64 str) must
        # be encoded; base64 text passes through untouched.
        conv = self.Conversation.message_new(dict(
            MSG, message_id="<gateway-txt@client.example>",
            attachments=[
                ("original_email.eml",
                 "From: x@y.z\r\nTo: dispatcher@logistics.premafirm.com\r\n",
                 {}),
                ("notes.txt", "hello dispatcher — plain text", {}),
                ("rates.pdf",
                 base64.b64encode(b"hello world").decode(), {}),
            ]))
        atts = conv.inbox_message_ids.attachment_ids
        self.assertEqual(atts.mapped("name"), ["notes.txt", "rates.pdf"])
        self.assertEqual(base64.b64decode(atts[0].datas).decode("utf-8"),
                         "hello dispatcher — plain text")
        self.assertEqual(base64.b64decode(atts[1].datas), b"hello world")


class TestOutboundPipeline(InboxTestCase):
    """Real send path (prema_inbox.intercept_outgoing = "0"): mail.mail is
    built with the production headers and the SMTP flight is stubbed — the
    assertions cover the honest state machine, never a real server."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "0")

    def tearDown(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "1")
        super().tearDown()

    def _recipient(self):
        return self.env["res.partner"].create({
            "name": "Test Recv", "email": "recv@test.example"})

    def _draft(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        res = conv.compose_and_send("Re: quote", "Body", "reply", False)
        msg = self.Message.browse(res["id"])
        msg.recipient_ids = [(6, 0, self._recipient().ids)]
        return msg

    @contextmanager
    def _stub_send(self, fn):
        """Replace mail.mail.send with a plain function for the duration.

        Note: env[model] is an EMPTY RECORDSET in Odoo 18 — the class must
        come from the registry (instance-attribute assignment on a record
        raises 'attribute is read-only')."""
        Mail = self.env.registry["mail.mail"]
        orig = Mail.send
        Mail.send = fn
        try:
            yield
        finally:
            Mail.send = orig

    def test_intercept_default_never_smtp(self):
        # UAT default (this class normally arms intercept=0): intercepted
        # state, zero mail.mail rows — the guard must win before SMTP
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "1")
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        before = self.env["mail.mail"].search_count(
            [("email_from", "ilike", "dispatcher@")])
        res = conv.compose_and_send("Re: quote", "Body", "reply", True)
        msg = self.Message.browse(res["id"])
        self.assertEqual(msg.outbound_state, "intercepted")
        self.assertEqual(self.env["mail.mail"].search_count(
            [("email_from", "ilike", "dispatcher@")]), before)

    def test_real_pipeline_success_state_machine(self):
        def fake_send(self, auto_commit=False, raise_exception=False,
                      post_send_callback=None):
            self.write({"state": "sent"})
            return True

        msg = self._draft()
        with self._stub_send(fake_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "sent")
        mail = self.env["mail.mail"].search(
            [("model", "=", "prema.inbox.message"),
             ("res_id", "=", msg.id)], limit=1)
        self.assertTrue(mail)
        self.assertEqual(mail.email_from,
                         "dispatcher@logistics.premafirm.com")
        self.assertEqual(mail.reply_to, "dispatcher@logistics.premafirm.com")
        self.assertEqual(mail.message_id, msg.message_id)
        self.assertEqual(mail.references, msg.references)
        self.assertEqual(mail.subject, "Re: quote")

    def test_real_pipeline_smtp_failure_is_honest(self):
        def fail_send(self, auto_commit=False, raise_exception=False,
                      post_send_callback=None):
            raise MailDeliveryException(
                "Unable to connect to SMTP Server", Exception("connection refused"))

        msg = self._draft()
        with self._stub_send(fail_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "failed")
        self.assertIn("connection refused", msg.send_error)

    def test_real_pipeline_gateway_left_unsent_is_failed(self):
        def noop_send(self, auto_commit=False, raise_exception=False,
                      post_send_callback=None):
            return True  # mail.mail stays 'outgoing' — nothing flew

        msg = self._draft()
        with self._stub_send(noop_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "failed")
        self.assertIn("outgoing", msg.send_error)

    def test_no_recipient_fails_before_smtp(self):
        msg = self._draft()
        msg.recipient_ids = [(5, 0, 0)]  # nobody to send to
        msg.send()
        self.assertEqual(msg.outbound_state, "failed")
        self.assertIn("No recipient", msg.send_error)

    def test_retry_after_failure_is_possible(self):
        def fail_send(self, auto_commit=False, raise_exception=False,
                      post_send_callback=None):
            raise MailDeliveryException(
                "Unable to connect to SMTP Server", Exception("down"))

        def fake_send(self, auto_commit=False, raise_exception=False,
                      post_send_callback=None):
            self.write({"state": "sent"})
            return True

        msg = self._draft()
        with self._stub_send(fail_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "failed")
        before = self.Message.search_count(
            [("conversation_id", "=", msg.conversation_id.id)])
        with self._stub_send(fake_send):
            msg.send()  # retry — no duplicate outbound row
        self.assertEqual(msg.outbound_state, "sent")
        self.assertFalse(msg.send_error)  # success clears the stale failure
        self.assertEqual(self.Message.search_count(
            [("conversation_id", "=", msg.conversation_id.id)]), before)

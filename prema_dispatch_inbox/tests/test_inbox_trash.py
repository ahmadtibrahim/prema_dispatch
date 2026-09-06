# -*- coding: utf-8 -*-
"""Trash lifecycle (§19.2) + server-mail safety (R5) + record identity (R1).

Soft trash: delete-to-Trash (single/bulk) and restore never destroy data —
only the trashed/trashed_at/trashed_by flag turns on, workflow state /
category / links / messages survive, and folders derive the thread's
absence/presence from the flag. Permanent delete exists ONLY inside Trash,
requires the typed "DELETE" confirmation, and removes inbox rows +
exclusively-bound attachments while the mail.mail ledger rows of sent
messages (the server-side record, provider identity by Message-ID) always
survive. Nothing in the module ever calls a mail server: trash/delete paths
are pure ORM, and every send test runs under a registry stub of
mail.mail.send so no SMTP flight can happen.

R1/R4 (Sent identity + dedupe): one mail.mail ledger row per outbound
message, reused across retries — the provider identity (RFC Message-ID)
stays unique, and the Sent folder lists a conversation once regardless of
how many of its messages were sent.
"""
from contextlib import contextmanager

from odoo.exceptions import ValidationError
from odoo.addons.mail.models.mail_mail import MailDeliveryException

from .common import InboxTestCase

TYPED = "DELETE"
LEDGER_RECV = "ledger-recv@test.example"


class TestTrashLifecycle(InboxTestCase):

    def _conv(self, subject="Rate quote: 4 pallets"):
        return self.Conversation.browse(self.ingest(subject=subject)[1].id)

    def test_folder_domain_after_single_trash(self):
        conv = self._conv()
        # visible in the working folders before…
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations("inbox").ids)
        # …trashing is one soft flag write (no messages lost)
        conv.action_trash()
        self.assertTrue(conv.trashed)
        self.assertTrue(conv.trashed_at)
        self.assertEqual(conv.trashed_by.id, self.admin.id)
        self.assertEqual(len(conv.inbox_message_ids), 1)
        # gone from every working folder, listed ONLY in Trash
        for key in ("inbox", "unread", "tasks", "drafts", "sent", "archived",
                    "spam", "needs_review", "quote_requests",
                    "load_opportunities", "active_shipments",
                    "waiting_reply"):
            self.assertNotIn(
                conv.id, self.Conversation._folder_conversations(key).ids,
                "trashed thread leaked into %s" % key)
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations("trash").ids)
        self.assertIn("trash", [f["key"]
                                for f in self.Conversation.inbox_folders()])

    def test_bulk_trash(self):
        c1, c2 = self._conv("A"), self._conv("B")
        self.Conversation.browse([c1.id, c2.id]).action_trash()
        self.assertTrue(c1.trashed and c2.trashed)
        self.assertEqual(
            len(self.Conversation._folder_conversations("trash")), 2)

    def test_restore_returns_to_working_folder_and_keeps_state(self):
        conv = self._conv()
        conv.write({"workflow_state": "completed"})
        conv.action_trash()
        self.assertNotIn(
            conv.id, self.Conversation._folder_conversations("archived").ids)
        conv.action_restore()
        self.assertFalse(conv.trashed)
        self.assertFalse(conv.trashed_at)
        self.assertFalse(conv.trashed_by)
        self.assertEqual(conv.workflow_state, "completed")  # untouched
        self.assertEqual(len(conv.inbox_message_ids), 1)    # history intact
        self.assertNotIn(
            conv.id, self.Conversation._folder_conversations("trash").ids)
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations(
                          "archived").ids)

    def test_restore_on_open_returns_to_inbox(self):
        conv = self._conv()
        conv.action_trash()
        conv.action_restore()
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations("inbox").ids)

    def test_new_incoming_reply_auto_restores_trashed_thread(self):
        # a customer reply is an active signal — never buried in Trash
        conv = self._conv()
        conv.action_trash()
        msg = conv.inbox_message_ids
        _, same, created = self.ingest(
            subject="Re: Rate quote: 4 pallets",
            references="<%s>" % msg.message_id)
        self.assertEqual(same.id, conv.id)
        self.assertFalse(created)
        self.assertFalse(conv.trashed)          # auto-restored
        self.assertEqual(len(conv.inbox_message_ids), 2)
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations("inbox").ids)

    def test_unread_counts_and_folder_exclude_trashed(self):
        conv = self._conv()
        self.assertGreater(
            self.Message._unread_counts([self.admin])[self.admin.id]["total"],
            0)
        conv.action_trash()
        self.assertEqual(
            self.Message._unread_counts([self.admin])[self.admin.id]["total"],
            0)
        self.assertNotIn(
            conv.id, self.Conversation._folder_conversations("unread").ids)
        conv.action_restore()
        self.assertIn(conv.id,
                      self.Conversation._folder_conversations("unread").ids)

    def test_composer_blocked_on_trashed_conversation(self):
        conv = self._conv()
        conv.action_trash()
        with self.assertRaises(ValidationError):
            conv.compose_and_send("Re: x", "<p>body</p>", "reply", False)
        with self.assertRaises(ValidationError):
            conv.compose_and_send("", "<p>note</p>", "note", False)
        # brand-new compose always allowed (its own fresh conversation)
        res = conv.compose_and_send("Fresh", "<p>hi</p>", "compose", False)
        self.assertTrue(res["id"])

    def test_send_blocked_on_trashed_conversation(self):
        conv = self._conv()
        res = conv.compose_and_send("Re: x", "<p>body</p>", "reply", False)
        draft = self.Message.browse(res["id"])
        conv.action_trash()
        with self.assertRaises(ValidationError):
            draft.send()
        self.assertEqual(draft.outbound_state, "draft")  # still intact

    def test_retry_send_blocked_on_trashed_conversation(self):
        conv = self._conv()
        res = conv.compose_and_send("Re: x", "<p>body</p>", "reply", False)
        conv.action_trash()
        with self.assertRaises(ValidationError):
            conv.retry_send(res["id"])
        self.assertEqual(self.Message.browse(res["id"]).outbound_state,
                         "draft")


class TestPermanentDelete(InboxTestCase):
    """Permanent delete: Trash-only, typed confirmation, inbox rows only —
    server-side mail (mail.mail ledger) is never deleted or altered."""

    def _trash_conv(self, subject="Rate quote: 2 pallets"):
        conv = self._conv(subject)
        conv.action_trash()
        return conv

    def _conv(self, subject="Rate quote: 2 pallets"):
        return self.Conversation.browse(self.ingest(subject=subject)[1].id)

    def test_permanent_delete_refuses_without_typed_confirmation(self):
        conv = self._trash_conv()
        with self.assertRaises(ValidationError):
            conv.action_delete_permanent("")       # empty
        with self.assertRaises(ValidationError):
            conv.action_delete_permanent("delete")  # lowercase ≠ DELETE
        self.assertTrue(conv.exists())

    def test_permanent_delete_refuses_outside_trash(self):
        conv = self._conv()  # NOT trashed
        with self.assertRaises(ValidationError):
            conv.action_delete_permanent(TYPED)
        self.assertTrue(conv.exists())

    def test_permanent_delete_removes_inbox_rows_only(self):
        conv = self._trash_conv()
        conv.action_delete_permanent(TYPED)
        self.assertFalse(conv.exists())
        self.assertEqual(self.Message.search_count([]), 0)

    def test_attachments_unlinked_only_when_exclusive(self):
        conv = self._trash_conv()
        msg = conv.inbox_message_ids
        att = self.env["ir.attachment"].sudo().create({
            "name": "rates.pdf", "datas": "aGVsbG8="})
        shared = self.env["ir.attachment"].sudo().create({
            "name": "shared.pdf", "datas": "aGVsbG8="})
        msg.attachment_ids = [(4, att.id), (4, shared.id)]
        # shared.pdf ALSO bound to a surviving message in another thread
        other = self._conv("Shared x")
        other.inbox_message_ids.attachment_ids = [(4, shared.id)]
        conv.action_delete_permanent(TYPED)
        self.assertFalse(att.exists(), "exclusive attachment must die")
        self.assertTrue(shared.exists(), "shared attachment must survive")

    def test_sent_ledger_and_attachments_survive_permanent_delete(self):
        """R5 + R1: a SENT message's mail.mail ledger row — the Odoo-side
        record of the server mail, carrying the provider Message-ID — must
        survive trash AND permanent delete untouched, with its attachment."""
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "0")
        partner = self.env["res.partner"].create({
            "name": "Ledger Recv", "email": LEDGER_RECV})
        conv = self._conv()
        res = conv.compose_and_send("Re: quote", "<p>sent</p>", "reply",
                                    False, to_partner_ids=[partner.id])
        msg = self.Message.browse(res["id"])
        att = self.env["ir.attachment"].sudo().create({
            "name": "quote.pdf", "datas": "aGVsbG8="})
        msg.attachment_ids = [(4, att.id)]
        with self._stub_send(self._ok_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "sent")
        mail = msg.mail_mail_id
        self.assertTrue(mail and mail.state == "sent")
        self.assertIn(att.id, mail.attachment_ids.ids)
        ledger_id = mail.id
        provider_mid = msg.message_id  # the identity both rows carry
        # trash the thread, then permanently delete it
        conv.action_trash()
        conv.action_delete_permanent(TYPED)
        self.assertFalse(conv.exists())
        mail = self.env["mail.mail"].browse(ledger_id)
        self.assertTrue(mail.exists(), "mail.mail ledger row must survive")
        self.assertEqual(mail.state, "sent")      # never altered
        self.assertTrue(att.exists(), "ledger-bound attachment survives")
        # provider identity intact: the ledger still carries the exact
        # Message-ID the (now deleted) inbox message had
        self.assertEqual(mail.message_id, provider_mid)

    def test_trash_and_delete_paths_never_call_smtp_nor_create_mail(self):
        def boom(self, *args, **kwargs):
            raise AssertionError("SMTP flight attempted on a trash path")

        Mail = self.env.registry["mail.mail"]
        orig, Mail.send = Mail.send, boom
        try:
            conv = self._trash_conv()
            before = self.env["mail.mail"].search_count([])
            conv.action_trash()       # re-trash (idempotent)
            conv.action_restore()
            conv.action_trash()
            conv.action_delete_permanent(TYPED)
            self.assertEqual(self.env["mail.mail"].search_count([]), before)
        finally:
            Mail.send = orig

    def test_purge_respects_retention_and_requires_confirmation(self):
        old = self._trash_conv("Old quote")
        old.write({"trashed_at": "2020-01-01 00:00:00"})  # way past any window
        fresh = self._trash_conv("Fresh quote")           # trashed just now
        with self.assertRaises(ValidationError):
            old.action_purge_trash(30, "")  # confirmation not typed
        # the fresh thread survives a 30-day window; the 2020 one does not
        n = old.action_purge_trash(30, TYPED)
        self.assertEqual(n, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        # zero-day window catches everything
        n = fresh.action_purge_trash(0, TYPED)
        self.assertEqual(n, 1)
        self.assertFalse(fresh.exists())

    def test_purge_defaults_to_retention_param_and_never_auto_runs(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.trash_retention_days", "7")
        self.assertEqual(self.Conversation.trash_retention_days(), 7)
        # the retention window feeds the EXPLICIT action only — and no cron
        # for trash/purge exists anywhere in the module
        crons = self.env["ir.cron"].sudo().search([
            ("model", "=", "prema.inbox.conversation"),
            ("function", "ilike", "%trash%"),
        ])
        self.assertEqual(len(crons), 0)
        # default = soft trash, never auto-purge: a just-trashed thread
        # survives the default-window (folder-wide) purge
        conv = self._trash_conv()
        n = self.Conversation.action_purge_trash(None, TYPED)
        self.assertEqual(n, 0)
        self.assertTrue(conv.exists())

    # -- shared send stubs (registry-level, mirror test_inbox_gateway) ----
    @contextmanager
    def _stub_send(self, fn):
        Mail = self.env.registry["mail.mail"]
        orig = Mail.send
        Mail.send = fn
        try:
            yield
        finally:
            Mail.send = orig

    @staticmethod
    def _ok_send(self, auto_commit=False, raise_exception=False,
                 post_send_callback=None):
        self.write({"state": "sent"})
        return True

    @staticmethod
    def _fail_send(self, auto_commit=False, raise_exception=False,
                   post_send_callback=None):
        raise MailDeliveryException("down", Exception("down"))


class TestOutboundIdentityAndSent(InboxTestCase):
    """R1/R4: retries reuse ONE mail.mail row (a single provider identity
    per message — Message-ID stays unique for webhook correlation) and the
    Sent folder lists a conversation once, never duplicated."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "0")
        self.partner = self.env["res.partner"].create({
            "name": "Identity Recv", "email": "identity@test.example"})

    def tearDown(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.intercept_outgoing", "1")
        super().tearDown()

    def _conv(self, subject="Rate quote: 6 pallets"):
        return self.Conversation.browse(self.ingest(subject=subject)[1].id)

    def _draft_msg(self, conv, subject="Re: quote"):
        res = conv.compose_and_send(subject, "<p>hi</p>", "reply", False,
                                    to_partner_ids=[self.partner.id])
        return self.Message.browse(res["id"])

    @contextmanager
    def _stub_send(self, fn):
        Mail = self.env.registry["mail.mail"]
        orig = Mail.send
        Mail.send = fn
        try:
            yield
        finally:
            Mail.send = orig

    @staticmethod
    def _ok_send(self, auto_commit=False, raise_exception=False,
                 post_send_callback=None):
        self.write({"state": "sent"})
        return True

    @staticmethod
    def _fail_send(self, auto_commit=False, raise_exception=False,
                   post_send_callback=None):
        raise MailDeliveryException("down", Exception("down"))

    def test_retry_reuses_single_mail_mail_row(self):
        conv = self._conv()
        msg = self._draft_msg(conv)
        with self._stub_send(self._fail_send):
            msg.send()
        self.assertEqual(msg.outbound_state, "failed")
        first_mail = msg.mail_mail_id
        self.assertTrue(first_mail)
        with self._stub_send(self._ok_send):
            msg.send()  # retry
        self.assertEqual(msg.outbound_state, "sent")
        self.assertEqual(msg.mail_mail_id.id, first_mail.id,
                         "retry must reuse the ONE ledger row")
        dupes = self.env["mail.mail"].search([
            ("message_id", "=", msg.message_id)])
        self.assertEqual(len(dupes), 1,
                         "provider identity (Message-ID) must stay unique")
        # the conversation appears ONCE in the Sent folder
        sent = self.Conversation._folder_conversations("sent")
        self.assertEqual(sent.filtered(lambda c: c.id == conv.id).ids,
                         [conv.id])

    def test_sent_folder_idempotent_and_excludes_trash(self):
        conv = self._conv()
        msg1 = self._draft_msg(conv, "Re: quote 1")
        msg2 = self._draft_msg(conv, "Re: quote 2")
        with self._stub_send(self._ok_send):
            msg1.send()
            msg2.send()
        self.assertTrue(msg1.mail_mail_id and msg2.mail_mail_id)
        self.assertNotEqual(msg1.mail_mail_id.id, msg2.mail_mail_id.id,
                            "each message has its OWN ledger row")
        # two sent messages → still ONE row for the conversation in Sent
        sent = self.Conversation._folder_conversations("sent")
        self.assertEqual(sent.filtered(lambda c: c.id == conv.id).ids,
                         [conv.id])
        # trashed threads leave the Sent folder with everything else
        conv.action_trash()
        self.assertNotIn(conv.id,
                         self.Conversation._folder_conversations("sent").ids)

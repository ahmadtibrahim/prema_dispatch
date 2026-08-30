# -*- coding: utf-8 -*-
"""Badge + realtime matrix (design §13 — Notifications)."""
import json

from .common import InboxTestCase


class TestInboxNotifications(InboxTestCase):

    def test_badge_0_to_1_to_2_to_3(self):
        """simulate_fetch ×3 drives the unread count 0→1→2→3."""
        self.assertEqual(
            self.Conversation._unread_counts_for_user()[self.env.user.id]["total"], 0)
        sim = self.Sim.simulate_fetch(scenario="demo", count=1)
        self.assertEqual(len(sim["messages"]), 1)
        counts = self.Conversation._unread_counts_for_user()[self.env.user.id]
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["spam"], 0)
        self.Sim.simulate_fetch(scenario="demo", count=2)
        counts = self.Conversation._unread_counts_for_user()[self.env.user.id]
        self.assertEqual(counts["total"], 3)

    def test_new_message_broadcast_payload(self):
        """Post-ingest broadcast writes bus rows with the §3 payload shape."""
        msg, conv, created = self.ingest()
        conv._broadcast_new_message(msg)
        self.assertTrue(created)
        self.assertGreater(self.bus_rows("prema_inbox"), 0)
        self.env.cr.execute(
            "SELECT message::text FROM bus_bus WHERE channel::text ILIKE "
            "'%%prema_inbox%%' ORDER BY id DESC LIMIT 1")
        payload = json.loads(self.env.cr.fetchone()[0])["payload"]
        for key in ("event_id", "type", "conversation_id", "message_id",
                    "unread_total", "category", "from_email", "subject"):
            self.assertIn(key, payload)
        self.assertEqual(payload["type"], "new_message")

    def test_read_state_and_broadcast(self):
        msg, conv, _ = self.ingest()
        self.assertFalse(msg.is_read)
        self.assertEqual(conv.unread_count, 1)
        msg.mark_read()
        self.assertTrue(msg.is_read)
        self.assertEqual(conv.unread_count, 0)
        # read change → bus event fired
        self.assertGreater(self.bus_rows("prema_inbox"), 0)
        msg.mark_unread()
        self.assertEqual(conv.unread_count, 1)

    def test_two_users_isolated_read_state(self):
        user = self.make_user()
        msg, conv, _ = self.ingest()
        counts_admin = self.Conversation.with_user(self.admin)._unread_counts_for_user()
        counts_other = self.Conversation.with_user(user)._unread_counts_for_user()
        self.assertEqual(counts_admin[self.admin.id]["total"], 1)
        self.assertEqual(counts_other[user.id]["total"], 1)
        # admin reads → admin 0, other still 1
        self.Message.with_user(self.admin).browse(msg.id).mark_read()
        counts_admin = self.Conversation.with_user(self.admin)._unread_counts_for_user()
        counts_other = self.Conversation.with_user(user)._unread_counts_for_user()
        self.assertEqual(counts_admin[self.admin.id]["total"], 0)
        self.assertEqual(counts_other[user.id]["total"], 1)

    def test_notes_outgoing_dupes_drafts_excluded_from_badge(self):
        msg, conv, _ = self.ingest()
        conv.compose_and_send("Re: x", "body", "note")            # note
        conv.compose_and_send("Re: y", "body", "reply")           # draft
        conv.compose_and_send("Re: z", "body", "reply", True)     # intercepted outgoing
        # duplicate delivery of the same Message-ID → no new message
        dup, conv2, created = self.ingest(message_id=msg.message_id)
        self.assertFalse(created)
        self.assertEqual(dup.id, msg.id)
        counts = self.Conversation._unread_counts_for_user()[self.env.user.id]
        self.assertEqual(counts["total"], 1)  # still exactly 1 incoming

    def test_spam_is_separate_count(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.spam_filter_active", "1")
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.spam_domains", "spam.example.com")
        _, conv, _ = self.ingest(
            email_from="Joe <joe@spam.example.com>", subject="WINNER!!!",
            body="You won a prize!")
        conv.is_spam = True  # quarantine classification
        counts = self.Conversation._unread_counts_for_user()[self.env.user.id]
        self.assertEqual(counts["total"], 0)
        self.assertEqual(counts["spam"], 1)

    def test_load_board_separate_breakdown(self):
        self.ingest(is_load_board=True, subject="RMIS Alert")
        counts = self.Conversation._unread_counts_for_user()[self.env.user.id]
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["load_board"], 1)

    def test_folder_queues(self):
        folders = {f["key"]: f for f in self.Conversation.inbox_folders()}
        self.assertEqual(folders["inbox"]["count"], 0)
        self.ingest(subject="Quote for 4 pallets")
        self.ingest(subject="RMIS Contact Change", is_load_board=True)
        folders = {f["key"]: f for f in self.Conversation.inbox_folders()}
        self.assertEqual(folders["inbox"]["count"], 2)
        self.assertEqual(folders["unread"]["count"], 2)
        # reading one drops unread but not inbox
        conv = self.Conversation.search([], limit=1)
        conv.inbox_message_ids.mark_read()
        folders = {f["key"]: f for f in self.Conversation.inbox_folders()}
        self.assertEqual(folders["inbox"]["count"], 2)
        self.assertEqual(folders["unread"]["count"], 1)

    def test_search_across_subject_body_partner(self):
        self.ingest(subject="Apples to Ottawa", body="10 pallets dry van")
        self.ingest(subject="Groceries", body="6 pallets reefer M5V to K8N")
        rows = self.Conversation.inbox_conversations("inbox", search="Apples")
        self.assertEqual(len(rows), 1)
        self.assertIn("Apples", rows[0]["name"])
        rows = self.Conversation.inbox_conversations("inbox", search="M5V")
        self.assertEqual(len(rows), 1)
        rows = self.Conversation.inbox_conversations("inbox", search="zzz-no-match")
        self.assertEqual(len(rows), 0)

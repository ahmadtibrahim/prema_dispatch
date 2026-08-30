# -*- coding: utf-8 -*-
"""Email actions matrix (design §13 — Email)."""
from .common import InboxTestCase


class TestInboxEmail(InboxTestCase):

    def test_reply_threads_back_into_same_conversation(self):
        _, conv, _ = self.ingest(subject="Rate quote: 6 pallets")
        sim = self.Sim.simulate_reply(conv.id)
        self.assertTrue(sim["threaded"])
        self.assertEqual(sim["conversation_id"], conv.id)
        self.assertEqual(len(conv.inbox_message_ids), 2)
        reply = self.Message.browse(sim["message_id"])
        self.assertTrue(reply.references)
        self.assertEqual(reply.in_reply_to,
                         "<%s>" % conv.inbox_message_ids[0].message_id)

    def test_no_subject_alone_grouping(self):
        """Two different senders with the same subject stay separate."""
        _, conv1, _ = self.ingest(subject="Same subject")
        _, conv2, _ = self.ingest(
            email_from="Other <other@test.example>", subject="Same subject")
        self.assertNotEqual(conv1.id, conv2.id)
        self.assertEqual(len(conv1.inbox_message_ids), 1)
        self.assertEqual(len(conv2.inbox_message_ids), 1)

    def test_draft_persistence_and_send_intercept(self):
        _, conv, _ = self.ingest()
        res = conv.compose_and_send("Re: quote", "Draft body", "reply")
        draft = self.Message.browse(res["id"])
        self.assertEqual(draft.outbound_state, "draft")
        self.assertEqual(draft.direction, "outgoing")
        self.assertEqual(draft.email_from, "dispatcher@logistics.premafirm.com")
        # drafts land in the Drafts folder
        drafts = self.Conversation.inbox_conversations("drafts")
        self.assertTrue(any(c["id"] == conv.id for c in drafts))

    def test_send_is_intercepted_never_smtp(self):
        _, conv, _ = self.ingest()
        before = self.env["mail.mail"].search_count(
            [("email_from", "ilike", "dispatcher@")])
        res = conv.compose_and_send("Re: quote", "Body", "reply", True)
        msg = self.Message.browse(res["id"])
        self.assertEqual(msg.outbound_state, "intercepted")
        # no mail.mail rows were ever created — zero real sends (the clone
        # carries prod's historic mail.mail rows, so compare a delta)
        self.assertEqual(
            self.env["mail.mail"].search_count(
                [("email_from", "ilike", "dispatcher@")]), before)

    def test_retry_send_no_duplicate(self):
        _, conv, _ = self.ingest()
        res = conv.compose_and_send("Re: quote", "Body", "reply", True)
        msg = self.Message.browse(res["id"])
        msg.send()  # idempotent retry
        self.assertEqual(msg.outbound_state, "intercepted")
        self.assertEqual(len(conv.inbox_message_ids), 2)  # original + 1 outbound

    def test_internal_note_never_emailed(self):
        _, conv, _ = self.ingest()
        before = self.env["mail.mail"].search_count(
            [("email_from", "ilike", "dispatcher@")])
        res = conv.compose_and_send("note subject", "internal thought", "note")
        note = self.Message.browse(res["id"])
        self.assertEqual(note.direction, "note")
        self.assertNotIn("outgoing", note.direction)
        self.assertEqual(
            self.env["mail.mail"].search_count(
                [("email_from", "ilike", "dispatcher@")]), before)
        # notes never appear in unread counts and never in Sent
        sent = self.Conversation.inbox_conversations("sent")
        self.assertFalse(any(c["id"] == conv.id for c in sent))

    def test_dedupe_by_message_id(self):
        msg, conv, created = self.ingest()
        self.assertTrue(created)
        again, conv2, created2 = self.ingest(message_id=msg.message_id)
        self.assertFalse(created2)
        self.assertEqual(conv2.id, conv.id)
        self.assertEqual(len(conv.inbox_message_ids), 1)

    def test_attachment_preserved(self):
        attach = self.env["ir.attachment"].create({
            "name": "POD.pdf", "raw": b"%PDF-1.4 fake",
            "mimetype": "application/pdf",
        })
        msg, conv, _ = self.ingest(
            message_id="<with-attach@prema-inbox>")
        msg.write({"attachment_ids": [(4, attach.id)]})
        self.assertIn(attach, msg.attachment_ids)
        detail = self.Conversation.inbox_conversation_detail(conv.id)
        self.assertTrue(any(
            a["name"] == "POD.pdf"
            for m in detail["messages"] for a in m["attachments"]))

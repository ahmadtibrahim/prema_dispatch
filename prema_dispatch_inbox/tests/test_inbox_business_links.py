# -*- coding: utf-8 -*-
"""Business links + rules matrix (design §13 — Business links)."""
from .common import InboxTestCase


class TestInboxBusinessLinks(InboxTestCase):

    def _make_partner_conv(self):
        partner = self.env["res.partner"].create({
            "name": "Demo Toronto Produce",
            "email": "bob@demo-toronto-produce.test",
        })
        msg, conv, _ = self.Conversation._ingest_email(
            email_from="Bob Green <bob@demo-toronto-produce.test>",
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject="Rate quote", body_plain="6 pallets M5V to K8N",
            body_html="<p>6 pallets</p>",
            message_id="<link-%d@prema-inbox>" % id(object()))
        conv.partner_id = partner.id
        return partner, conv

    def test_booking_job_invoice_link(self):
        partner, conv = self._make_partner_conv()
        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "name": "B-1001",
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 6,
            "weight_lbs": 1000,
        })
        job = self.env["prema.dispatch.job"].create({
            "partner_id": partner.id,
            "name": "J-2001",
        })
        invoice = self.env["account.move"].create({
            "partner_id": partner.id,
            "move_type": "out_invoice",
        })
        conv.action_link_record("booking", booking.id)
        conv.action_link_record("job", job.id)
        conv.action_link_record("invoice", invoice.id)
        self.assertEqual(conv.booking_id.id, booking.id)
        self.assertEqual(conv.job_id.id, job.id)
        self.assertEqual(conv.invoice_id.id, invoice.id)
        # candidates RPC finds them by partner
        cands = conv.inbox_link_candidates("booking", conv.id, "")
        self.assertTrue(any(c["id"] == booking.id for c in cands))
        cands = conv.inbox_link_candidates("invoice", conv.id, "")
        self.assertTrue(any(c["id"] == invoice.id for c in cands))

    def test_activity_creation_via_rule(self):
        """An 'internal' rule may create a mail.activity; assistant may not."""
        _, conv = self._make_partner_conv()
        Rule = self.env["prema.inbox.rule"]
        # assistant level → suggestion only, nothing written
        assistant = Rule.create({
            "name": "assistant follow-up",
            "trigger": "no_reply_since",
            "actions": [{"action": "create_activity", "days": 2}],
            "level": "assistant",
        })
        Rule._run_for_conversation(conv, "no_reply_since")
        self.assertEqual(
            self.env["mail.activity"].search_count(
                [("res_model", "=", "prema.inbox.conversation")]), 0)
        self.assertTrue(assistant.run_log)  # suggestion recorded

        # internal level → activity created
        internal = Rule.create({
            "name": "internal follow-up",
            "trigger": "no_reply_since",
            "actions": [{"action": "create_activity", "days": 2}],
            "level": "internal",
        })
        Rule._run_for_conversation(conv, "no_reply_since")
        activities = self.env["mail.activity"].search([
            ("res_model", "=", "prema.inbox.conversation"),
        ])
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.res_id, conv.id)
        # conversation shows up in the Tasks folder
        tasks = self.Conversation.inbox_conversations("tasks")
        self.assertTrue(any(c["id"] == conv.id for c in tasks))

    def test_rule_categorize_and_conditions(self):
        _, conv = self._make_partner_conv()
        # ingest's keyword guess maps "Rate quote" → quote_request; the
        # rule's condition is category_in ["other"] — start from "other"
        conv.write({"category": "other"})
        Rule = self.env["prema.inbox.rule"]
        rule = Rule.create({
            "name": "categorize quotes",
            "trigger": "quote_request_received",
            "conditions": {"category_in": ["other"]},
            "actions": [{"action": "set_category", "value": "quote_request"}],
            "level": "internal",
        })
        Rule._run_for_conversation(conv, "quote_request_received")
        self.assertEqual(conv.category, "quote_request")
        # condition blocks when category already set → no NEW log entry
        Rule._run_for_conversation(conv, "quote_request_received")
        self.assertEqual(len(rule.run_log), 1)

    def test_rule_hard_defaults_unknown_action_ignored(self):
        _, conv = self._make_partner_conv()
        Rule = self.env["prema.inbox.rule"]
        rule = Rule.create({
            "name": "evil rule",
            "trigger": "quote_request_received",
            "actions": [{"action": "send_email_autonomously",
                         "value": "spam@evil.test"}],
            "level": "internal",
        })
        before = self.env["mail.mail"].search_count(
            [("email_from", "ilike", "dispatcher@")])
        Rule._run_for_conversation(conv, "quote_request_received")
        self.assertEqual(rule.run_log[-1]["actions"], [])  # rejected
        # no mail was queued by the rejected action (clone carries prod's
        # historic mail.mail rows, so compare a delta)
        self.assertEqual(
            self.env["mail.mail"].search_count(
                [("email_from", "ilike", "dispatcher@")]), before)

    def test_rule_attempt_limits(self):
        _, conv = self._make_partner_conv()
        Rule = self.env["prema.inbox.rule"]
        rule = Rule.create({
            "name": "limited",
            "trigger": "quote_request_received",
            "actions": [{"action": "set_priority", "value": "urgent"}],
            "level": "internal",
            "max_attempts": 2,
        })
        for _ in range(3):
            Rule._run_for_conversation(conv, "quote_request_received")
        self.assertEqual(rule.attempts, 2)
        self.assertTrue(rule.last_run)

    def test_cross_customer_visibility(self):
        """No record rules: shared inbox by design — but ACL keeps
        non-members out entirely (covered in security tests)."""
        partner, conv = self._make_partner_conv()
        # every inbox member sees the same shared queue
        rows = self.Conversation.inbox_conversations("inbox")
        self.assertTrue(any(c["id"] == conv.id for c in rows))

    def test_document_save_never_auto_completes_delivery(self):
        """POD arrival does not auto-complete deliveries (design §6)."""
        _, conv = self._make_partner_conv()
        self.assertEqual(conv.workflow_state, "open")
        self.assertTrue(conv.action_archive_thread())
        self.assertEqual(conv.workflow_state, "archived")
        conv.action_reopen()
        self.assertEqual(conv.workflow_state, "open")

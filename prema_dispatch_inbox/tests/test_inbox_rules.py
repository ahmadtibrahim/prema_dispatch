# -*- coding: utf-8 -*-
"""Automations matrix (design §9 — conservative defaults, three levels)."""
from .common import InboxTestCase


class TestInboxRules(InboxTestCase):

    def _conv(self):
        msg, conv, _ = self.ingest(subject="Quote please")
        # ingest's keyword guess maps "Quote please" → quote_request; reset
        # so each test runs from a known "other" baseline
        conv.write({"category": "other"})
        return conv

    def test_rule_lifecycle_fields(self):
        rule = self.env["prema.inbox.rule"].create({
            "name": "quote ack", "trigger": "quote_request_received",
            "conditions": {"hours": True},
            "actions": [{"action": "flag_needs_review"}],
        })
        self.assertEqual(rule.level, "assistant")  # conservative default
        self.assertTrue(rule.active)
        self.assertEqual(rule.sequence, 10)

    def test_assistant_level_never_writes(self):
        conv = self._conv()
        rule = self.env["prema.inbox.rule"].create({
            "name": "assistant",
            "trigger": "quote_request_received",
            "actions": [
                {"action": "set_category", "value": "quote_request"},
                {"action": "assign_to", "value": self.admin.id},
            ],
            "level": "assistant",
        })
        self.env["prema.inbox.rule"]._run_for_conversation(
            conv, "quote_request_received")
        self.assertEqual(conv.category, "other")   # unchanged
        self.assertFalse(conv.assignee_id)          # unchanged
        log = rule.run_log[-1]["actions"]
        self.assertTrue(all("suggested" in a for a in log))

    def test_internal_level_writes(self):
        conv = self._conv()
        rule = self.env["prema.inbox.rule"].create({
            "name": "internal",
            "trigger": "quote_request_received",
            "actions": [
                {"action": "set_category", "value": "quote_request"},
                {"action": "set_priority", "value": "urgent"},
            ],
            "level": "internal",
        })
        self.env["prema.inbox.rule"]._run_for_conversation(
            conv, "quote_request_received")
        self.assertEqual(conv.category, "quote_request")
        self.assertEqual(conv.priority, "urgent")

    def test_external_level_never_executes(self):
        conv = self._conv()
        rule = self.env["prema.inbox.rule"].create({
            "name": "external",
            "trigger": "quote_request_received",
            "actions": [{"action": "set_priority", "value": "emergency"}],
            "level": "external",
        })
        self.env["prema.inbox.rule"]._run_for_conversation(
            conv, "quote_request_received")
        self.assertEqual(conv.priority, "normal")  # never touched
        self.assertFalse(rule.run_log)             # not even logged

    def test_missing_extraction_condition(self):
        conv = self._conv()
        rule = self.env["prema.inbox.rule"].create({
            "name": "need weight",
            "trigger": "quote_request_received",
            "conditions": {"missing_extraction": ["weight_lbs"]},
            "actions": [{"action": "flag_needs_review"}],
            "level": "internal",
        })
        # no extraction yet → condition NOT met (missing is present)
        self.env["prema.inbox.rule"]._run_for_conversation(
            conv, "quote_request_received")
        self.assertFalse(rule.run_log)
        # extraction with weight → condition met → flags needs review
        conv.write({"ai_extraction": {
            "fields": {"weight_lbs": 4200, "pallets": 6}, "sources": {},
            "missing": [], "conflicting": []}})
        self.env["prema.inbox.rule"]._run_for_conversation(
            conv, "quote_request_received")
        self.assertEqual(conv.category, "needs_review")

    def test_run_log_bounded(self):
        conv = self._conv()
        rule = self.env["prema.inbox.rule"].create({
            "name": "loggy",
            "trigger": "quote_request_received",
            "actions": [{"action": "set_priority", "value": "urgent"}],
            "level": "internal",
            "max_attempts": 60,
        })
        for _ in range(55):
            self.env["prema.inbox.rule"]._run_for_conversation(
                conv, "quote_request_received")
        self.assertLessEqual(len(rule.run_log), 50)

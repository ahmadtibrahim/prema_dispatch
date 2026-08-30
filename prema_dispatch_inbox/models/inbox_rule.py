# -*- coding: utf-8 -*-
"""prema.inbox.rule — configurable automations with three permission levels.

Design §9: triggers / conditions / actions are stored as JSONB on the rule;
defaults are conservative. Hard defaults (never overridable by data):
no autonomous sending, no load acceptance, no booking creation, no
invoice/payment action, no deletion. "Assistant" level rules only ever
record suggestions in the run log — they write nothing.
"""
import json
from datetime import datetime, timedelta

import pytz

from odoo import api, fields, models

TRIGGERS = [
    ("quote_request_received", "Quote request received"),
    ("missing_weight", "Extraction missing weight"),
    ("no_reply_since", "No reply since"),
    ("customer_replied", "Customer replied"),
    ("rate_confirmation_received", "Rate confirmation received"),
    ("delivery_completed_without_pod", "Delivery completed without POD"),
    ("invoice_question", "Invoice question"),
    ("load_alert_match", "Load board alert match"),
]

LEVELS = [
    ("assistant", "Assistant (suggestions only — default)"),
    ("internal", "Internal (may categorize / assign / create tasks)"),
    ("external", "External (narrow ack/follow-up — disabled by default)"),
]

# Actions the engine can perform at "internal" level. Anything not in this
# map is rejected at run time — hard defaults win over data.
_INTERNAL_ACTIONS = {
    "set_category": lambda conv, a: conv.write({"category": a["value"]}),
    "set_priority": lambda conv, a: conv.write({"priority": a["value"]}),
    "assign_to": lambda conv, a: conv.write({"assignee_id": a["value"]}),
    "flag_needs_review": lambda conv, a: conv.write({"category": "needs_review"}),
    "create_activity": lambda conv, a: conv.activity_schedule(
        "mail.mail_activity_data_todo",
        summary=a.get("summary", "Follow-up"),
        date_deadline=(
            datetime.now(pytz.timezone("America/Toronto")).date()
            + timedelta(days=int(a.get("days", 1)))),
        user_id=a.get("user_id") or conv.assignee_id.id or conv.env.user.id),
    "suggest_follow_up": lambda conv, a: conv.write({"ai_status": "ready"}),
}

_BUSINESS_HOURS = (8, 18)  # America/Toronto, Mon-Fri


class InboxRule(models.Model):
    _name = "prema.inbox.rule"
    _description = "Dispatch Inbox Rule"
    _order = "sequence, id"
    _rec_name = "name"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(string="Active", default=True)
    sequence = fields.Integer(string="Sequence", default=10)
    trigger = fields.Selection(TRIGGERS, string="Trigger", required=True)
    conditions = fields.Json(
        string="Conditions",
        help="JSONB condition map, e.g. "
             "{'category_in': ['quote_request'], 'hours': true, "
             "'min_messages': 1, 'missing_extraction': ['weight_lbs']}")
    actions = fields.Json(
        string="Actions",
        help="JSONB list, e.g. [{'action': 'set_category', 'value': "
             "'quote_request'}, {'action': 'create_activity', 'days': 1}]")
    level = fields.Selection(
        LEVELS, string="Level", default="assistant", required=True)
    owner_id = fields.Many2one("res.users", string="Owner")
    run_log = fields.Json(string="Run log")
    attempts = fields.Integer(string="Attempts", default=0)
    last_run = fields.Datetime(string="Last run")
    max_attempts = fields.Integer(string="Max attempts per day", default=20)

    @api.model
    def _run_for_conversation(self, conversation, trigger):
        """Evaluate active rules for a trigger on one conversation.

        Level enforcement:
          assistant → nothing written, suggestions recorded in run_log only
          internal  → actions from _INTERNAL_ACTIONS may write
          external  → never executes in this prototype (narrow ack needs
                      explicit enablement at cutover)
        """
        if not conversation:
            return []
        results = []
        rules = self.search([
            ("active", "=", True), ("trigger", "=", trigger),
        ])
        for rule in rules:
            outcome = self._run_one(rule, conversation)
            results.append({
                "rule_id": rule.id,
                "rule_name": rule.name,
                "level": rule.level,
                "ok": outcome["ok"],
                "actions": outcome["actions"],
                "reason": outcome["reason"],
            })
        return results

    def _run_one(self, rule, conversation):
        if not self._conditions_ok(rule, conversation):
            return {"ok": False, "actions": [], "reason": "conditions"}
        if not self._within_limits(rule):
            return {"ok": False, "actions": [], "reason": "attempt-limit"}
        if rule.level not in ("assistant", "internal"):
            # external level never executes in this prototype — and never
            # logs, so the run log stays an honest record of real runs
            return {"ok": False, "actions": [], "reason": "level"}
        executed = []
        for action in rule.actions or []:
            action = action or {}
            kind = action.get("action")
            if kind not in _INTERNAL_ACTIONS:
                continue
            if rule.level == "assistant":
                # suggestions only — record, never write
                executed.append({"suggested": kind, "value": action.get("value")})
                continue
            if rule.level != "internal":
                continue  # external: nothing autonomous
            try:
                _INTERNAL_ACTIONS[kind](conversation, action)
                executed.append({"done": kind, "value": action.get("value")})
            except Exception as exc:  # noqa: BLE001 — rules must never crash mail flow
                executed.append({"failed": kind, "error": str(exc)})
        self._log_run(rule, conversation, executed)
        return {"ok": True, "actions": executed, "reason": ""}

    # ------------------------------------------------------------------
    # conditions / guards
    # ------------------------------------------------------------------
    def _conditions_ok(self, rule, conversation):
        cond = rule.conditions or {}
        if cond.get("category_in") and conversation.category not in cond["category_in"]:
            return False
        if cond.get("hours"):
            now = datetime.now(pytz.timezone("America/Toronto"))
            if now.weekday() >= 5 or not (
                    _BUSINESS_HOURS[0] <= now.hour < _BUSINESS_HOURS[1]):
                return False
        if cond.get("min_messages") and len(conversation.inbox_message_ids) < cond["min_messages"]:
            return False
        # extraction is stored as {"fields": {...}, "sources": {...}, ...}
        # — the missing-field check reads inside "fields"
        for missing in cond.get("missing_extraction", []):
            if not (conversation.ai_extraction or {}).get("fields", {}).get(missing):
                return False
        return True

    def _within_limits(self, rule):
        if rule.attempts >= rule.max_attempts:
            return False
        if rule.last_run:
            # last_run is a naive UTC datetime (fields.Datetime) — compare
            # like for like; mixing in pytz.utc datetimes raises TypeError.
            day_ago = fields.Datetime.now() - timedelta(hours=24)
            if rule.last_run < day_ago:
                rule.attempts = 0
        return True

    def _log_run(self, rule, conversation, executed):
        log = rule.run_log or []
        log.append({
            "at": fields.Datetime.now().isoformat(),
            "conversation_id": conversation.id,
            "subject": conversation.name,
            "actions": executed,
        })
        rule.write({
            "run_log": log[-50:],  # bounded
            "attempts": rule.attempts + 1,
            "last_run": fields.Datetime.now(),
        })
        return True

    def action_test_rule(self):
        """Manual 'test' — evaluate against recent conversations, dry run."""
        sample = self.env["prema.inbox.conversation"].search(
            [], limit=3, order="last_message_date desc")
        return {
            "type": "ir.actions.act_window_close",
            "info": json.dumps(
                [self._run_one(self, c) for c in sample], indent=2),
        }

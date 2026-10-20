# -*- coding: utf-8 -*-
"""18.0.1.3.3 — repair narrowly identified double-escaped internal notes.

Odoo 18 escapes a plain ``str`` body passed to ``message_post``. The mail
tracking, CRM operator and COI verification code passed HTML strings, so
their internal notes render literal tags. New notes are posted as Markup.
This one-time pass corrects ONLY the known note families, unescapes one level
and sanitizes the result, so untrusted subject/name text cannot become active
HTML. Sent emails (``mail.mail`` and outgoing email messages) are untouched;
affected messages are corrected in place, never deleted.
"""
import html as html_lib
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import html_sanitize

_logger = logging.getLogger(__name__)

# (model, exact body prefix)
NOTE_PATTERNS = [
    ("crm.lead", "<p>&lt;p&gt;👁️ &lt;b&gt;Email opened"),
    ("crm.lead", "<p>&lt;p&gt;⚠️ &lt;b&gt;Email bounced"),
    ("crm.lead", "<p>&lt;p&gt;⚠️ &lt;b&gt;Spam complaint"),
    ("crm.lead", "<p>&lt;p&gt;ℹ️ &lt;b&gt;Delivery delayed"),
    ("hr.applicant", "<p>&lt;p&gt;👁️ &lt;b&gt;Email opened"),
    ("crm.lead",
     "<p>Prema AI moved this opportunity from &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI created a &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI completed the &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI assigned this opportunity from"),
    ("crm.lead", "<p>Prema AI added tag &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI removed tag &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI marked this opportunity as &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI paused this opportunity (moved from &lt;b&gt;"),
    ("crm.lead", "<p>Prema AI resumed this opportunity (moved from &lt;b&gt;"),
    ("crm.lead", "<p>&lt;b&gt;📬 AI Follow-up Draft"),
    ("crm.lead", "<p>&lt;b&gt;Snov.io Escalation:"),
    ("prema.owner.operator.application",
     "<p>&lt;p&gt;&lt;strong&gt;Verification email"),
    ("discuss.channel",
     "<p>&lt;p&gt;&lt;b&gt;Hi Admin 👋 — Your PremaFirm Activity Summary"),
    ("discuss.channel", "<p>&lt;h3&gt;📊 Staff Daily Report"),
    ("discuss.channel", "<p>&lt;b&gt;New load tender needs review"),
    ("discuss.channel", "<p>&lt;i&gt;📄 Processing load tender from"),
]


def correct_note_bodies(env):
    """Return the number of messages corrected. Idempotent."""
    Message = env["mail.message"].with_context(active_test=False)
    corrected = 0
    for model, prefix in NOTE_PATTERNS:
        messages = Message.search([
            ("model", "=", model),
            ("message_type", "in", ("comment", "notification", "email")),
            ("body", "=like", prefix + "%"),
        ])
        for message in messages:
            body = message.body or ""
            if not body.startswith(prefix):
                continue
            # Never alter a genuinely outgoing email.
            if message.message_type == "email" and \
                    message.subtype_id != env.ref(
                        "mail.mt_note", raise_if_not_found=False):
                continue
            fixed = html_sanitize(html_lib.unescape(body))
            if fixed and fixed != body:
                message.write({"body": fixed})
                corrected += 1
    if corrected:
        _logger.info("Corrected %s double-escaped internal notes", corrected)
    return corrected


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    correct_note_bodies(env)

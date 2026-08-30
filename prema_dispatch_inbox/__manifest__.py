{
    "name": "Prema Dispatch Inbox",
    "version": "18.0.0.1.0",
    "summary": "Shared dispatch inbox for dispatcher@logistics.premafirm.com — conversations, follow-ups, CRM/booking/invoice links, AI assistant, pricing engine integration",
    "category": "Logistics",
    "description": """
Prema Dispatch Inbox (UAT prototype)
====================================

A native shared Dispatch Inbox for dispatcher@logistics.premafirm.com:

* Folders/queues: Inbox, Unread, Needs Review, Quote Requests, Load
  Opportunities, Active Shipments, Waiting for Reply, Tasks, Drafts, Sent,
  Archived, Spam/Quarantine, Rules & Automation.
* One canonical conversation per thread (dedupe by Message-ID, threading by
  References/In-Reply-To). Per-message, per-user read state — personal unread
  is separate from the shared workflow state (Open / Waiting / Completed).
* Top-bar envelope with live unread badge (bus websocket, post-commit),
  gentle pulse, non-blocking toast, optional sound/desktop alerts; separate
  spam count; load-board alerts can be muted while keeping their count.
* Email actions: compose / reply / reply-all / forward / internal note,
  draft autosave, attachments, assign, priority, snooze, read/unread,
  archive/reopen, mark waiting/completed. Outbound is INTERCEPTED in UAT
  (never sent over SMTP) and preserves Message-ID/References/In-Reply-To.
* Business links: tasks (mail.activity), optional CRM opportunity,
  booking/job, invoice (read-only), save documents.
* AI assistant on the existing DeepSeek runtime (premafirm_ai_engine):
  summarize, draft reply, extract shipment with per-field source + confidence,
  missing/conflicting flags, follow-up suggestions. Email content is treated
  as untrusted data (prompt-injection safe); no privileged actions.
* Prema Pricing Engine integration: extraction resolves to FSAs and calls the
  existing deterministic PricingService — no second calculator, no invented
  rates. Price snapshot persisted on the conversation.
* Configurable rules (triggers/conditions/actions/owner/audit) with three
  permission levels; defaults are conservative (no autonomous sending).

UAT ONLY: dev-gated simulated fetch controller (/prema_inbox/simulate_fetch,
refuses unless prema_inbox.uat_mode is set). This module is a UAT prototype
and must never be installed on the production database.
""",
    "depends": ["prema_dispatch", "prema_logistics_booking", "premafirm_ai_engine", "documents", "mail", "bus", "account", "crm"],
    "data": [
        "security/inbox_security.xml",
        "security/ir.model.access.csv",
        "data/inbox_data.xml",
        "views/inbox_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "prema_dispatch_inbox/static/src/scss/inbox.scss",
            "prema_dispatch_inbox/static/src/xml/inbox_app.xml",
            "prema_dispatch_inbox/static/src/js/inbox_badge.js",
            "prema_dispatch_inbox/static/src/js/inbox_app.js",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}

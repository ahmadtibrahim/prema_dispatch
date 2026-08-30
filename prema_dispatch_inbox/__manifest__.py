{
    "name": "Prema Dispatch Inbox",
    "version": "18.0.1.2.0",
    "summary": "Shared dispatch inbox for dispatcher@logistics.premafirm.com — conversations, follow-ups, CRM/booking/invoice links, AI assistant, pricing engine integration",
    "category": "Logistics",
    "description": """
Prema Dispatch Inbox
====================

The production Dispatch Inbox for dispatcher@logistics.premafirm.com,
available inside Prema Dispatch on erp.premafirm.com.

* Folders/queues: Inbox, Unread, Needs Review, Quote Requests, Load
  Opportunities, Active Shipments, Waiting for Reply, Tasks, Drafts, Sent,
  Archived, Spam/Quarantine, Rules & Automation.
* One canonical conversation per thread (dedupe by Message-ID, threading by
  References/In-Reply-To). Per-message, per-user read state — personal unread
  is separate from the shared workflow state (Open / Waiting / Completed).
* Top-bar envelope beside the app title with live unread badge (bus
  websocket), gentle pulse (respects prefers-reduced-motion), non-blocking
  toast, optional sound; separate spam count; load-board alerts can be
  muted while keeping their count.
* Email actions: compose / reply / reply-all / forward / internal note,
  draft autosave, attachments, assign, priority, read/unread,
  archive/reopen, mark waiting/completed. Outbound goes through the
  configured mail server From dispatcher@logistics.premafirm.com with
  Reply-To pinned to the same address and Message-ID/References preserved
  (replies thread back into the same conversation). Status is honest:
  draft → pending (queued) → sent | failed.
* Incoming routing: the dispatcher@logistics.premafirm.com alias and the
  fetchmail object_id route into prema.inbox.conversation.message_new —
  same dedupe and threading as the UI; attachments preserved.
* Business links: tasks (mail.activity), optional CRM opportunity,
  booking/job, invoice (read-only), save documents. Internal notes never
  leave the company.
* AI assistant on the existing DeepSeek runtime (premafirm_ai_engine):
  summarize, draft reply, extract shipment with per-field source + confidence,
  missing/conflicting flags, follow-up suggestions. Email content is treated
  as untrusted data (prompt-injection safe); no privileged actions. AI
  never sends email, never books, never posts invoices or payments.
* Prema Pricing Engine integration: extraction resolves to FSAs and calls the
  existing deterministic PricingService — no second calculator, no invented
  rates. Price snapshot persisted on the conversation; quoted replies
  require human review.
* Configurable rules (triggers/conditions/actions/owner/audit) with three
  permission levels; defaults are conservative (no autonomous sending).

Safety gates: prema_inbox.uat_mode (fetch-sim refuses outside UAT clones),
prema_inbox.intercept_outgoing (default "1" — production sets "0" only
after cutover acceptance), prema_inbox.ai_mode (mock | live).
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

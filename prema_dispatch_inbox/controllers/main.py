# -*- coding: utf-8 -*-
"""Dev-gated simulated fetch + badge RPC.

simulate_fetch / simulate_reply: create synthetic messages (UAT only),
COMMIT, then broadcast the same post-commit bus events the real fetch path
will use. Guards: inbox group membership AND the fetch-sim UAT gate.

unread_counts: badge reconcile RPC — server truth always wins.
"""
import logging

from odoo import http
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class PremaInboxController(http.Controller):

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    def _require_inbox_user(self):
        if not http.request.env.user.has_group(
                "prema_dispatch_inbox.group_dispatch_inbox"):
            raise AccessError("Dispatch Inbox is not available to this user.")
        return http.request.env.user

    # ------------------------------------------------------------------
    # badge reconcile RPC
    # ------------------------------------------------------------------
    @http.route("/prema_inbox/unread_counts", type="json", auth="user")
    def unread_counts(self):
        user = self._require_inbox_user()
        counts = http.request.env["prema.inbox.conversation"]._unread_counts_for_user()
        return {"user_id": user.id, "counts": counts[user.id]}

    # ------------------------------------------------------------------
    # UAT simulated fetch (dev-gated)
    # ------------------------------------------------------------------
    @http.route("/prema_inbox/simulate_fetch", type="json", auth="user")
    def simulate_fetch(self, scenario="demo", count=1):
        self._require_inbox_user()
        sim = http.request.env["prema.inbox.fetch.sim"].simulate_fetch(
            scenario=scenario, count=count)
        # Broadcast FIRST (bus._sendone buffers in cr.precommit), then
        # commit — the precommit flush writes the bus_bus rows in the same
        # commit that makes the messages durable (same as the real fetch
        # path will do).
        for row in sim["messages"]:
            conv = http.request.env["prema.inbox.conversation"].browse(
                row["conversation_id"])
            msg = http.request.env["prema.inbox.message"].browse(
                row["message_id"])
            conv._broadcast_new_message(msg)
        http.request.env.cr.commit()
        _logger.info("inbox simulate_fetch(%s, %s) → %s",
                     scenario, count, len(sim["messages"]))
        return sim

    @http.route("/prema_inbox/simulate_reply", type="json", auth="user")
    def simulate_reply(self, conversation_id):
        self._require_inbox_user()
        sim = http.request.env["prema.inbox.fetch.sim"].simulate_reply(
            conversation_id)
        conv = http.request.env["prema.inbox.conversation"].browse(
            sim["conversation_id"])
        msg = http.request.env["prema.inbox.message"].browse(
            sim["message_id"])
        conv._broadcast_new_message(msg)
        http.request.env.cr.commit()
        return sim

# -*- coding: utf-8 -*-
"""Dev-gated simulated fetch + badge RPC + attachment serving.

simulate_fetch / simulate_reply: create synthetic messages (UAT only),
COMMIT, then broadcast the same post-commit bus events the real fetch path
will use. Guards: inbox group membership AND the fetch-sim UAT gate.

unread_counts: badge reconcile RPC — server truth always wins.

attachment_open: the ONLY way attachments are served — authenticated
(auth="user"), inbox-group gated, and restricted to attachments actually
bound to an inbox message. A public endpoint is deliberately not provided.
"""
import logging
from urllib.parse import quote

from odoo import http
from odoo.exceptions import AccessError
from werkzeug.exceptions import NotFound

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

    # ------------------------------------------------------------------
    # attachment open / preview / download (authenticated, inbox-group)
    # ------------------------------------------------------------------
    @http.route("/prema_inbox/attachment/<int:attachment_id>/<path:name>",
                type="http", auth="user", website=False, csrf=False)
    def attachment_open(self, attachment_id, name, download=False):
        """Serve an inbox-message attachment.

        Authorized route — NOT public: requires a logged-in inbox-group
        member, and the attachment must be bound to a prema.inbox.message
        row (no arbitrary /web/content-style access to any file). The rel
        table is checked in SQL because ir.attachment ACLs alone would
        leak any attachment the user can read; the inbox group is the
        boundary. ?download=1 forces Content-Disposition: attachment;
        otherwise images/PDFs preview inline in a new tab (no remote
        content is ever fetched server-side).
        """
        self._require_inbox_user()
        attachment = http.request.env["ir.attachment"].browse(attachment_id)
        if not attachment.exists():
            raise NotFound()
        http.request.env.cr.execute(
            """
            SELECT message_id
              FROM prema_inbox_message_attachment_rel
             WHERE attachment_id = %s
             LIMIT 1
            """, (attachment_id,))
        if not http.request.env.cr.fetchone():
            # Not an inbox attachment — no cross-app file serving here.
            raise NotFound()
        # The authorization boundary is the inbox group + the rel-table
        # check above — NOT ir.attachment ownership rules. Fetch-path
        # attachments are owned by the ingesting user with res_id=0, so a
        # normal read would deny every dispatcher; the shared inbox is the
        # whole point. sudo the byte read only AFTER both checks passed.
        attachment = attachment.sudo()
        data = attachment.raw or b""
        mimetype = attachment.mimetype or "application/octet-stream"
        filename = name or attachment.name or "attachment"
        if download or not (
                mimetype.startswith("image/")
                or mimetype == "application/pdf"):
            disposition = "attachment"
        else:
            disposition = "inline"
        # UTF-8-safe RFC 5987 filename; also keep a plain ASCII fallback.
        ascii_name = "".join(
            c if ord(c) < 128 else "_" for c in filename)
        headers = [
            ("Content-Type", mimetype),
            ("Content-Disposition",
             "%s; filename=\"%s\"; filename*=UTF-8''%s"
             % (disposition, ascii_name, quote(filename))),
            ("Content-Length", str(len(data))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, max-age=300"),
        ]
        return http.request.make_response(data, headers=headers)

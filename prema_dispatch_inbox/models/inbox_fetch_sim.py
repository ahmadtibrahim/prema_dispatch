# -*- coding: utf-8 -*-
"""prema.inbox.fetch.sim — dev-gated simulated fetch.

The controller /prema_inbox/simulate_fetch creates synthetic messages with
proper Message-IDs and fires the same post-commit bus broadcast the real
fetch path will use. The gate refuses to run outside UAT: ICP
prema_inbox.uat_mode AND (config file path or database name) must look like
a UAT/inbox environment. This module is never part of a production release.
"""
import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Synthetic demo scenario (approved mockup): Demo Toronto Produce books a
# 6-pallet reefer move Toronto (M5V) → Belleville (K8N).
_DEMO_SCENARIO = [
    {
        "email_from": "Bob Green <bob@demo-toronto-produce.test>",
        "to_addrs": ["dispatcher@logistics.premafirm.com"],
        "subject": "Rate quote: 6 pallets reefer Toronto to Belleville",
        "body_plain": (
            "Hi,\n\nWe need a truck for 6 pallets of mixed produce (reefer, "
            "3C), pickup Wednesday 9am at our Toronto warehouse, deliver "
            "Belleville same day. Need a liftgate at pickup. Weight about "
            "4200 lbs. Please send your best rate.\n\n"
            "Bob Green\nDemo Toronto Produce\n"
            "Pickup: 300 Progress Ave, Toronto ON M5V3E1\n"
            "Delivery: 55 Station St, Belleville ON K8N2S1"),
        "body_html": ("<p>Hi,</p><p>We need a truck for 6 pallets of mixed "
                      "produce (reefer, 3C) — see details below.</p>"
                      "<p>Bob Green<br/>Demo Toronto Produce</p>"),
    },
    {
        "email_from": "Sandra Chen <sandra@demodairy.test>",
        "to_addrs": ["dispatcher@logistics.premafirm.com"],
        "subject": "Load available: Brampton to Vaughan, this Thursday",
        "body_plain": (
            "Hello, one load available Thursday from our Brampton DC to "
            "Vaughan, 10 pallets dry van, standard hours. Reply if you can "
            "cover it.\nSandra Chen\nDemo United Dairy"),
    },
    {
        "email_from": "RMIS Alerts <alerts@rmis.test>",
        "to_addrs": ["dispatcher@logistics.premafirm.com"],
        "subject": "RMIS Contact Change Alert",
        "body_plain": ("Carrier Red Ball Express' phone number was changed "
                       "in the system."),
        "is_load_board": True,
    },
]

_RATE_CONFIRMATION_SUBJECT = "RE: Rate quote: 6 pallets reefer Toronto to Belleville"
_RATE_CONFIRMATION_BODY = (
    "Thanks — we accept your rate. Please book it. (Demo rate confirmation, "
    "UAT only.)\nBob Green\nDemo Toronto Produce")


class InboxFetchSim(models.Model):
    _name = "prema.inbox.fetch.sim"
    _description = "Dispatch Inbox simulated fetch (UAT only)"

    @api.model
    def _uat_gate(self, raise_if_denied=True):
        """Refuse to simulate unless this is unmistakably a UAT instance."""
        icp = self.env["ir.config_parameter"].sudo()
        uat_mode = icp.get_param("prema_inbox.uat_mode", "0")
        if uat_mode != "1":
            if raise_if_denied:
                raise ValidationError(
                    _("prema_inbox.uat_mode is not enabled — simulated "
                      "fetch refuses to run on this instance."))
            return False
        conf_path = config.get("config") or ""
        db_name = self.env.cr.dbname or ""
        looks_uat = ("inbox-uat" in conf_path or "inbox-uat" in db_name
                     or "inbox_uat" in db_name)
        if not looks_uat:
            if raise_if_denied:
                raise ValidationError(
                    _("This instance does not look like the inbox-uat "
                      "environment (config/db name) — refusing to simulate."))
            return False
        return True

    @api.model
    def simulate_fetch(self, scenario="demo", count=1):
        """Create synthetic incoming mail, return created records.

        Runs inside the caller's transaction (controller commits, then
        broadcasts post-commit — same as the real fetch path).
        """
        self._uat_gate()
        if scenario not in ("demo", "empty"):
            raise ValidationError(_("Unknown scenario: %s") % scenario)

        messages = []
        for i, vals in enumerate(_DEMO_SCENARIO):
            if i >= count:
                break
            msg, conv, created = self.env["prema.inbox.conversation"]._ingest_email(
                email_from=vals["email_from"],
                to_addrs=vals["to_addrs"],
                subject=vals["subject"],
                body_html=vals.get("body_html", ""),
                body_plain=vals.get("body_plain", ""),
                # per-call unique suffix: repeated fetches must create NEW
                # messages (badge 0→1→2→3), not dedupe into the old ones
                message_id="<sim-%s-%d-%s@prema-inbox-uat>" % (
                    self.env.cr.dbname, i, uuid.uuid4().hex[:8]),
                references="",
                in_reply_to="",
                is_load_board=vals.get("is_load_board", False),
            )
            messages.append({
                "message_id": msg.id,
                "conversation_id": conv.id,
                "subject": conv.name,
                "created": created,
            })
        if scenario == "empty":
            return {"messages": [], "scenario": "empty"}
        return {"messages": messages, "scenario": scenario}

    @api.model
    def simulate_reply(self, conversation_id):
        """Thread a customer reply back into an existing conversation —
        exercises the References/In-Reply-To thread-match path."""
        self._uat_gate()
        conv = self.env["prema.inbox.conversation"].browse(conversation_id)
        if not conv:
            raise ValidationError(_("No such conversation: %s") % conversation_id)
        last = conv.inbox_message_ids.sorted(key=lambda m: m.date, reverse=True)[:1]
        msg, conv2, created = self.env["prema.inbox.conversation"]._ingest_email(
            email_from="Bob Green <bob@demo-toronto-produce.test>",
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject=_RATE_CONFIRMATION_SUBJECT,
            body_plain=_RATE_CONFIRMATION_BODY,
            body_html="<p>%s</p>" % _RATE_CONFIRMATION_BODY.replace("\n", "<br/>"),
            message_id="<sim-reply-%s@prema-inbox-uat>" % uuid.uuid4().hex,
            references="<%s>" % last.message_id,
            in_reply_to="<%s>" % last.message_id,
        )
        return {
            "message_id": msg.id,
            "conversation_id": conv2.id,
            "threaded": conv2.id == conv.id,
            "subject": conv2.name,
            "created": created,
        }

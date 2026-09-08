# -*- coding: utf-8 -*-
"""Shared fixtures for inbox tests."""
import itertools

from odoo.tests import TransactionCase

DEMO_SENDER = "Bob Green <bob@demo-toronto-produce.test>"

# Unique per-call default Message-IDs — id(object()) can be reused by the
# interpreter (freed object → same address), which would make two ingests
# "the same message" and trigger the dedupe path in tests that intend
# distinct messages.
_msg_counter = itertools.count(1)


class InboxTestCase(TransactionCase):
    """Base: inbox group armed on the admin user AND the test env user,
    uat_mode armed so the fetch-sim gate passes (db name is inbox_uat on
    the clone)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # uid 1 on this clone is the DEACTIVATED superuser row (__system__;
        # prod's admin is id 2). Odoo refuses to reactivate it, and m2m
        # reads into res.users (read_user_ids, assignee_id, ...) filter
        # inactive rows — the write lands in the rel table but the ORM
        # read is blind. Act as the real admin user for the whole suite.
        cls.env = cls.env(user=cls.env.ref("base.user_admin").id)
        cls.admin = cls.env.ref("base.user_admin")
        cls.admin.write({"groups_id": [(4, cls.env.ref(
            "prema_dispatch_inbox.group_dispatch_inbox").id)]})
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.uat_mode", "1")
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.ai_mode", "mock")
        cls.Conversation = cls.env["prema.inbox.conversation"]
        cls.Message = cls.env["prema.inbox.message"]
        cls.Sim = cls.env["prema.inbox.fetch.sim"]

    def setUp(self):
        """TransactionCase shares ONE transaction across all test methods of
        a class — data created by an earlier test is still visible to later
        ones (and rules created earlier re-trigger on every
        _run_for_conversation). Clear the inbox tables per test so absolute
        count assertions (0, 2, 1, ...) hold regardless of test order."""
        super().setUp()
        self.env["prema.inbox.message"].search([]).unlink()
        self.env["prema.inbox.conversation"].search([]).unlink()
        self.env["prema.inbox.rule"].search([]).unlink()
        # mail.activity references the model by res_model+res_id (no FK) —
        # deleting the conversation orphans its activities, which would
        # trip absolute search_count assertions in later tests
        self.env["mail.activity"].search(
            [("res_model", "=", "prema.inbox.conversation")]).unlink()

    def make_user(self, login="ops.user", group=True):
        partner = self.env["res.partner"].create({
            "name": login,
            "email": "%s@premafirm.test" % login,
        })
        vals = {"login": login, "partner_id": partner.id}
        if group:
            vals["groups_id"] = [(4, self.env.ref(
                "prema_dispatch_inbox.group_dispatch_inbox").id)]
        return self.env["res.users"].create(vals)

    def ingest(self, email_from=DEMO_SENDER, subject="Rate quote: 6 pallets",
               body="Pickup Toronto M5V, delivery Belleville K8N, 6 pallets.",
               message_id=None,
               references="", is_load_board=False, body_html=None, **kw):
        message_id = message_id or "<test-%d@prema-inbox>" % next(
            _msg_counter)
        if body_html is None:
            body_html = "<p>%s</p>" % body
        return self.Conversation._ingest_email(
            email_from=email_from,
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject=subject, body_html=body_html,
            body_plain=body, message_id=message_id,
            references=references, is_load_board=is_load_board, **kw)

    def bus_rows(self, channel_like="prema_inbox"):
        """Raw SQL — bus_bus.channel is jsonb; ORM domain can't 'like' it.

        bus._sendone buffers in cr.precommit and the flush hook only runs at
        commit; in the test transaction we flush manually (same create the
        hook performs) so assertions see the rows.
        """
        vals = self.env.cr.precommit.data.pop("bus.bus.values", [])
        if vals:
            self.env["bus.bus"].sudo().create(vals)
        self.env.cr.execute(
            "SELECT count(*) FROM bus_bus WHERE channel::text ILIKE %s",
            ("%%%s%%" % channel_like,))
        return self.env.cr.fetchone()[0]

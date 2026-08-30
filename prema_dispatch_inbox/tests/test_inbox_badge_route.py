# -*- coding: utf-8 -*-
"""Regression: the systray badge must reconcile from the PUBLIC controller
route /prema_inbox/unread_counts — commit 31d3ff9 fixed the badge calling a
private ORM method over call_kw (Odoo 18 refuses private methods remotely,
so every reconcile raised AccessError).

These are HTTP-level tests (HttpCase boots an in-process HTTP server; the
requests share the test transaction, same pattern as
prema_logistics_booking/tests/test_phase20_security_personas.py).
"""
from odoo.tests import HttpCase, JsonRpcException


def _mk_user(env, login, password, with_group):
    partner = env["res.partner"].create({"name": login})
    groups = [(6, 0, [env.ref("base.group_user").id])]
    if with_group:
        groups[0][2].append(
            env.ref("prema_dispatch_inbox.group_dispatch_inbox").id)
    return env["res.users"].create({
        "name": login, "login": login, "password": password,
        "partner_id": partner.id,
        "groups_id": groups,
    })


# HttpCase needs a working HTTP stack; on this build post_install-tagged
# tests are skipped on -u runs, so these run at-install like the rest of
# the suite (the runner boots the in-process HTTP server on demand).
class TestInboxBadgeRoute(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.uat_mode", "1")
        self.member = _mk_user(self.env, "badge.member@test.local",
                               "BadgeTest1!", True)
        self.outsider = _mk_user(self.env, "badge.outsider@test.local",
                                 "BadgeTest2!", False)
        # The clone carries real walkthrough data (5 unread at the time of
        # writing) — normalize to a deterministic baseline. Must be done AS
        # the member: mark_read records self.env.user, and the test env is
        # uid 1 (deactivated __system__ on this clone).
        self.env["prema.inbox.message"].with_user(self.member).search(
            [("direction", "=", "incoming")]).mark_read()

    def _ingest(self, subject="Rate quote: 3 pallets", body="6 pallets dry"):
        msg, conv, created = self.env[
            "prema.inbox.conversation"]._ingest_email(
            email_from="Badge Tester <badge@test.local>",
            to_addrs=["dispatcher@logistics.premafirm.com"],
            subject=subject, body_html="", body_plain=body,
            message_id="<badge-route-7c1f@test.local>",
            references="", in_reply_to="")
        self.assertTrue(created)
        return msg, conv

    def test_member_gets_the_exact_badge_shape(self):
        """The route the badge JS reconciles from returns {user_id, counts}
        with the total/load_board/spam keys the widget reads."""
        self.authenticate("badge.member@test.local", "BadgeTest1!")
        res = self.make_jsonrpc_request("/prema_inbox/unread_counts", {})
        self.assertEqual(res["user_id"], self.member.id)
        self.assertIn("counts", res)
        for key in ("total", "load_board", "spam"):
            self.assertIn(key, res["counts"])
        self.assertEqual(res["counts"]["total"], 0)

    def test_route_tracks_ingest_and_mark_read(self):
        """Server truth wins: a new incoming message moves the count, and
        reading it drops it — the regression scenario for 31d3ff9 (before
        the fix, the badge called a private method and got AccessError)."""
        self.authenticate("badge.member@test.local", "BadgeTest1!")
        msg, conv = self._ingest()
        res = self.make_jsonrpc_request("/prema_inbox/unread_counts", {})
        self.assertEqual(res["counts"]["total"], 1)
        # reading is always the acting user's action (the UI calls mark_read
        # as the logged-in member — not as the test env's uid 1)
        msg.with_user(self.member).mark_read()
        res = self.make_jsonrpc_request("/prema_inbox/unread_counts", {})
        self.assertEqual(res["counts"]["total"], 0)

    def test_non_member_cannot_read_counts(self):
        """The route is gated on the inbox group: outsiders get a JSON-RPC
        error, never a count."""
        self.authenticate("badge.outsider@test.local", "BadgeTest2!")
        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request("/prema_inbox/unread_counts", {})

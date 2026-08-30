# -*- coding: utf-8 -*-
"""Security matrix (design §2 security + §13 — 403/empty for non-members)."""
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase


class TestInboxSecurity(TransactionCase):

    def _arm(self):
        admin = self.env.ref("base.user_admin")
        admin.write({"groups_id": [(4, self.env.ref(
            "prema_dispatch_inbox.group_dispatch_inbox").id)]})
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.uat_mode", "1")
        return admin

    def test_non_member_has_no_access_to_models(self):
        self._arm()
        outsider = self.env["res.users"].create({
            "login": "outsider",
            "partner_id": self.env["res.partner"].create(
                {"name": "Outsider", "email": "out@test.example"}).id,
        })
        self.assertRaises(AccessError, lambda: self.env[
            "prema.inbox.conversation"].with_user(outsider).search([]))
        self.assertRaises(AccessError, lambda: self.env[
            "prema.inbox.message"].with_user(outsider).search([]))
        self.assertRaises(AccessError, lambda: self.env[
            "prema.inbox.rule"].with_user(outsider).search([]))

    def test_member_full_crud(self):
        admin = self._arm()
        conv = self.env["prema.inbox.conversation"].with_user(admin).create({
            "name": "member-created",
            "partner_id": self.env["res.partner"].create(
                {"name": "P"}).id,
        })
        conv.write({"category": "invoice_question"})
        self.assertEqual(conv.category, "invoice_question")
        conv.unlink()
        self.assertFalse(self.env["prema.inbox.conversation"].search(
            [("name", "=", "member-created")]))

    def test_invoice_links_are_read_only_for_inbox_group(self):
        """ACL grants read on account.move — never create/write/unlink."""
        self._arm()
        group = self.env.ref("prema_dispatch_inbox.group_dispatch_inbox")
        acl = self.env["ir.model.access"].search([
            ("group_id", "=", group.id),
            ("model_id.model", "=", "account.move"),
        ], limit=1)
        self.assertTrue(acl)
        self.assertEqual(acl.perm_read, 1)
        self.assertEqual(acl.perm_write, 0)
        self.assertEqual(acl.perm_create, 0)
        self.assertEqual(acl.perm_unlink, 0)
        # same read-only contract for crm.lead
        acl_crm = self.env["ir.model.access"].search([
            ("group_id", "=", group.id),
            ("model_id.model", "=", "crm.lead"),
        ], limit=1)
        self.assertEqual(acl_crm.perm_read, 1)
        self.assertEqual(acl_crm.perm_write, 0)

    def test_detail_rpc_returns_empty_for_non_member(self):
        admin = self._arm()
        conv = self.env["prema.inbox.conversation"].with_user(admin).create({
            "name": "secret thread",
            "partner_id": self.env["res.partner"].create(
                {"name": "S"}).id,
        })
        outsider = self.env["res.users"].create({
            "login": "outsider2",
            "partner_id": self.env["res.partner"].create(
                {"name": "Outsider2", "email": "out2@test.example"}).id,
        })
        detail = self.env["prema.inbox.conversation"].with_user(
            outsider).inbox_conversation_detail(conv.id)
        self.assertEqual(detail, {})

    def test_sim_gate_refuses_without_uat_mode(self):
        admin = self._arm()
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_inbox.uat_mode", "0")
        with self.assertRaises(ValidationError):
            self.env["prema.inbox.fetch.sim"].with_user(
                admin).simulate_fetch(count=1)

    def test_sim_gate_accepts_with_uat_mode(self):
        admin = self._arm()
        sim = self.env["prema.inbox.fetch.sim"].with_user(
            admin).simulate_fetch(count=1)
        self.assertEqual(len(sim["messages"]), 1)

    def test_controller_guard_uses_same_group(self):
        """The badge/sim routes are gated on the same group the ACLs use
        (verified at the source, since the HTTP layer needs a full stack)."""
        self._arm()
        from odoo.addons.prema_dispatch_inbox.controllers import main
        src = main.PremaInboxController._require_inbox_user.__code__.co_consts
        self.assertTrue(
            any("prema_dispatch_inbox.group_dispatch_inbox" in str(c) for c in src))

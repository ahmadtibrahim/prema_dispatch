# -*- coding: utf-8 -*-
"""The warehouse scan endpoint, and who is allowed to use it.

The point of these tests is the *refusal*. A pallet's QR token is printed on a
label that falls off in yards and gets photographed by strangers. If holding
the token were enough, the token would be the authorisation — so these tests
pin down that an anonymous visitor and a signed-in internal user with no
warehouse role both get nothing, and only a warehouse role resolves a pallet.

The test users are created here, in a disposable test database, because the
permission model is what is under test. Nothing in this file runs against
production.
"""
import json

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestPflWarehouseHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.job = cls.env["prema.pfl.warehouse.job"].create({
            "partner_id": cls.env["res.partner"].create(
                {"name": "PFL HTTP Customer"}).id,
            "warehouse_id": cls.env["stock.warehouse"].search(
                [("code", "=", "WH")], limit=1).id,
            "service_mode": "ltl",
        })
        cls.job.action_pfl_confirm()
        cls.job.action_pfl_add_pallets(1)
        cls.package = cls.job.package_ids
        cls.job.action_pfl_receive()

        cls.warehouse_user = cls.env["res.users"].create({
            "name": "PFL Warehouse Tester",
            "login": "pfl_warehouse_tester",
            "password": "pfl-warehouse-tester",
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("prema_dispatch.group_dispatch_warehouse").id,
            ])],
        })
        cls.plain_user = cls.env["res.users"].create({
            "name": "PFL Plain Internal Tester",
            "login": "pfl_plain_tester",
            "password": "pfl-plain-tester",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })

    def _resolve(self, token):
        response = self.url_open(
            "/pfl/api/resolve",
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call",
                "params": {"code": token},
            }),
            headers={"Content-Type": "application/json"},
        )
        return response

    def test_an_anonymous_visitor_cannot_resolve_a_pallet(self):
        """The token is a locator, not a credential. No session is opened.

        Odoo answers a JSON-RPC error with HTTP 200, so the assertion is on the
        payload, not the status code: what matters is that no pallet data comes
        back, however unguessable the token is.
        """
        response = self._resolve(self.package.pfl_qr_token)
        payload = response.json()
        self.assertNotIn("result", payload)
        self.assertIn("error", payload)
        self.assertNotIn(self.package.name, response.text)
        self.assertNotIn(self.package.pfl_qr_token, response.text)

    def test_an_internal_user_without_a_warehouse_role_is_refused(self):
        self.authenticate("pfl_plain_tester", "pfl-plain-tester")
        response = self._resolve(self.package.pfl_qr_token)
        payload = response.json()
        self.assertIn("error", payload,
                      "Being signed in is not the same as being warehouse "
                      "staff.")
        self.assertEqual(payload["error"]["data"].get("name"),
                         "odoo.exceptions.AccessError")

    def test_a_warehouse_role_resolves_the_native_package(self):
        self.authenticate("pfl_warehouse_tester", "pfl-warehouse-tester")
        payload = self._resolve(self.package.pfl_qr_token).json()
        self.assertIn("result", payload)
        result = payload["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["package"]["id"], self.package.id)
        self.assertEqual(result["package"]["owner"],
                         self.package.pfl_owner_id.display_name)

    def test_a_warehouse_role_can_receive_through_the_scan_endpoint(self):
        package = self.job.action_pfl_add_pallets(1)
        self.authenticate("pfl_warehouse_tester", "pfl-warehouse-tester")
        response = self.url_open(
            "/pfl/api/action",
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call",
                "params": {
                    "package_ids": [package.id],
                    "action": "receive",
                },
            }),
            headers={"Content-Type": "application/json"},
        )
        payload = response.json()
        self.assertIn("result", payload, msg=response.text[:400])
        self.assertEqual(payload["result"]["status"], "ok")
        package.invalidate_recordset(["pfl_received_at"])
        self.assertTrue(package.pfl_received_at,
                        "The action must record the real receipt, not just "
                        "answer the phone.")

    def test_a_warehouse_role_can_attach_a_receipt_photo(self):
        """The photo the operator takes must actually be stored.

        The custody log is append-only and no role holds write on it, so the
        photograph is attached by the action itself. This drives that path as
        a *warehouse user* rather than as the superuser the model tests run
        as, because a real operator is where the write was being refused.
        """
        import base64
        package = self.job.action_pfl_add_pallets(1)
        photo = "data:image/jpeg;base64," + base64.b64encode(
            b"\xff\xd8\xff\xdb\xff\xd9").decode()
        self.authenticate("pfl_warehouse_tester", "pfl-warehouse-tester")
        response = self.url_open(
            "/pfl/api/action",
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call",
                "params": {
                    "package_ids": [package.id],
                    "action": "receive",
                    "photo": photo,
                    "photo_name": "receipt.jpg",
                },
            }),
            headers={"Content-Type": "application/json"},
        )
        payload = response.json()
        self.assertIn("result", payload, msg=response.text[:400])
        self.assertTrue(
            payload["result"]["photo_saved"],
            "The receipt photo must be saved, not silently dropped.")
        event = self.env["prema.pfl.custody.event"].search([
            ("package_id", "=", package.id),
            ("event_type", "=", "received")], limit=1)
        self.assertTrue(
            event.attachment_ids,
            "The photo must be linked to the receipt event — an unlinked "
            "attachment is a lost photograph.")

    def test_the_warehouse_page_requires_a_role(self):
        self.authenticate("pfl_plain_tester", "pfl-plain-tester")
        response = self.url_open("/pfl/warehouse", allow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 303, 307),
                      "A signed-in user with no warehouse role must be sent "
                      "away, not shown the operational screen.")
        self.assertIn("/web/login", response.headers.get("Location", ""))

    def test_the_warehouse_page_opens_for_a_warehouse_role(self):
        self.authenticate("pfl_warehouse_tester", "pfl-warehouse-tester")
        response = self.url_open("/pfl/warehouse")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Scan or type a pallet code", response.text)

# -*- coding: utf-8 -*-
"""The warehouse flow, end to end, against the real adopted configuration.

These tests deliberately run on the live catalogue and the live warehouse
locations (adopted by `hooks.py`) rather than on freshly created fixtures: the
thing most likely to be wrong is the *wiring* to what already exists, and
fixtures built inside the test would hide exactly that.

Time is always passed in explicitly. A storage charge that depends on "now"
cannot be tested, and a test that sleeps for thirty hours is not a test.
"""
import base64
import re
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPflWarehouse(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Package = cls.env["stock.quant.package"]
        cls.Job = cls.env["prema.pfl.warehouse.job"]
        cls.CustodyEvent = cls.env["prema.pfl.custody.event"]
        cls.customer = cls.env["res.partner"].create({
            "name": "PFL Test Billing Customer"})
        cls.owner = cls.env["res.partner"].create({
            "name": "PFL Test Freight Owner"})
        cls.ltl = cls.env.ref("prema_pfl_warehouse.pfl_product_pfl_xd_ltl")
        cls.ftl = cls.env.ref("prema_pfl_warehouse.pfl_product_pfl_xd_ftl")
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("code", "=", "WH")], limit=1)

    # ── helpers ────────────────────────────────────────────────────────
    def _job(self, pallets=4, mode="ltl", owner=None):
        job = self.Job.create({
            "partner_id": self.customer.id,
            "freight_owner_id": (owner or self.owner).id,
            "company_id": self.env.company.id,
            "warehouse_id": self.warehouse.id,
            "service_mode": mode,
            "expected_pallets": pallets,
        })
        job.action_pfl_confirm()
        job.action_pfl_add_pallets(pallets)
        self.assertEqual(len(job.package_ids), pallets)
        return job

    def _receive_release(self, job, t0, received_hours, released_hours=None,
                         packages=None):
        """Take pallets in at t0 and (optionally) release them after N hours."""
        job.action_pfl_receive(packages=packages, when=t0)
        if released_hours is not None:
            job.action_pfl_release(
                packages=packages,
                when=t0 + timedelta(hours=released_hours))

    # ── 1. four unlabeled pallets, six hours ───────────────────────────
    def test_six_hours_charges_handling_only(self):
        t0 = fields.Datetime.now() - timedelta(hours=10)
        job = self._job(4)
        self._receive_release(job, t0, 0, 6)
        job.action_pfl_review_charges()
        self.assertAlmostEqual(job.charge_total, 48.0, 2)
        self.assertFalse(
            job.charge_ids.filtered(lambda c: c.service == "storage"),
            "Six hours is inside the free first day, so nothing is stored for.")
        self.assertEqual(len(job.charge_ids), 4)

    # ── 2. four pallets, thirty hours ──────────────────────────────────
    def test_thirty_hours_adds_one_started_period_per_pallet(self):
        t0 = fields.Datetime.now() - timedelta(hours=40)
        job = self._job(4)
        self._receive_release(job, t0, 0, 30)
        job.action_pfl_review_charges()
        handling = sum(job.charge_ids.filtered(
            lambda c: c.service == "ltl").mapped("amount"))
        storage = sum(job.charge_ids.filtered(
            lambda c: c.service == "storage").mapped("amount"))
        self.assertAlmostEqual(handling, 48.0, 2)
        self.assertAlmostEqual(storage, 8.0, 2)
        self.assertAlmostEqual(job.charge_total, 56.0, 2)

    def test_a_started_period_is_charged_even_one_minute_over(self):
        t0 = fields.Datetime.now() - timedelta(hours=2)
        job = self._job(1)
        self._receive_release(job, t0, 0, 24.5)
        job.action_pfl_review_charges()
        storage = job.charge_ids.filtered(lambda c: c.service == "storage")
        self.assertEqual(len(storage), 1,
                         "24 h 30 m is into a second day, so one period bills.")

    # ── 3. partial release ─────────────────────────────────────────────
    def test_partial_release_only_stops_storage_for_departing_pallets(self):
        t0 = fields.Datetime.now() - timedelta(hours=60)
        job = self._job(4)
        staying = job.package_ids[3]
        leaving = job.package_ids - staying
        job.action_pfl_receive(when=t0)
        # Three leave at 30 h; one stays to 54 h.
        job.action_pfl_release(packages=leaving,
                               when=t0 + timedelta(hours=30))
        job.action_pfl_release(packages=staying,
                               when=t0 + timedelta(hours=54))
        job.action_pfl_review_charges()

        by_pallet = {}
        for charge in job.charge_ids.filtered(lambda c: c.service == "storage"):
            by_pallet[charge.package_id] = by_pallet.get(charge.package_id, 0) + 1
        self.assertEqual(by_pallet.get(staying), 2,
                         "The pallet left behind accrues a second period.")
        for package in leaving:
            self.assertEqual(by_pallet.get(package), 1,
                             "A released pallet stops accruing at its release.")
        self.assertAlmostEqual(job.charge_total, 48.0 + 10.0, 2)

    def test_internal_move_does_not_restart_the_storage_clock(self):
        t0 = fields.Datetime.now() - timedelta(hours=40)
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=t0)
        first_receipt = package.pfl_received_at
        job.action_pfl_move(packages=package, dest_barcode="PFL-WH-CD",
                            when=t0 + timedelta(hours=20))
        job.action_pfl_move(packages=package, dest_barcode="PFL-WH-DRY",
                            when=t0 + timedelta(hours=21))
        package.invalidate_recordset(["pfl_received_at"])
        self.assertEqual(package.pfl_received_at, first_receipt,
                         "Moving a pallet must not re-date its receipt.")
        job.action_pfl_release(packages=package, when=t0 + timedelta(hours=30))
        job.action_pfl_review_charges()
        storage = job.charge_ids.filtered(lambda c: c.service == "storage")
        self.assertEqual(len(storage), 1)

    def test_preparing_a_release_does_not_stop_storage(self):
        """Staged is not released — the pallet is still on our floor."""
        t0 = fields.Datetime.now() - timedelta(hours=40)
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=t0)
        job.action_pfl_prepare_release(
            packages=package, when=t0 + timedelta(hours=26))
        package.invalidate_recordset(["pfl_state", "pfl_staged_at"])
        self.assertEqual(package.pfl_state, "staged")
        self.assertFalse(package.pfl_released_at)
        job.action_pfl_review_charges()
        self.assertTrue(
            job.charge_ids.filtered(lambda c: c.service == "storage"),
            "A staged pallet still accrues storage until it actually leaves.")

    # ── 4. FTL base fee once ───────────────────────────────────────────
    def test_ftl_base_fee_is_charged_once_and_never_with_ltl(self):
        t0 = fields.Datetime.now() - timedelta(hours=40)
        job = self._job(4, mode="ftl")
        job.action_pfl_receive(when=t0)
        job.action_pfl_release(packages=job.package_ids[0],
                               when=t0 + timedelta(hours=30))
        job.action_pfl_review_charges()
        job.action_pfl_review_charges()  # twice: must not add a second fee
        ftl = job.charge_ids.filtered(lambda c: c.service == "ftl")
        self.assertEqual(len(ftl), 1)
        self.assertAlmostEqual(ftl.amount, 225.0, 2)
        self.assertFalse(
            job.charge_ids.filtered(lambda c: c.service == "ltl"),
            "An FTL job must never also pay per-pallet LTL handling.")
        # A later release of the remaining pallets adds no second base fee.
        job.action_pfl_release(packages=job.package_ids - job.package_ids[0],
                               when=t0 + timedelta(hours=31))
        job.action_pfl_review_charges()
        self.assertEqual(
            len(job.charge_ids.filtered(lambda c: c.service == "ftl")), 1)

    # ── 5. hold blocks release ─────────────────────────────────────────
    def test_held_pallet_cannot_be_released(self):
        t0 = fields.Datetime.now() - timedelta(hours=10)
        job = self._job(2)
        package = job.package_ids[0]
        job.action_pfl_receive(when=t0)
        job.action_pfl_hold(packages=package, reason="Damage under review")
        with self.assertRaises(UserError):
            job.action_pfl_release(packages=package)
        # The other pallet is unaffected.
        job.action_pfl_release(packages=job.package_ids[1],
                               when=t0 + timedelta(hours=6))
        package.invalidate_recordset(["pfl_on_hold"])
        self.assertTrue(package.pfl_on_hold)
        self.assertFalse(package.pfl_released_at)
        job.action_pfl_release_hold(packages=package, reason="Claim settled")
        job.action_pfl_release(packages=package,
                               when=t0 + timedelta(hours=7))
        package.invalidate_recordset(["pfl_on_hold", "pfl_released_at"])
        self.assertTrue(package.pfl_released_at)

    # ── 6. ownership ───────────────────────────────────────────────────
    def test_freight_owner_is_preserved_and_differs_from_the_billing_customer(self):
        t0 = fields.Datetime.now() - timedelta(hours=5)
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=t0)
        package.invalidate_recordset(["pfl_owner_id"])
        self.assertEqual(package.pfl_owner_id, self.owner)
        self.assertNotEqual(job.partner_id, job.freight_owner_id)
        quants = self.env["stock.quant"].search([
            ("package_id", "=", package.id)])
        self.assertTrue(quants)
        self.assertEqual(quants.mapped("owner_id"), self.owner,
                         "The quant, not just the job, must know whose freight "
                         "this is.")

    def test_billing_customer_is_never_assumed_to_own_the_freight(self):
        job = self._job(1)
        self.assertNotEqual(
            job.freight_owner_id, job.partner_id,
            "The job must keep the two parties apart rather than defaulting "
            "the owner to whoever pays.")

    # ── 6b. one job cannot touch another job's freight ─────────────────
    def test_a_job_cannot_move_or_release_another_jobs_pallet(self):
        """The ACL grants the dispatch roles every job; this is what stops one
        job acting on another's pallet."""
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job_a = self._job(1)
        job_b = self._job(1, owner=self.customer)
        job_a.action_pfl_receive(when=t0)
        job_b.action_pfl_receive(when=t0)
        with self.assertRaises(UserError):
            job_a._pfl_transfer(
                job_b.package_ids, dest=job_a._pfl_location("PFL-WH-DRY"),
                sequence_code="PFL-INT", event_type="moved")
        with self.assertRaises(UserError):
            job_a.action_pfl_release(packages=job_b.package_ids)
        job_b.package_ids.invalidate_recordset(["pfl_released_at"])
        self.assertFalse(job_b.package_ids.pfl_released_at,
                         "The refused release must not have happened anyway.")

    def test_a_job_cannot_use_a_foreign_operation_type_or_location(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        package = job.package_ids
        with self.assertRaises(UserError):
            job._pfl_transfer(
                package, dest=job._pfl_location("PFL-WH-DRY"),
                sequence_code="WH/IN", event_type="moved")
        with self.assertRaises(UserError):
            job._pfl_transfer(
                package, dest=self.env.ref("stock.stock_location_stock"),
                sequence_code="PFL-INT", event_type="moved")

    def test_a_photo_attaches_to_a_receipt_without_event_write_rights(self):
        """The event ACL grants no write to any role; the action must attach
        the operator's photo without depending on one."""
        t0 = fields.Datetime.now() - timedelta(hours=2)
        job = self._job(1)
        package = job.package_ids
        attachment = self.env["ir.attachment"].create({
            "name": "receipt.jpg", "datas": "aGVsbG8=",
            "res_model": "stock.quant.package", "res_id": package.id})
        job.action_pfl_receive(when=t0, attachment_ids=[attachment.id])
        event = self.CustodyEvent.search([
            ("package_id", "=", package.id),
            ("event_type", "=", "received")], limit=1)
        self.assertEqual(event.attachment_ids, attachment)

    # ── 7. repeated actions ────────────────────────────────────────────
    def test_receiving_twice_creates_one_movement_and_one_charge(self):
        t0 = fields.Datetime.now() - timedelta(hours=5)
        job = self._job(2)
        picking = job.action_pfl_receive(when=t0)
        again = job.action_pfl_receive(when=t0)
        self.assertTrue(picking)
        self.assertFalse(again,
                         "Already-received pallets produce no second transfer.")
        self.assertEqual(len(job.event_ids.filtered(
            lambda e: e.event_type == "received")), 2)
        job.action_pfl_review_charges()
        job.action_pfl_review_charges()
        self.assertEqual(len(job.charge_ids), 2)
        self.assertEqual(len(set(job.charge_ids.mapped("idempotency_key"))), 2)

    def test_releasing_twice_creates_one_movement(self):
        t0 = fields.Datetime.now() - timedelta(hours=5)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        job.action_pfl_release(packages=job.package_ids, when=t0)
        again = job.action_pfl_release(packages=job.package_ids, when=t0)
        self.assertFalse(again)
        job.action_pfl_review_charges()
        job.action_pfl_review_charges()
        self.assertEqual(len(job.charge_ids.filtered(
            lambda c: c.service == "ltl")), 1)

    # ── 8. rate freeze ─────────────────────────────────────────────────
    def test_a_later_price_change_does_not_touch_an_accepted_job(self):
        job = self._job(2)
        rate = job.rate_ids.filtered(lambda r: r.service == "ltl")
        self.assertAlmostEqual(rate.unit_rate, 12.0, 2)
        self.assertEqual(rate.source, "catalogue")
        self.ltl.list_price = 999.0
        try:
            t0 = fields.Datetime.now() - timedelta(hours=5)
            job.action_pfl_receive(when=t0)
            job.action_pfl_release(packages=job.package_ids, when=t0)
            job.action_pfl_review_charges()
            self.assertAlmostEqual(job.charge_total, 24.0, 2,
                                   msg="The accepted rate must survive a "
                                       "catalogue change.")
            rate.invalidate_recordset(["unit_rate"])
            self.assertAlmostEqual(rate.unit_rate, 12.0, 2)
        finally:
            self.ltl.list_price = 12.0

    def test_rates_come_from_the_accepted_sale_order_when_there_is_one(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [(0, 0, {
                "product_id": self.ltl.product_variant_id.id,
                "product_uom_qty": 4,
                "price_unit": 9.5,
            })],
        })
        order.action_confirm()
        job = self.env["prema.pfl.warehouse.job"].create({
            "partner_id": self.customer.id,
            "freight_owner_id": self.customer.id,
            "sale_order_id": order.id,
            "warehouse_id": self.warehouse.id,
            "service_mode": "ltl",
        })
        job.action_pfl_confirm()
        rate = job.rate_ids.filtered(lambda r: r.service == "ltl")
        self.assertEqual(rate.source, "sale_order")
        self.assertAlmostEqual(rate.unit_rate, 9.5, 2)

    # ── 9. scanning, labels, photos ────────────────────────────────────
    def test_scanning_resolves_the_native_package_by_opaque_token(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=t0)
        self.assertTrue(package.pfl_qr_token)
        self.assertNotIn(str(package.id), package.pfl_qr_token)
        found = self.Package._pfl_resolve_scan(package.pfl_qr_token)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["package"], package)

    def test_customer_barcode_alias_resolves_and_never_merges_two_pallets(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(2)
        first, second = job.package_ids[0], job.package_ids[1]
        job.action_pfl_receive(when=t0)
        self.env["prema.pfl.barcode.alias"].create({
            "barcode": "SHIPPER-0001",
            "customer_id": self.customer.id,
            "package_id": first.id,
        })
        found = self.Package._pfl_resolve_scan("SHIPPER-0001", self.customer)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["package"], first)
        # The same code on a different pallet is refused, not merged. The
        # savepoint keeps the unique-constraint failure from poisoning the
        # surrounding transaction.
        with self.assertRaises(Exception), self.env.cr.savepoint():
            self.env["prema.pfl.barcode.alias"].create({
                "barcode": "SHIPPER-0001",
                "customer_id": self.customer.id,
                "package_id": second.id,
            })
        self.assertEqual(len(job.package_ids), 2)
        self.assertEqual(second.pfl_state, "received")

    def test_the_same_barcode_from_two_customers_is_ambiguous_not_guessed(self):
        other = self.env["res.partner"].create({"name": "PFL Other Customer"})
        job = self._job(1)
        package = job.package_ids
        job2 = self.Job.create({
            "partner_id": other.id,
            "freight_owner_id": other.id,
            "warehouse_id": self.warehouse.id,
            "service_mode": "ltl",
        })
        job2.action_pfl_confirm()
        job2.action_pfl_add_pallets(1)
        self.env["prema.pfl.barcode.alias"].create({
            "barcode": "SHARED-9", "customer_id": self.customer.id,
            "package_id": package.id})
        self.env["prema.pfl.barcode.alias"].create({
            "barcode": "SHARED-9", "customer_id": other.id,
            "package_id": job2.package_ids.id})
        result = self.Package._pfl_resolve_scan("SHARED-9")
        self.assertEqual(result["status"], "ambiguous",
                         "A code shared by two customers must not be guessed.")

    def test_photo_is_linked_to_the_receipt_event(self):
        t0 = fields.Datetime.now() - timedelta(hours=2)
        job = self._job(1)
        package = job.package_ids
        attachment = self.env["ir.attachment"].create({
            "name": "receipt.jpg",
            "datas": "aGVsbG8=",
            "res_model": "stock.quant.package",
            "res_id": package.id,
        })
        job.action_pfl_receive(packages=package, when=t0,
                               attachment_ids=[attachment.id])
        event = job.event_ids.filtered(lambda e: e.event_type == "received")
        self.assertEqual(event.attachment_ids, attachment,
                         "The photograph belongs to the moment it documents.")

    def test_label_report_carries_the_token_and_no_customer_detail(self):
        job = self._job(2)
        first, second = job.package_ids[0], job.package_ids[1]
        report = self.env["ir.actions.report"]
        html = report._render_qweb_html(
            "prema_pfl_warehouse.report_pfl_pallet_label_document",
            first.ids)[0].decode()
        # The QR is embedded inline. Rendering it through `/report/barcode`
        # would depend on a renderPM backend this environment does not ship,
        # and a label whose image 500s still prints — looking finished and
        # carrying no scannable code.
        match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html)
        self.assertTrue(
            match, "The label must embed the QR inline, not reference it.")
        self.assertTrue(
            base64.b64decode(match.group(1)).startswith(b"\x89PNG\r\n\x1a\n"),
            "The embedded QR must be a real PNG.")
        other = report._render_qweb_html(
            "prema_pfl_warehouse.report_pfl_pallet_label_document",
            second.ids)[0].decode()
        self.assertNotEqual(
            re.search(r"base64,([A-Za-z0-9+/=]+)", other).group(1),
            match.group(1),
            "Each pallet's label must encode that pallet's own token.")
        # The token lives inside the code, and the sheet says nothing about
        # who the freight belongs to.
        self.assertNotIn(first.pfl_qr_token, html)
        self.assertNotIn(self.customer.name, html)
        self.assertNotIn(self.owner.name, html)

    # ── 10. sales order, delivered quantities and the draft invoice ────
    def test_sale_order_delivered_quantities_and_invoice_agree(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [(0, 0, {
                "product_id": self.ltl.product_variant_id.id,
                "product_uom_qty": 4,
                "price_unit": 12.0,
            })],
        })
        order.action_confirm()
        action = order.action_pfl_create_job()
        job = self.Job.browse(action["res_id"])
        self.assertEqual(job.sale_order_id, order)

        t0 = fields.Datetime.now() - timedelta(hours=40)
        job.action_pfl_add_pallets(4)
        job.action_pfl_receive(when=t0)
        job.action_pfl_release(when=t0 + timedelta(hours=30))
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()

        invoice = job.invoice_ids
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, "draft",
                         "A warehouse invoice is prepared, never posted.")
        self.assertAlmostEqual(invoice.amount_untaxed, 56.0, 2)
        self.assertEqual(invoice.invoice_origin, job.name)

        line = order.order_line
        self.assertAlmostEqual(line.qty_delivered, 4.0, 2,
                               msg="Delivered quantity must come from the "
                                   "warehouse actuals.")
        self.assertTrue(invoice.invoice_line_ids.sale_line_ids,
                        "The invoice line keeps the native sale-order link.")

    def test_invoice_is_extended_not_duplicated_when_charges_are_added(self):
        # Inside the free first day, so the only variable is the pallet count.
        t0 = fields.Datetime.now() - timedelta(hours=10)
        job = self._job(2)
        job.action_pfl_receive(packages=job.package_ids[0], when=t0)
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()
        self.assertEqual(len(job.invoice_ids), 1)
        self.assertAlmostEqual(job.invoice_ids.amount_untaxed, 12.0, 2)
        # The second pallet is received later; its charge joins the same draft.
        job.action_pfl_receive(packages=job.package_ids[1],
                               when=t0 + timedelta(hours=1))
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()
        self.assertEqual(len(job.invoice_ids), 1,
                         "An open draft is extended, not duplicated.")
        job.invoice_ids.invalidate_recordset(["amount_untaxed"])
        self.assertAlmostEqual(job.invoice_ids.amount_untaxed, 24.0, 2)

    def test_a_posted_invoice_is_never_modified_and_later_work_bills_separately(self):
        t0 = fields.Datetime.now() - timedelta(hours=10)
        job = self._job(2)
        first, second = job.package_ids[0], job.package_ids[1]
        job.action_pfl_receive(packages=first, when=t0)
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()
        original = job.invoice_ids
        original.action_post()
        self.assertEqual(original.state, "posted")
        posted_amount = original.amount_untaxed
        self.assertAlmostEqual(posted_amount, 12.0, 2)

        # The second pallet arrives after the first invoice was posted.
        job.action_pfl_receive(packages=second, when=t0 + timedelta(hours=2))
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()
        self.assertEqual(len(job.invoice_ids), 2,
                         "A posted invoice is closed; later work gets its own "
                         "draft.")
        original.invalidate_recordset(["amount_untaxed", "state"])
        self.assertAlmostEqual(original.amount_untaxed, posted_amount, 2,
                               msg="The posted invoice must not have moved.")
        self.assertEqual(original.state, "posted")
        new_draft = job.invoice_ids - original
        self.assertEqual(new_draft.state, "draft")
        self.assertAlmostEqual(new_draft.amount_untaxed, 12.0, 2,
                               "Only the newly earned charge is billed again.")
        # Each charge is billed exactly once, on exactly one invoice.
        invoiced = job.charge_ids.filtered("invoiced")
        self.assertEqual(len(invoiced), 2)
        self.assertEqual(len(invoiced.invoice_id), 2)
        self.assertEqual(invoiced.invoice_id, original | new_draft)

    def test_nothing_to_invoice_is_refused_rather_than_invented(self):
        job = self._job(1)
        with self.assertRaises(UserError):
            job.action_pfl_create_invoice()

    # ── the log itself ─────────────────────────────────────────────────
    def test_custody_events_are_append_only(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        event = job.event_ids.filtered(lambda e: e.event_type == "received")
        with self.assertRaises(UserError):
            event.write({"occurred_at": fields.Datetime.now()})
        with self.assertRaises(UserError):
            event.unlink()
        # Adding a photograph to an event that already happened is allowed.
        event.write({"attachment_ids": [(0, 0, {"name": "x.jpg",
                                                "datas": "aGk="})]})

    def test_a_correction_keeps_the_original_and_the_reason(self):
        t0 = fields.Datetime.now() - timedelta(hours=10)
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=t0)
        original = job.event_ids.filtered(lambda e: e.event_type == "received")
        job.action_pfl_correct_moment(
            package, "received",
            fields.Datetime.now() - timedelta(hours=11),
            "Gate log shows the truck arrived an hour earlier.")
        events = self.CustodyEvent.search([
            ("package_id", "=", package.id), ("event_type", "=", "received")])
        self.assertEqual(len(events), 2,
                         "A correction adds an event; it does not replace one.")
        self.assertTrue(original.exists())
        correction = events - original
        self.assertEqual(correction.corrects_id, original)
        self.assertTrue(correction.reason)
        original.invalidate_recordset(["is_corrected"])
        self.assertTrue(original.is_corrected)
        package.invalidate_recordset(["pfl_received_at"])
        self.assertEqual(package.pfl_received_at, correction.occurred_at,
                         "The cache follows the newest entry for that moment.")
        self.assertNotEqual(package.pfl_received_at, original.occurred_at)

    def test_a_correction_without_a_reason_is_refused(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        with self.assertRaises(UserError):
            job.action_pfl_correct_moment(
                job.package_ids, "received", fields.Datetime.now(), False)

    # ── off-pallet cards ───────────────────────────────────────────────
    def test_a_position_can_only_identify_one_pallet(self):
        job = self._job(2)
        position = self.env.ref("prema_pfl_warehouse.pfl_location_pfl_wh_cd_01")
        first, second = job.package_ids[0], job.package_ids[1]
        self.env["prema.pfl.card"].create({
            "package_id": first.id, "position_location_id": position.id})
        with self.assertRaises(UserError):
            self.env["prema.pfl.card"].create({
                "package_id": second.id, "position_location_id": position.id})

    def test_a_no_label_pallet_still_carries_its_identification(self):
        job = self.Job.create({
            "partner_id": self.customer.id,
            "freight_owner_id": self.owner.id,
            "warehouse_id": self.warehouse.id,
            "service_mode": "ltl",
            "label_restriction": "no_attached_label",
        })
        job.action_pfl_confirm()
        job.action_pfl_add_pallets(1)
        package = job.package_ids
        self.assertEqual(package.pfl_label_mode, "no_attached_label")
        card = self.env["prema.pfl.card"].create({
            "package_id": package.id,
            "position_location_id": self.env.ref(
                "prema_pfl_warehouse.pfl_location_pfl_wh_cd_02").id,
        })
        package.write({"pfl_card_id": card.id})
        t0 = fields.Datetime.now() - timedelta(hours=2)
        job.action_pfl_receive(when=t0)
        job.action_pfl_move(packages=package, dest_barcode="PFL-WH-CD", when=t0)
        self.assertEqual(
            len(job.event_ids.filtered(lambda e: e.event_type == "moved")), 1,
            "A pallet that cannot carry a label still has every movement "
            "recorded.")
        self.assertTrue(package.pfl_qr_token)

    # ── labelling cannot be given away ─────────────────────────────────
    def test_labelling_at_the_zero_placeholder_is_refused(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        rate = job.rate_ids.filtered(lambda r: r.service == "labeling")
        self.assertAlmostEqual(rate.unit_rate, 0.0, 2)
        job.action_pfl_receive(when=t0)
        job.labeling_pallets = 1
        with self.assertRaises(UserError):
            job.action_pfl_review_charges()

    def test_labelling_bills_once_a_rate_is_agreed(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        rate = job.rate_ids.filtered(lambda r: r.service == "labeling")
        rate.write({"unit_rate": 1.5, "source": "manual"})
        job.action_pfl_receive(when=t0)
        job.labeling_pallets = 2
        job.action_pfl_review_charges()
        labelling = job.charge_ids.filtered(lambda c: c.service == "labeling")
        self.assertEqual(len(labelling), 1)
        self.assertAlmostEqual(labelling.amount, 3.0, 2)

    def test_a_service_explicitly_agreed_free_is_not_billed(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        rate = job.rate_ids.filtered(lambda r: r.service == "labeling")
        rate.write({"free_service": True})
        job.action_pfl_receive(when=t0)
        job.labeling_pallets = 2
        job.action_pfl_review_charges()
        labelling = job.charge_ids.filtered(lambda c: c.service == "labeling")
        self.assertEqual(labelling.amount, 0.0)

    def test_an_agreed_free_service_closes_without_a_zero_invoice_line(self):
        """Nothing to bill must not put a $0 line on the customer's invoice."""
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        rate = job.rate_ids.filtered(lambda r: r.service == "labeling")
        rate.write({"free_service": True})
        job.action_pfl_receive(when=t0)
        job.labeling_pallets = 1
        job.action_pfl_review_charges()
        job.action_pfl_create_invoice()

        invoice = job.invoice_ids
        self.assertEqual(len(invoice.invoice_line_ids), 1,
                         "Only the priced handling line belongs on it.")
        self.assertTrue(all(invoice.invoice_line_ids.mapped("price_subtotal")))
        free = job.charge_ids.filtered(lambda c: c.service == "labeling")
        self.assertTrue(free.invoiced,
                        "The free service is closed so it stops appearing as "
                        "work still to bill.")
        self.assertFalse(free.invoice_line_id)
        self.assertFalse(job.uninvoiced_charge_count)

    # ── extras need approval ───────────────────────────────────────────
    def test_an_unapproved_extra_is_never_billed(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        extra = self.env["prema.pfl.extra"].create({
            "job_id": job.id,
            "description": "Hand-bombing onto the mezzanine",
            "quantity": 2,
            "unit_rate": 50.0,
        })
        job.action_pfl_review_charges()
        self.assertFalse(job.charge_ids.filtered(lambda c: c.service == "extra"))
        extra.action_approve()
        job.action_pfl_review_charges()
        billed = job.charge_ids.filtered(lambda c: c.service == "extra")
        self.assertEqual(len(billed), 1)
        self.assertAlmostEqual(billed.amount, 100.0, 2)

    # ── labour minimum ─────────────────────────────────────────────────
    def test_labour_minimum_is_one_hour_per_person(self):
        t0 = fields.Datetime.now() - timedelta(hours=3)
        job = self._job(1)
        job.action_pfl_receive(when=t0)
        self.env["prema.pfl.labour"].create({
            "job_id": job.id, "persons": 2, "minutes": 15,
            "occurred_at": t0,
        })
        job.action_pfl_review_charges()
        labour = job.charge_ids.filtered(lambda c: c.service == "labour")
        self.assertAlmostEqual(labour.quantity, 2.0, 2,
                               msg="Two people for a quarter hour is two "
                                   "person-hours at the one-hour minimum.")
        self.assertAlmostEqual(labour.amount, 80.0, 2)

    # ── the job state follows the pallets ──────────────────────────────
    def test_job_state_follows_the_pallets(self):
        t0 = fields.Datetime.now() - timedelta(hours=5)
        job = self._job(2)
        self.assertEqual(job.state, "confirmed")
        job.action_pfl_receive(packages=job.package_ids[0], when=t0)
        self.assertEqual(job.state, "storing")
        job.action_pfl_prepare_release(packages=job.package_ids[0], when=t0)
        self.assertEqual(job.state, "prepared")
        job.action_pfl_receive(packages=job.package_ids[1], when=t0)
        job.action_pfl_release(when=t0)
        self.assertEqual(job.state, "released")

    def test_a_rate_cannot_be_changed_once_the_job_is_confirmed(self):
        job = self._job(1)
        package = job.package_ids
        job.action_pfl_receive(when=fields.Datetime.now())
        package.invalidate_recordset(["pfl_received_at"])
        self.assertTrue(package.pfl_received_at)
        self.assertIsNotNone(job.rates_frozen_at)

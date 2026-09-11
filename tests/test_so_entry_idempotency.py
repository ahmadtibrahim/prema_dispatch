"""D-C1 (master §1/§14): Sale Order entry is canonical and idempotent.

Both SO flows (Book Load button wizard, Generate from Text) must confirm
exactly ONE logistics.booking through BookingOrchestrationService with the
"sale_order" channel; the booking's dispatch-job bridge creates the
Planner card(s) with logistics_booking_id + sale_order_id back-links.
No flow in models/sale_order_dispatch.py may create prema.dispatch.job
directly, and the orphan guard in dispatch_job.create() blocks the retired
direct source_model "sale.order" create.

Coverage (synthetic fixtures, no external calls):
(a) Book Load twice → exactly one booking + one set of jobs; second click
    opens the existing booking.
(b) Every job created by an SO flow carries its logistics booking.
(c) Legacy (pre-booking) SO jobs stay openable; nothing new is stacked on
    them; identical AI text reuses the legacy job.
(d) Direct "sale.order" job create raises; the documented legacy context
    escape works; planner-style unlinked creates still pass (audit-only).
"""

from datetime import datetime

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _fake_analyze(record, text, extra=""):
    """Deterministic stand-in for the AI analyzer (no external calls)."""
    return {
        "reference": "TEST-REF-1",
        "stops": [
            {"type": "pickup", "address": "1406 Test Line 8, Ayr, ON N0B 1E0"},
            {"type": "dropoff", "address": "2277 Test Sideroad 15, Ayr, ON N0B 1E0",
             "pallets": 2},
        ],
        "approximate_skids": 2,
        "commodity": "widgets",
        "scheduled_date": "2026-09-10",
        "requires_reefer": False,
        "requires_liftgate": False,
        "temp_requirement": "",
    }


class JobCreateRecorder:
    """Records every prema.dispatch.job created while active — proves the
    SO flows never create jobs outside the booking bridge."""

    def __init__(self, env):
        self.env = env
        self.created = []
        model_cls = type(env["prema.dispatch.job"])
        self._model_cls = model_cls
        self._orig = model_cls.create

    def __enter__(self):
        def _recorded_create(recordset, vals_list):
            records = self._orig(recordset, vals_list)
            self.created.extend(records)
            return records

        self._model_cls.create = _recorded_create
        return self

    def __exit__(self, exc_type, exc, tb):
        self._model_cls.create = self._orig


@tagged("post_install", "-at_install")
class TestSoEntryIdempotency(TransactionCase):
    """post_install, not at_install, and it has to be.

    Every test here books a load, and booking goes through
    `logistics.booking`, which lives in prema_logistics_booking — a module
    that DEPENDS ON THIS ONE, so it is always loaded after it. An
    at_install test runs inside this module's own step of the load graph
    (`odoo/modules/loading.py`), i.e. before that model exists, so
    `_logistics_loaded()` answers False and all eight booking tests error
    out on the guard rather than exercising anything. They had been
    passing as "0 failed" only because errors are not failures.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "SO Entry Test Customer"})

    # ── Fixtures ─────────────────────────────────────────────────────────

    def _location(self, name, address, postal, lat=43.63, lng=-79.46):
        return self.env["prema.dispatch.location"].create({
            "name": name,
            "business_name": name,
            "address": address,
            "street": address,
            "city": "Ayr",
            "province_code": "ON",
            "postal_code": postal,
            "pin_lat": lat,
            "pin_lng": lng,
            "google_verified": True,
            "google_place_id": "ChIJ-TEST-%s" % name.replace(" ", ""),
            "verification_state": "verified",
        })

    def _make_priced_so(self, partner=None, amount=1500.0):
        """Sale Order with order lines so amount_untaxed is real — the
        agreed-rate basis for the Custom / Expedited and text flows."""
        product = self.env["product.product"].create({
            "name": "SO Entry Test Product",
            "type": "consu",
        })
        so = self.env["sale.order"].create({
            "partner_id": (partner or self.partner).id,
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": "Freight: %s" % product.name,
                "product_uom_qty": 2,
                "price_unit": amount / 2.0,
            })],
        })
        self.assertTrue(so.amount_untaxed)
        return so

    def _so_book_wizard(self, so, pickup, delivery, mode="custom",
                        service_type="ltl", skids=4):
        wizard = self.env["prema.dispatch.so.book.wizard"].with_context(
            active_id=so.id).create({
                "sale_order_id": so.id,
                "partner_id": so.partner_id.id,
                "booking_mode": mode,
                "service_type": service_type,
                "equipment_type": "dry",
                "expected_skids": skids,
                "total_weight_lbs": 1000.0,
                "scheduled_pickup": datetime(2026, 9, 20, 8, 0),
                "pickup_saved_location_id": pickup.id,
                "delivery_saved_location_id": delivery.id,
            })
        return wizard

    def _bookings_for(self, so):
        return self.env["logistics.booking"].sudo().search(
            [("sale_order_id", "=", so.id)])

    def _open_target(self, action):
        """Resolve the record the action opens (notification 'next' too)."""
        if action.get("tag") == "display_notification":
            return action["params"]["next"]
        return action

    def _patch_ai(self):
        from odoo.addons.premafirm_ai_engine.services import invoice_ai_service
        self._ai_service = invoice_ai_service
        self._orig_analyze = invoice_ai_service.InvoiceAIService.analyze_from_text
        invoice_ai_service.InvoiceAIService.analyze_from_text = staticmethod(
            _fake_analyze)

    def _unpatch_ai(self):
        self._ai_service.InvoiceAIService.analyze_from_text = self._orig_analyze

    # ── (a) Book Load twice → one booking, one job set ───────────────────

    def test_book_load_confirm_twice_creates_one_booking_and_reuses_it(self):
        so = self._make_priced_so()
        pickup = self._location("SO Entry Pickup", "1406 Test Line 8", "N0B 1E0")
        delivery = self._location("SO Entry Delivery", "2277 Test Sideroad 15", "N0B 1E0")

        with JobCreateRecorder(self.env) as recorder:
            first = self._so_book_wizard(so, pickup, delivery).action_confirm()
            booking = self.env["logistics.booking"].browse(first["res_id"])
            self.assertTrue(booking)
            self.assertEqual(booking.source_channel, "sale_order")
            self.assertEqual(booking.sale_order_id, so)
            self.assertEqual(
                booking.idempotency_key, f"sale.order:{so.id}:custom")

            # Second Book Load click — a fresh wizard on the same SO must
            # open the SAME booking (wizard pre-check + orchestration key).
            second = self._so_book_wizard(so, pickup, delivery).action_confirm()
            self.assertEqual(second["res_model"], "logistics.booking")
            self.assertEqual(second["res_id"], booking.id)

        self.assertEqual(len(self._bookings_for(so)), 1)
        self.assertEqual(len(so.dispatch_job_ids), 1)
        job = so.dispatch_job_ids
        self.assertEqual(job.logistics_booking_id, booking)
        self.assertEqual(job.sale_order_id, so)
        # The Book Load button itself now opens the booking, never the wizard.
        button = so.action_book_load()
        self.assertEqual(button["res_model"], "logistics.booking")
        self.assertEqual(button["res_id"], booking.id)

        # (b) — every job created on the way carried its booking back-link.
        self.assertEqual(len(recorder.created), 1)
        for job in recorder.created:
            self.assertTrue(job.logistics_booking_id,
                            "every SO-flow job must come from the booking bridge")
            self.assertEqual(job.sale_order_id, so)

    def test_book_load_opens_booking_created_by_ai_flow(self):
        """The Book Load button keeps the one-booking-per-SO semantic: a
        booking that already exists (here via the text flow) is opened."""
        so = self._make_priced_so()
        so.x_so_text_input = (
            "Ship A: 1406 Test Line 8, Ayr, ON N0B 1E0 to 2277 Test Sideroad "
            "15, Ayr, ON N0B 1E0; 2 skids of widgets."
        )
        self._patch_ai()
        try:
            so.action_generate_dispatch_from_text()
        finally:
            self._unpatch_ai()
        self.assertEqual(len(self._bookings_for(so)), 1)

        # Button on an SO that already has its booking → opens it.
        button = so.action_book_load()
        self.assertEqual(button["res_model"], "logistics.booking")

    # ── (b) AI text flow: canonical booking, per-text idempotency ────────

    def test_generate_same_text_confirms_one_booking_with_job_backlinks(self):
        so = self._make_priced_so()
        so.x_so_text_input = (
            "Pickup 1406 Test Line 8, Ayr, ON N0B 1E0; deliver 2277 Test "
            "Sideroad 15, Ayr, ON N0B 1E0; 2 skids of widgets."
        )
        self._patch_ai()
        try:
            with JobCreateRecorder(self.env) as recorder:
                first = self._open_target(
                    so.action_generate_dispatch_from_text())
                booking = self.env["logistics.booking"].browse(first["res_id"])
                self.assertTrue(booking)
                self.assertEqual(booking.source_channel, "sale_order")
                self.assertEqual(
                    booking.idempotency_key,
                    f"sale.order:{so.id}:text:"
                    f"{so._so_text_fingerprint(so.x_so_text_input)}")
                # Canonical booking stops, not direct dispatch stops.
                self.assertEqual(len(booking.stop_ids), 2)
                self.assertEqual(
                    booking.stop_ids.filtered(
                        lambda s: s.stop_type == "delivery").pallet_count, 2)

                second = self._open_target(
                    so.action_generate_dispatch_from_text())
                self.assertEqual(second["res_id"], booking.id)
        finally:
            self._unpatch_ai()

        self.assertEqual(len(self._bookings_for(so)), 1)
        self.assertEqual(len(so.dispatch_job_ids), 1)
        job = so.dispatch_job_ids
        self.assertTrue(job.logistics_booking_id)
        self.assertEqual(job.sale_order_id, so)
        self.assertEqual(len(job.stop_ids), 2)

    def test_generate_different_text_allows_a_second_booking(self):
        """A different shipment on the same SO is still legitimate — same
        behavior as the pre-canonical flow (distinct fingerprint = distinct
        idempotency key = its own booking + its own job set)."""
        so = self._make_priced_so()
        self._patch_ai()
        try:
            so.x_so_text_input = "Ship A: Ayr to Ayr, 2 skids of widgets."
            so.action_generate_dispatch_from_text()
            so.x_so_text_input = (
                "Ship B on the same order: 1406 Test Line 8, Ayr, ON N0B 1E0 "
                "to 2277 Test Sideroad 15, Ayr, ON N0B 1E0, 4 skids, Friday."
            )
            so.action_generate_dispatch_from_text()
        finally:
            self._unpatch_ai()

        self.assertEqual(len(self._bookings_for(so)), 2)
        self.assertEqual(len(so.dispatch_job_ids), 2)
        for job in so.dispatch_job_ids:
            self.assertTrue(job.logistics_booking_id)

    # ── (c) Legacy pre-booking rows are preserved, never re-booked ───────

    def test_book_load_on_legacy_jobs_opens_them_and_creates_nothing(self):
        so = self._make_so_for_legacy()
        action = so.action_book_load()
        self.assertEqual(action["tag"], "display_notification")
        target = action["params"]["next"]
        self.assertEqual(target["res_model"], "prema.dispatch.job")
        self.assertEqual(len(self._bookings_for(so)), 0,
                         "legacy jobs must never be re-booked under a booking")

    def test_generate_text_reuses_legacy_fingerprint_job(self):
        so = self._make_so_for_legacy()
        so.x_so_text_input = (
            "Pickup 1406 Test Line 8, Ayr, ON N0B 1E0; deliver 2277 Test "
            "Sideroad 15, Ayr, ON N0B 1E0; 2 skids of widgets."
        )
        # Same text already produced a pre-D-C1 job (fp in its notes).
        fp = so._so_text_fingerprint(so.x_so_text_input)
        self.env["prema.dispatch.job"].with_context(
            dispatch_job_legacy_create=True).create({
                "sale_order_id": so.id,
                "partner_id": so.partner_id.id,
                "source_model": "sale.order",
                "source_res_id": so.id,
                "internal_notes": f"[fp:{fp}]",
            })
        action = so.action_generate_dispatch_from_text()
        self.assertEqual(action["params"]["title"], "Legacy Dispatch Booking Reused")
        self.assertEqual(action["params"]["next"]["res_model"], "prema.dispatch.job")
        self.assertEqual(len(self._bookings_for(so)), 0)

    def _make_so_for_legacy(self):
        so = self._make_priced_so()
        self.env["prema.dispatch.job"].with_context(
            dispatch_job_legacy_create=True).create({
                "sale_order_id": so.id,
                "partner_id": so.partner_id.id,
                "source_model": "sale.order",
                "source_res_id": so.id,
            })
        self.assertEqual(len(so.dispatch_job_ids), 1)
        return so

    # ── (d) Orphan guard in dispatch_job.create ──────────────────────────

    def test_direct_sale_order_job_create_is_blocked(self):
        with self.assertRaises(ValidationError):
            self.env["prema.dispatch.job"].create({
                "source_model": "sale.order",
                "source_res_id": 1,
            })

    def test_legacy_escape_hatch_context_allows_historical_reproduction(self):
        job = self.env["prema.dispatch.job"].with_context(
            dispatch_job_legacy_create=True).create({
                "source_model": "sale.order",
                "source_res_id": 999,
            })
        self.assertTrue(job)
        self.assertFalse(job.logistics_booking_id)

    def test_planner_style_unlinked_create_still_allowed(self):
        """Manual/legacy Planner creates (no booking, no operation key, no
        sale.order origin) remain possible — the guard audits, it does not
        raise on them."""
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "ref": "Planner-style manual card",
        })
        self.assertTrue(job)
        self.assertFalse(job.logistics_booking_id)

    # ── misc ─────────────────────────────────────────────────────────────

    def test_text_fingerprint_is_stable_across_whitespace(self):
        so = self._make_priced_so()
        self.assertEqual(
            so._so_text_fingerprint("  Ship  one  \n"),
            so._so_text_fingerprint("Ship one"),
        )
        self.assertNotEqual(
            so._so_text_fingerprint("Ship one"),
            so._so_text_fingerprint("Ship two"),
        )

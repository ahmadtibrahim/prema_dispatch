# -*- coding: utf-8 -*-
"""Work package D-B2 — §6 freight identifiers + §7 payment/QuickPay.

Acceptance coverage of the master requirements, on top of the existing
lifecycle suite (test_custom_quote_lifecycle):

§6 identifiers — customer_po / reference / bol_number / pod_number:
(a) customer_po is populated ONLY from a customer-supplied PO (the lead's
    "Customer PO #"); nothing anywhere derives a PO from the Internal Load
    Reference, the BOL or the legacy ref;
(b) the Internal Load Reference is generated exactly once (defaults to the
    RC number at conversion) and propagates UNCHANGED
    RC → booking → dispatch job → invoice, with no PO/BOL fallback;
(c) the POD number is captured ONCE from the existing completion-flow POD
    evidence at invoice time (idempotent, never invented).

§7 payment / tax / QuickPay:
(d) QuickPay is OFF by default; the discount appears only when the
    customer profile enables it (or a recorded per-document override);
    a manual price below the system price never stacks with QuickPay
    silently — blocked unless stacking is explicitly allowed;
(e) the per-document payment-method override is never silently replaced:
    every change is audit-trailed (quote chatter / booking mail.message),
    out-of-allowed-methods writes are refused, and the method + terms +
    QuickPay snapshot travel onto the invoice;
(f) tax-inclusive and tax-exclusive totals are both preserved through
    invoicing (inclusive 1000 + 13% → invoice total 1000.00, not 1130);
(g) the customer documents (RC email payload, invoice narration) show the
    due-date terms, fees, e-Transfer instructions / card link and the
    QuickPay numbers with the original balance clearly displayed.

Run: --test-tags /prema_logistics_booking/tests/test_d_b2_identifiers_payment
"""
import datetime

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from unittest.mock import patch

# Synthetic test addresses (streets that do not exist) — never real
# customer facilities; the orchestration service is mocked for the
# conversion path exactly like the lifecycle suite.
PICKUP_ADDRESS = "1406 Test Line 8, Ayr, ON N0B 1E0"
PICKUP_POSTAL = "N0B 1E0"
DELIVERY_ADDRESS = "2277 Test Sideroad 15, Ayr, ON N0B 1E0"
DELIVERY_POSTAL = "N0B 1E0"

BOOKING_MANAGER = "prema_logistics_booking.group_logistics_booking_manager"


class TestDB2IdentifiersPayment(TransactionCase):
    """D-B2 §6/§7 — identifiers, payment, QuickPay, tax modes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        group = env.ref(BOOKING_MANAGER)
        if not env.user.has_group(BOOKING_MANAGER):
            env.user.write({"groups_id": [(4, group.id)]})
        # AI description enrichment must stay offline in tests (the
        # deterministic description is what we assert on).
        env["ir.config_parameter"].sudo().set_param("deepseek.api_key", "")

        # Seeded payment-method catalog (§7).
        cls.card = env.ref("prema_logistics_booking.payment_method_credit_card")
        cls.etransfer = env.ref(
            "prema_logistics_booking.payment_method_interac_etransfer")

        # Payment-configured customer: allowed methods + default method +
        # e-Transfer instructions + a payment term. QuickPay stays OFF
        # (the default) on this profile.
        cls.partner = env["res.partner"].create({
            "name": "D-B2 Payment Customer",
            "is_company": True,
            "email": "db2-pay@example.test",
        })
        cls.terms30 = env["account.payment.term"].search([], limit=1)
        if not cls.terms30:
            cls.terms30 = env["account.payment.term"].create({
                "name": "D-B2 Net 30",
                "line_ids": [(0, 0, {"value": "balance", "days": 30})],
            })
        cls.partner.write({
            "x_logistics_allowed_payment_method_ids": [
                (6, 0, [cls.card.id, cls.etransfer.id])],
            "x_logistics_default_payment_method_id": cls.card.id,
            "x_logistics_etransfer_instructions":
                "e-Transfer to pay@premafirm.test — answer FR8-DB2.",
            "property_payment_term_id": cls.terms30.id,
        })
        cls.lead = env["crm.lead"].create({
            "name": "D-B2 Payment Opportunity",
            "partner_id": cls.partner.id,
        })

        # QuickPay-eligible customer profile (enabled + 2.5% + 10 days).
        cls.qp_partner = env["res.partner"].create({
            "name": "D-B2 QuickPay Customer",
            "is_company": True,
            "email": "db2-qp@example.test",
            "x_logistics_allowed_payment_method_ids": [
                (6, 0, [cls.card.id, cls.etransfer.id])],
            "x_logistics_default_payment_method_id": cls.card.id,
            "x_logistics_etransfer_instructions":
                "e-Transfer to pay@premafirm.test — answer QP-DB2.",
            "x_logistics_quickpay_enabled": True,
            "x_logistics_quickpay_discount_pct": 2.5,
            "x_logistics_quickpay_deadline_days": 10,
        })
        cls.qp_lead = env["crm.lead"].create({
            "name": "D-B2 QuickPay Opportunity",
            "partner_id": cls.qp_partner.id,
        })

        corridor = env["logistics.corridor"].create({
            "name": "D-B2 Test Corridor",
            "equipment_type": "dry",
            "direction": "bidirectional",
        })
        cls.departure = env["logistics.corridor.departure"].create({
            "corridor_id": corridor.id,
            "departure_date": datetime.date(2026, 9, 22),
        })

        # 13% HST for the tax-mode tests (percent-only, solvable both ways).
        cls.tax13 = env["account.tax"].create({
            "name": "D-B2 Test HST 13%",
            "amount": 13.0,
            "amount_type": "percent",
            "type_tax_use": "sale",
        })

        # Freight product mapping: the prod dump already points at the
        # configured CA dry-LTL product; a scratch DB without it gets a
        # synthetic product so invoice creation stays deterministic.
        cls._ensure_freight_product()

    @classmethod
    def _ensure_freight_product(cls):
        env = cls.env
        ICP = env["ir.config_parameter"].sudo()
        product_id = int(ICP.get_param(
            "logistics.product_ca_dry_ltl_id", "0") or "0")
        product = env["product.product"].browse(product_id)
        if product.exists():
            cls.freight_product = product
            return
        account = env["account.account"].search([
            ("account_type", "=", "income"),
            ("deprecated", "=", False),
        ], limit=1)
        product = env["product.product"].create({
            "name": "D-B2 Freight (Dry LTL)",
            "type": "service",
            "list_price": 0.0,
            "property_account_income_id": account.id,
        })
        ICP.set_param("logistics.product_ca_dry_ltl_id", product.id)
        cls.freight_product = product

    # ── Fixture helpers ──────────────────────────────────────────────

    def _mk_quote(self, partner=None, lead=None, extra=None, **kw):
        """Open (quoted, priced, departure-assigned) draft RC."""
        partner = partner or self.partner
        lead = lead or self.lead
        vals = {
            "partner_id": partner.id,
            "crm_lead_id": lead.id,
            "source": "internal",
            "contact_name": partner.name,
            "contact_email": partner.email,
            "pickup_postal_code": PICKUP_POSTAL,
            "pickup_address": PICKUP_ADDRESS,
            "delivery_postal_code": DELIVERY_POSTAL,
            "delivery_address": DELIVERY_ADDRESS,
            "pallets": 4,
            "weight_lbs": 2000.0,
            "temperature_mode": "dry",
            "load_type": "ltl",
            "commodity": "D-B2 widgets",
            "system_calculated_price": 850.0,
            "quoted_price": 850.0,
            "manual_price_reason": "",
            "departure_id": self.departure.id,
            "state": "quoted",
        }
        vals.update(extra or {})
        vals.update(kw)
        return self.env["logistics.custom.quote"].create(vals)

    def _booking_vals(self, partner=None, extra=None, **kw):
        """Minimal booking exactly like the orchestration service does
        (booking_number is readonly-after-create, so set at create)."""
        vals = {
            "partner_id": (partner or self.partner).id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 22),
            "estimated_delivery_date": datetime.date(2026, 9, 22),
            "price_snapshot": [{"line": "D-B2 test"}],
            "booking_number":
                self.env["logistics.booking"]._generate_booking_number(),
            "calculated_price": 850.0,
        }
        vals.update(extra or {})
        vals.update(kw)
        return vals

    def _mk_booking(self, partner=None, extra=None, **kw):
        return self.env["logistics.booking"].create(
            self._booking_vals(partner=partner, extra=extra, **kw))

    def _mk_shipment_booking(self, partner=None, extra=None, **kw):
        """Booking with pickup/delivery stops → can produce dispatch jobs
        (mirrors the legacy no-legs bridge used by the P14 suite)."""
        booking = self._mk_booking(partner=partner, extra=extra, **kw)
        loc = self._location
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10,
             "stop_type": "pickup", "saved_location_id": loc("D-B2 Pickup").id,
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20,
             "stop_type": "delivery",
             "saved_location_id": loc("D-B2 Delivery").id,
             "city": "Delivery City", "pallet_count": 1},
        ])
        return booking

    _loc_n = 0

    def _location(self, name):
        type(self)._loc_n += 1
        n = self._loc_n
        return self.env["prema.dispatch.location"].create({
            "name": name,
            "address": f"505 D-B2 Ave #{n}, Ontario",
            "pin_lat": 43.63, "pin_lng": -79.46,
        })

    def _fake_orchestration(self):
        """Capture the conversion payload; confirm returns a real booking
        built from the payload's §6/§7 keys (the mapping the real service
        performs verbatim). Returns (payload_store, patch1, patch2)."""
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        payload_store = {}
        # Bound TestCase helper — fake_confirm runs AS the service (its
        # ``self`` is the patched service object), so it must not look up
        # helpers on ``self``.
        booking_vals = self._booking_vals

        def fake_normalize(self, values, **kwargs):
            payload_store["values"] = values
            return {"request": values, "source_channel":
                    kwargs.get("source_channel")}

        def fake_confirm(self, norm, **kwargs):
            payload = norm["request"] if isinstance(norm, dict) else {}
            extra = {}
            for key in ("reference", "po_number", "bol_number",
                        "price_tax_mode", "payment_method_id",
                        "payment_term_id", "quickpay_apply",
                        "quickpay_discount_pct", "quickpay_deadline_days",
                        "quickpay_stack_allowed", "quickpay_override_reason"):
                if key in payload and payload[key] is not None:
                    extra[key] = payload[key]
            partner = self.env["res.partner"].browse(
                payload.get("partner_id") or False)
            return self.env["logistics.booking"].create(
                booking_vals(partner=partner, **extra))

        return payload_store, (
            patch.object(BookingOrchestrationService, "normalize_request",
                         fake_normalize),
            patch.object(BookingOrchestrationService, "confirm_from_internal",
                         fake_confirm),
        )

    def _accept_confirm_convert(self, quote):
        """Full staff GO path with the booking engine mocked — returns the
        captured payload dict."""
        payload_store, (norm_patch, confirm_patch) = self._fake_orchestration()
        with norm_patch, confirm_patch:
            quote.action_record_customer_acceptance("email")
            quote.action_confirm_internally()
            quote.action_convert_to_booking()
        return payload_store["values"]

    def _booking_audits(self, booking, needle=""):
        domain = [("model", "=", "logistics.booking"),
                  ("res_id", "=", booking.id)]
        if needle:
            domain.append(("body", "ilike", needle))
        return self.env["mail.message"].sudo().search(domain)

    def _quote_audits(self, quote, needle=""):
        domain = [("model", "=", "logistics.custom.quote"),
                  ("res_id", "=", quote.id)]
        if needle:
            domain.append(("body", "ilike", needle))
        return self.env["mail.message"].sudo().search(domain)

    # ── §6a: customer_po is never auto-filled ────────────────────────

    def test_a_po_only_from_customer_supplied_source(self):
        """PO comes ONLY from the lead's customer PO; a reference/BOL on
        the document never spawns one, on the quote, the booking or the
        conversion payload."""
        # Lead WITHOUT a PO → quote carries no PO.
        lead_plain = self.env["crm.lead"].create({
            "name": "D-B2 No-PO Opportunity",
            "partner_id": self.partner.id,
        })
        vals = self.env["logistics.custom.quote"]._prepare_from_lead(
            lead_plain)
        quote = self.env["logistics.custom.quote"].create(vals)
        self.assertFalse(quote.customer_po)
        self.assertFalse(quote.reference)

        # Lead WITH a customer PO → that exact PO lands on the quote.
        lead_po = self.env["crm.lead"].create({
            "name": "D-B2 PO Opportunity",
            "partner_id": self.partner.id,
            "po_number": "PO-ALPHA-77",
        })
        quote_po = self.env["logistics.custom.quote"].create(
            self.env["logistics.custom.quote"]._prepare_from_lead(lead_po))
        self.assertEqual(quote_po.customer_po, "PO-ALPHA-77")

        # Quote with a staff reference and NO PO → conversion carries the
        # reference and an EMPTY po_number (nothing is ever derived).
        priced = self._mk_quote(extra={"reference": "REF-NOPO-1"})
        self.assertFalse(priced.customer_po)
        payload = self._accept_confirm_convert(priced)
        self.assertEqual(payload["po_number"], "")
        self.assertEqual(payload["reference"], "REF-NOPO-1")
        self.assertNotIn("customer_reference", payload,
                         "the legacy ref fallback must never become a PO alias")
        self.assertFalse(priced.booking_id.po_number)
        self.assertEqual(priced.booking_id.reference, "REF-NOPO-1")

    # ── §6b: reference generation + unchanged propagation ────────────

    def test_b_reference_generated_once_and_propagated_unchanged(self):
        """Empty reference → generated from the RC number at conversion,
        then identical on the booking payload and the booking row."""
        quote = self._mk_quote()
        self.assertFalse(quote.reference)
        payload = self._accept_confirm_convert(quote)
        # Generated exactly once, from the RC number — never from PO/BOL.
        self.assertEqual(quote.reference, quote.name)
        self.assertEqual(payload["reference"], quote.name)
        self.assertEqual(quote.booking_id.reference, quote.name)
        self.assertEqual(payload["po_number"], "")
        self.assertEqual(payload["bol_number"], "")
        # A pre-assigned staff reference is kept as-is (not re-derived).
        quote2 = self._mk_quote(extra={"reference": "REF-MANUAL-2"})
        payload2 = self._accept_confirm_convert(quote2)
        self.assertEqual(quote2.reference, "REF-MANUAL-2")
        self.assertEqual(payload2["reference"], "REF-MANUAL-2")

    def test_c_reference_and_po_on_job_and_invoice_without_fallback(self):
        """Booking → dispatch job inherits the same reference; the invoice
        carries load_reference = reference and premafirm_po = PO — and an
        EMPTY reference never falls back to the PO or the BOL."""
        booking = self._mk_shipment_booking(extra={
            "reference": "REF-JOB-117", "po_number": "", "bol_number": "",
            "calculated_price": 500.0,
        })
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertEqual(job.reference, "REF-JOB-117")
        self.assertFalse(job.po_number)
        self.assertFalse(job.bol_number)

        # The operational BOL is entered on the JOB and mirrors up to the
        # booking (booking stays the authority once set — never overwritten).
        job.write({"bol_number": "BOL-SHP-42"})
        self.assertEqual(booking.bol_number, "BOL-SHP-42")
        job.write({"bol_number": "BOL-OTHER-9"})
        self.assertEqual(booking.bol_number, "BOL-SHP-42",
                         "booking BOL is the authority once set")

        invoice = booking._create_draft_invoice()
        self.assertTrue(invoice)
        Invoice = self.env["account.move"].sudo()
        if "load_reference" in Invoice._fields:
            self.assertEqual(invoice.load_reference, "REF-JOB-117")
        if "premafirm_po" in Invoice._fields:
            self.assertFalse(invoice.premafirm_po)
        if "premafirm_bol" in Invoice._fields:
            self.assertEqual(invoice.premafirm_bol, "BOL-SHP-42")

        # No-fallback: booking WITHOUT reference but WITH a PO.
        booking2 = self._mk_booking(extra={
            "po_number": "PO-ONLY-9", "calculated_price": 500.0})
        self.assertFalse(booking2.reference)
        invoice2 = booking2._create_draft_invoice()
        self.assertTrue(invoice2)
        if "load_reference" in Invoice._fields:
            self.assertFalse(invoice2.load_reference)
        if "premafirm_po" in Invoice._fields:
            self.assertEqual(invoice2.premafirm_po, "PO-ONLY-9")

    # ── §6c: POD capture at completion (existing evidence) ───────────

    def test_d_pod_number_from_completion_evidence_once(self):
        """pod_number is captured from the POD evidence of the existing
        completion flow — first uploaded file wins, idempotent, never
        rewritten, deterministic fallback without evidence."""
        booking = self._mk_shipment_booking()
        job = booking._create_dispatch_job()
        self.assertTrue(job)

        def _evidence(filename, uploaded_at, ev_type="scanned_pod"):
            att = self.env["ir.attachment"].create({
                "name": filename,
                "mimetype": "application/pdf",
                "raw": b"%PDF-1.4 D-B2 fake pod",
            })
            return self.env["prema.dispatch.evidence"].sudo().create({
                "attachment_id": att.id,
                "evidence_type": ev_type,
                "job_id": job.id,
                "booking_id": booking.id,
                "original_filename": filename,
                "uploaded_at": uploaded_at,
            })

        _evidence("POD-99812-final.PDF",
                  datetime.datetime(2026, 9, 5, 10, 0, 0))
        self.assertFalse(booking.pod_number)
        self.assertEqual(booking._capture_pod_number(), "POD-99812-final")
        self.assertEqual(booking.pod_number, "POD-99812-final")

        # A later upload never rewrites the captured number.
        _evidence("POD-99999-superseding.pdf",
                  datetime.datetime(2026, 9, 5, 11, 0, 0), "pod_general")
        self.assertEqual(booking._capture_pod_number(), "POD-99812-final")
        self.assertEqual(booking.pod_number, "POD-99812-final")
        self.assertEqual(
            len(self._booking_audits(booking, "POD")), 1,
            "exactly one POD audit row — capture is write-once")

        # No evidence → deterministic booking-number label, still once.
        empty = self._mk_booking()
        captured = empty._capture_pod_number()
        self.assertTrue(captured.endswith("-POD"))
        self.assertEqual(empty.pod_number, captured)

    # ── §7a: QuickPay disabled by default ─────────────────────────────

    def test_e_quickpay_off_by_default_only_when_configured(self):
        """Non-eligible customer: QuickPay stays OFF and no discount is
        shown. Eligible customer: the profile's offer is applied and the
        numbers shown; original balance is never overwritten."""
        quote = self._mk_quote()
        self.assertFalse(quote.quickpay_apply)
        self.assertEqual(quote.quickpay_discount_amount, 0.0)
        self.assertEqual(quote.quickpay_discounted_total, 0.0)
        self.assertEqual(quote.quoted_price, 850.0)

        qp = self._mk_quote(partner=self.qp_partner, lead=self.qp_lead)
        self.assertTrue(qp.quickpay_apply,
                        "eligible profile offer applied on the document")
        self.assertEqual(qp.quickpay_discount_pct, 2.5)
        self.assertEqual(qp.quickpay_deadline_days, 10)
        self.assertEqual(qp.quickpay_discount_amount, 21.25)
        self.assertEqual(qp.quickpay_discounted_total, 828.75)
        self.assertEqual(qp.quoted_price, 850.0,
                         "original balance is never rewritten by QuickPay")

        # Conversion carries the QuickPay agreement onto the booking.
        payload = self._accept_confirm_convert(qp)
        self.assertTrue(payload["quickpay_apply"])
        self.assertEqual(payload["quickpay_discount_pct"], 2.5)
        self.assertTrue(qp.booking_id.quickpay_apply)
        self.assertEqual(qp.booking_id.quickpay_discount_pct, 2.5)

    def test_f_quickpay_override_and_stacking_guards(self):
        """A per-document QuickPay override on a non-eligible customer
        requires a recorded reason; QuickPay never stacks with a manual
        price adjustment silently."""
        # Override without a reason → refused.
        with self.assertRaises(UserError):
            self._mk_quote(extra={
                "quickpay_apply": True, "quickpay_discount_pct": 3.0,
                "quickpay_deadline_days": 7})
        # Same override WITH the recorded reason → allowed + audited.
        quote = self._mk_quote(extra={
            "quickpay_apply": True, "quickpay_discount_pct": 3.0,
            "quickpay_deadline_days": 7,
            "quickpay_override_reason": "One-time courtesy match"})
        self.assertTrue(quote.quickpay_apply)
        self.assertEqual(quote.quickpay_override_reason,
                         "One-time courtesy match")
        self.assertEqual(quote.quickpay_discount_amount, 25.50)
        self.assertEqual(quote.quickpay_discounted_total, 824.50)

        # Turning QuickPay on without a percentage → refused.
        with self.assertRaises(UserError):
            self._mk_quote(partner=self.qp_partner, lead=self.qp_lead,
                           extra={"quickpay_apply": True,
                                  "quickpay_discount_pct": 0.0})

        # Stacking guard: eligible customer, quoted BELOW the system
        # price, stacking not allowed → refused.
        with self.assertRaises(UserError):
            self._mk_quote(partner=self.qp_partner, lead=self.qp_lead,
                           extra={"quoted_price": 800.0,
                                  "system_calculated_price": 850.0})
        # Explicit document-level stacking allowance → allowed.
        stacked = self._mk_quote(partner=self.qp_partner, lead=self.qp_lead,
                                 extra={"quoted_price": 800.0,
                                        "system_calculated_price": 850.0,
                                        "manual_price_reason": "Volume",
                                        "quickpay_stack_allowed": True})
        self.assertTrue(stacked.quickpay_apply)
        self.assertTrue(stacked.quickpay_stack_allowed)
        # Customer-profile stacking allowance auto-inherits.
        self.qp_partner.write({"x_logistics_quickpay_stack_allowed": True})
        inherited = self._mk_quote(partner=self.qp_partner,
                                   lead=self.qp_lead,
                                   extra={"quoted_price": 800.0,
                                          "system_calculated_price": 850.0,
                                          "manual_price_reason": "Volume"})
        self.assertTrue(inherited.quickpay_stack_allowed)

    # ── §7b: payment override never silent ───────────────────────────

    def test_g_payment_method_override_never_silent(self):
        """Per-document override defaults from the profile, stays inside
        the allowed methods, and every change is audit-trailed."""
        quote = self._mk_quote()
        self.assertEqual(quote.payment_method_id.id, self.card.id,
                         "profile default method applied")
        self.assertEqual(quote.payment_term_id.id, self.terms30.id,
                         "profile default terms applied")

        # Override to a still-allowed method → tracked on the chatter
        # (tracking=True posts a message row on the write).
        audits_before = len(self._quote_audits(quote))
        quote.write({"payment_method_id": self.etransfer.id})
        self.assertEqual(quote.payment_method_id.id, self.etransfer.id)
        self.assertGreater(
            len(self._quote_audits(quote)), audits_before,
            "the override is recorded on the quote chatter")

        # Override outside the allowed list → refused, nothing written.
        terms_method = self.env.ref(
            "prema_logistics_booking.payment_method_terms_30")
        with self.assertRaises(UserError):
            quote.write({"payment_method_id": terms_method.id})
        self.assertEqual(quote.payment_method_id.id, self.etransfer.id)

    def test_h_booking_change_audited_and_carried_to_invoice(self):
        """Booking-side changes write mail.message audit rows (the booking
        has no chatter), and the agreed method + terms + QuickPay snapshot
        travel onto the invoice unchanged."""
        booking = self._mk_booking(partner=self.partner, extra={
            "payment_method_id": self.card.id,
            "payment_term_id": self.terms30.id,
            "price_tax_mode": "exclusive",
            "calculated_price": 1000.0,
        })
        booking.write({"payment_method_id": self.etransfer.id})
        booking.write({"quickpay_apply": True,
                       "quickpay_discount_pct": 3.0,
                       "quickpay_deadline_days": 10})
        audits = self._booking_audits(booking)
        self.assertTrue(any("Payment Method changed" in (m.body or "")
                            for m in audits))
        self.assertTrue(any("QuickPay Discount % changed" in (m.body or "")
                            for m in audits))
        self.assertTrue(any("QuickPay Discount Applies changed"
                            in (m.body or "") for m in audits))

        invoice = booking._create_draft_invoice()
        self.assertTrue(invoice)
        self.assertEqual(invoice.logistics_payment_method_id.id,
                         self.etransfer.id)
        self.assertEqual(invoice.invoice_payment_term_id.id,
                         self.terms30.id)
        self.assertEqual(invoice.logistics_price_tax_mode, "exclusive")
        self.assertTrue(invoice.logistics_quickpay_apply)
        self.assertEqual(invoice.logistics_quickpay_discount_pct, 3.0)
        self.assertIn("pay@premafirm.test",
                      invoice.logistics_payment_instructions or "")
        # QuickPay deadline date = invoice date + deadline days.
        invoice.write({"invoice_date": datetime.date(2026, 9, 10)})
        self.assertEqual(invoice.logistics_quickpay_deadline_date,
                         datetime.date(2026, 9, 20))

        # The document-level override is editable and tracked on the move
        # (tracking=True on the account.move field → chatter row).
        before = self.env["mail.message"].sudo().search_count([
            ("model", "=", "account.move"),
            ("res_id", "=", invoice.id)])
        invoice.write({"logistics_payment_method_id": self.card.id})
        self.assertEqual(invoice.logistics_payment_method_id.id,
                         self.card.id)
        self.assertGreater(self.env["mail.message"].sudo().search_count([
            ("model", "=", "account.move"),
            ("res_id", "=", invoice.id)]), before,
            "the invoice-level override is audit-trailed too")

    # ── §7c: tax-inclusive total preserved ───────────────────────────

    def test_i_tax_inclusive_and_exclusive_totals_preserved(self):
        """Inclusive 1000 + 13% → invoice total 1000.00 (line price
        solved); exclusive 1000 + 13% → 1130.00. The booking snapshot
        matches the invoice in both modes."""
        # Exclusive (the historical default).
        excl = self._mk_booking(extra={
            "price_tax_mode": "exclusive",
            "calculated_price": 1000.0,
            "tax_rule_id": self.tax13.id,
        })
        inv_excl = excl._create_draft_invoice()
        self.assertTrue(inv_excl)
        self.assertAlmostEqual(inv_excl.invoice_line_ids[0].price_unit,
                               1000.0, places=2)
        self.assertAlmostEqual(inv_excl.amount_total, 1130.0, places=2)
        self.assertAlmostEqual(excl.amount_total, 1130.0, places=2)

        # Inclusive: the agreed total stays 1000.00.
        incl = self._mk_booking(extra={
            "price_tax_mode": "inclusive",
            "calculated_price": 1000.0,
            "tax_rule_id": self.tax13.id,
        })
        inv_incl = incl._create_draft_invoice()
        self.assertTrue(inv_incl)
        # Solved line price (base = 1000 incl of 13%): the field stores 4
        # decimals (Product Price precision), so compare with a delta.
        self.assertAlmostEqual(
            inv_incl.invoice_line_ids[0].price_unit,
            1000.0 * 1000.0 / 1130.0, delta=0.0001)
        self.assertAlmostEqual(inv_incl.amount_total, 1000.0, places=2,
                               msg="inclusive total must never reprice up")
        self.assertAlmostEqual(incl.amount_total, 1000.0, places=2)
        self.assertEqual(incl.amount_total, incl.calculated_price)
        # The mode itself travels onto the invoice.
        self.assertEqual(inv_incl.logistics_price_tax_mode, "inclusive")
        self.assertEqual(inv_excl.logistics_price_tax_mode, "exclusive")

    # ── §7d: customer documents show the payment deal ────────────────

    def test_j_rc_email_and_invoice_show_payment_deal(self):
        """The RC email rows and the invoice narration carry the
        identifiers, the due-date terms, fees/instructions and the
        QuickPay numbers with the original balance visible."""
        quote = self._mk_quote(partner=self.qp_partner, lead=self.qp_lead,
                               extra={"customer_po": "PO-111",
                                      "reference": "REF-SEND-1",
                                      "payment_method_id": self.etransfer.id})
        payload = quote._build_send_payload()
        body = payload["body_html"]
        self.assertIn("REF-SEND-1", body)
        self.assertIn("PO-111", body)
        self.assertIn("Interac e-Transfer", body)
        self.assertIn("QuickPay Discount (paid by deadline)", body)
        self.assertIn("828.75", body)  # 850 − 2.5%

        booking = self._mk_booking(partner=self.qp_partner, extra={
            "reference": "REF-INV-1",
            "po_number": "PO-INV-9",
            "payment_method_id": self.etransfer.id,
            "payment_term_id": self.terms30.id,
            "quickpay_apply": True,
            "quickpay_discount_pct": 3.0,
            "quickpay_deadline_days": 10,
            "calculated_price": 1000.0,
        })
        # INV/2026/00091 presentation: the deterministic description is a
        # CLEAN summary — no internal refs / PO / payment text / dumps.
        description = booking._generate_invoice_description()
        self.assertTrue(description.startswith("Freight / Delivery Service"))
        self.assertIn("Date: September 22, 2026", description)
        self.assertNotIn("Internal Load Reference: REF-INV-1", description)
        self.assertNotIn("PO: PO-INV-9", description)
        self.assertNotIn("Payment:", description)

        invoice = booking._create_draft_invoice()
        self.assertTrue(invoice)
        self.assertAlmostEqual(invoice.amount_total, 1000.0, places=2)
        product_line = invoice.invoice_line_ids.filtered(
            lambda line: line.display_type == "product")[:1]
        note_line = invoice.invoice_line_ids.filtered(
            lambda line: line.display_type == "line_note")[:1]
        self.assertTrue(product_line)
        self.assertTrue(note_line)
        product, _country = booking._select_freight_product()
        self.assertEqual(product_line.name,
                         product.display_name or product.name)
        self.assertEqual(note_line.name, description)
        # The §7 payment deal travels in the NARRATION (printed by the
        # report on the customer document), never in the lines.
        narration = invoice.narration or ""
        self.assertIn("Terms: %s" % self.terms30.name, narration)
        self.assertIn("Method: Interac e-Transfer", narration)
        self.assertIn("pay@premafirm.test", narration)
        self.assertIn("within 10 day(s) of the invoice date", narration)
        self.assertIn("deduct 3% (30.00)", narration)
        self.assertIn("balance due 970.00", narration)
        self.assertIn("original balance 1000.00", narration)

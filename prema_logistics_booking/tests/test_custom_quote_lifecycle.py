import datetime
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

# Test fixture addresses are SYNTHETIC on purpose — these streets do not
# exist anywhere; they never hit real customer facilities, unique address
# constraints or geocoding (the orchestration service is mocked here
# anyway; this suite asserts the Rate Confirmation lifecycle, not the
# booking engine).
PICKUP_ADDRESS = "1406 Test Line 8, Ayr, ON N0B 1E0"
PICKUP_POSTAL = "N0B 1E0"
DELIVERY_ADDRESS = "2277 Test Sideroad 15, Ayr, ON N0B 1E0"
DELIVERY_POSTAL = "N0B 1E0"

BOOKING_MANAGER = "prema_logistics_booking.group_logistics_booking_manager"
PRICING_VIEWER = "prema_logistics_booking.group_logistics_pricing_viewer"


class TestCustomQuoteLifecycle(TransactionCase):
    """Customer Rate Confirmation lifecycle — master-audit §3.7/§4.

    (a) preview / acceptance recording / saves never create a send-attempt
        row or an email — sending is EXPLICIT only;
    (b) one explicit send → exactly ONE attempt row, even when a second
        send races it;
    (c) recorded acceptance alone cannot convert; internal confirmation +
        acceptance can;
    (d) convert is idempotent — exactly one booking from two calls;
    (e) Revise & Resend branches revision N+1, keeps the sent revision's
        markers, and never re-emails a sent revision;
    (f) a lead has at most one open draft: repeated find_or_create returns
        the same row, and a NEW draft may be created only after conversion.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.booking_manager_group = env.ref(BOOKING_MANAGER)
        if not env.user.has_group(BOOKING_MANAGER):
            # Revise / override paths are gated on this group; the audit
            # suite user is not guaranteed to hold it on every derived DB.
            env.user.write({"groups_id": [(4, cls.booking_manager_group.id)]})
        cls.partner = env["res.partner"].create({
            "name": "RC Lifecycle Customer",
            "is_company": True,
            "email": "rc-lifecycle@example.test",
        })
        cls.lead = env["crm.lead"].create({
            "name": "RC Lifecycle Opportunity",
            "partner_id": cls.partner.id,
        })
        corridor = env["logistics.corridor"].create({
            "name": "RC Lifecycle Corridor",
            "equipment_type": "dry",
        })
        cls.departure = env["logistics.corridor.departure"].create({
            "corridor_id": corridor.id,
            "departure_date": datetime.date(2026, 9, 15),
        })

    # ── Fixture helpers ──────────────────────────────────────────────

    def _mk_quote(self, lead=None, **extra):
        """Open (quoted, priced, departure-assigned) draft RC."""
        lead = lead or self.lead
        vals = {
            "partner_id": self.partner.id,
            "crm_lead_id": lead.id,
            "source": "internal",
            "contact_name": self.partner.name,
            "contact_email": self.partner.email,
            "pickup_postal_code": PICKUP_POSTAL,
            "pickup_address": PICKUP_ADDRESS,
            "delivery_postal_code": DELIVERY_POSTAL,
            "delivery_address": DELIVERY_ADDRESS,
            "pallets": 4,
            "weight_lbs": 2000.0,
            "temperature_mode": "dry",
            "load_type": "ltl",
            "commodity": "Lifecycle widgets",
            "system_calculated_price": 850.0,
            "quoted_price": 850.0,
            "manual_price_reason": "",
            "departure_id": self.departure.id,
            "state": "quoted",
        }
        vals.update(extra)
        return self.env["logistics.custom.quote"].create(vals)

    def _mk_booking(self, env):
        """Minimal booking exactly like the orchestration service does
        (booking_number is readonly-after-create, so set at create)."""
        return env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 8),
            "estimated_delivery_date": datetime.date(2026, 9, 8),
            "price_snapshot": [{"line": "RC lifecycle test"}],
            "booking_number":
                env["logistics.booking"]._generate_booking_number(),
        })

    def _fake_orchestration(self, counter=None):
        """Replace BookingOrchestrationService so the booking engine never
        runs (no geocoding, no pricing) — the convert tests assert the CQ
        lifecycle gates, not the engine. Returns the patch context."""
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        mk_booking = self._mk_booking

        def fake_normalize(self, request, **kwargs):
            return {"request": request, "source_channel":
                    kwargs.get("source_channel")}

        def fake_confirm(self, norm, **kwargs):
            if counter is not None:
                counter["calls"] += 1
            return mk_booking(self.env)

        return patch.object(
            BookingOrchestrationService, "normalize_request",
            fake_normalize), patch.object(
            BookingOrchestrationService, "confirm_from_internal",
            fake_confirm)

    def _attempt_count(self, quote):
        return self.env["logistics.custom.quote.send.attempt"].search_count(
            [("cq_id", "=", quote.id)])

    # ── (a) preview / acceptance / save never send ───────────────────

    def test_a_preview_acceptance_and_saves_never_send(self):
        quote = self._mk_quote()
        mails_before = self.env["mail.mail"].search_count([])
        attempts_before = self._attempt_count(quote)

        # Preview is a pure read: it returns the PDF report action and
        # writes nothing.
        action = quote.action_preview()
        self.assertEqual(action["type"], "ir.actions.report")

        # Staff records out-of-band acceptance (phone) — records ONLY.
        self.assertTrue(quote.action_record_customer_acceptance("phone"))
        self.assertEqual(quote.state, "accepted")
        self.assertEqual(quote.acceptance_channel, "phone")
        self.assertTrue(quote.acceptance_recorded_at)
        self.assertTrue(quote.acceptance_recorded_by)

        # Internal confirmation — internal only, never emailed.
        self.assertTrue(quote.action_confirm_internally())
        self.assertTrue(quote.internal_confirmed)

        # Plain saves (staff editing notes) after acceptance.
        quote.write({"internal_notes": "edited after acceptance"})

        self.assertEqual(self._attempt_count(quote), attempts_before)
        self.assertEqual(
            self.env["mail.mail"].search_count([]), mails_before,
            "Nothing in the preview/accept/save path may ever create mail.")

    # ── (b) one-shot idempotent send ─────────────────────────────────

    def test_b_explicit_send_is_exactly_one_attempt(self):
        quote = self._mk_quote()
        with patch.object(
                self.env["logistics.custom.quote"],
                "_render_quotation_pdf",
                return_value=b"%PDF-1.4\nlifecycle-test-pdf"), \
                patch.object(self.env["mail.mail"], "send") as mail_send:
            quote.action_send_rc()
            # A rapid second click/retry must be REFUSED, not duplicated.
            with self.assertRaises(UserError):
                quote.action_send_rc()
        self.assertEqual(mail_send.call_count, 1)
        attempts = self.env["logistics.custom.quote.send.attempt"].search(
            [("cq_id", "=", quote.id)])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts.state, "sent")
        self.assertEqual(attempts.revision_no, 1)
        self.assertEqual(attempts.template_ref,
                         "prema_logistics_booking."
                         "action_report_logistics_quotation")
        self.assertEqual(len(attempts.send_hash), 40)
        # Marker written BEFORE queueing, still set after the refusal.
        self.assertTrue(quote.last_sent_at)
        self.assertEqual(quote.last_sent_attempt_id.id, attempts.id)
        self.assertTrue(quote.is_locked)
        # The refusal message came from the attempt history.
        sent_mail = mail_send.call_args[0][0]
        self.assertEqual(sent_mail.email_to, self.partner.email)
        self.assertIn("Rate Confirmation", sent_mail.subject)
        self.assertEqual(len(sent_mail.attachment_ids), 1)
        self.assertEqual(sent_mail.attachment_ids.mimetype, "application/pdf")

    # ── (c) acceptance alone cannot convert ──────────────────────────

    def test_c_acceptance_alone_cannot_convert(self):
        quote = self._mk_quote()
        quote.action_record_customer_acceptance("email")
        self.assertEqual(quote.state, "accepted")
        counter = {"calls": 0}
        norm_patch, confirm_patch = self._fake_orchestration(counter)
        with norm_patch, confirm_patch:
            with self.assertRaises(UserError):
                quote.action_convert_to_booking()
            self.assertEqual(counter["calls"], 0)
            self.assertFalse(quote.booking_id)
            # Internal staff GO decision unblocks conversion.
            quote.action_confirm_internally()
            booking = quote.action_convert_to_booking()
            self.assertTrue(booking)
            self.assertEqual(quote.booking_id, booking)
            self.assertEqual(quote.state, "converted")
            self.assertEqual(counter["calls"], 1)

    # ── (d) convert yields exactly one booking ───────────────────────

    def test_d_convert_is_idempotent_one_booking(self):
        quote = self._mk_quote()
        quote.action_record_customer_acceptance("phone")
        quote.action_confirm_internally()
        counter = {"calls": 0}
        norm_patch, confirm_patch = self._fake_orchestration(counter)
        with norm_patch, confirm_patch:
            booking = quote.action_convert_to_booking()
            self.assertTrue(booking)
            booking_again = quote.action_convert_to_booking()
            self.assertEqual(booking_again, booking)
            self.assertEqual(quote.booking_id, booking)
            self.assertEqual(
                self.env["logistics.booking"].search_count(
                    [("id", "=", booking.id)]), 1)
            self.assertEqual(counter["calls"], 1,
                             "The orchestration ran exactly once.")
        # And the booking exists exactly once in the database.
        self.assertEqual(
            self.env["logistics.booking"].search_count([]), 1)

    # ── (e) Revise & Resend branches revision N+1 ────────────────────

    def test_e_revise_bumps_revision_and_never_resends_old(self):
        quote = self._mk_quote()
        with patch.object(
                self.env["logistics.custom.quote"],
                "_render_quotation_pdf",
                return_value=b"%PDF-1.4\nlifecycle-test-pdf"), \
                patch.object(self.env["mail.mail"], "send") as mail_send:
            quote.action_send_rc()
            sent_at_marker = quote.last_sent_at

            # Sent rows are immutable for customer-facing fields — only
            # the authorized revision path may change the document.
            with self.assertRaises(UserError):
                quote.write({"quoted_price": 9999.0})
            # A non-manager cannot revise.
            viewer = self.env["res.users"].create({
                "name": "RC Pricing Viewer", "login": "rc-viewer@test.local",
                "tz": "UTC",
                "groups_id": [(6, 0, [
                    self.env.ref("base.group_user").id,
                    self.env.ref(PRICING_VIEWER).id,
                ])],
            })
            with self.assertRaises(AccessError):
                quote.with_user(viewer).action_revise("not allowed")

            # Booking-manager revision branches revision 2.
            revised = quote.action_revise("Customer asked for a lower rate")
            self.assertEqual(revised.revision_no, 2)
            self.assertEqual(revised.revised_from_id.id, quote.id)
            self.assertEqual(revised.name, quote.name)
            self.assertEqual(revised.state, "quoted")
            self.assertEqual(revised.quoted_price, quote.quoted_price)
            self.assertTrue(revised.revision_reason)
            self.assertTrue(revised.revised_by_id)
            self.assertTrue(revised.revised_at)
            # Acceptance/confirmation do NOT carry over.
            self.assertFalse(revised.internal_confirmed)
            self.assertFalse(revised.idempotency_key)

            # The old revision keeps its sent marker and gains no attempt.
            self.assertEqual(quote.revision_no, 1)
            self.assertTrue(quote.is_locked)
            self.assertTrue(quote.is_superseded)
            self.assertEqual(quote.last_sent_at, sent_at_marker)
            self.assertEqual(self._attempt_count(quote), 1)

            # Only the NEW revision may be sent — and exactly once.
            revised.action_send_rc()
            with self.assertRaises(UserError):
                revised.action_send_rc()
            self.assertEqual(mail_send.call_count, 2)
        self.assertEqual(self._attempt_count(quote), 1,
                         "The sent revision's email is never duplicated.")
        self.assertEqual(self._attempt_count(revised), 1)
        self.assertEqual(revised.revision_no, 2)

        # The old revision can still be declined for the record, but the
        # doc fields stay locked.
        with self.assertRaises(UserError):
            quote.write({"quoted_price": 700.0})

    # ── (f) lead draft uniqueness / find_or_create ───────────────────

    def test_f_one_open_draft_per_lead_find_or_create(self):
        CQ = self.env["logistics.custom.quote"]

        # Repeated calls (any idempotency key) return the SAME draft.
        first = CQ.find_or_create_draft_for_lead(
            self.lead.id, idempotency_key="wave-2-call-1")
        again = CQ.find_or_create_draft_for_lead(
            self.lead.id, idempotency_key="wave-2-call-1")
        other_key = CQ.find_or_create_draft_for_lead(
            self.lead.id, idempotency_key="wave-2-call-2")
        self.assertEqual(first, again)
        self.assertEqual(first, other_key)
        self.assertEqual(CQ.search_count([("crm_lead_id", "=", self.lead.id)]),
                         1)
        self.assertEqual(first.idempotency_key, "wave-2-call-1")

        # A second OPEN draft may not be stacked through the ORM either.
        with self.assertRaises(UserError):
            self._mk_quote()
        self.assertEqual(CQ.search_count([("crm_lead_id", "=", self.lead.id)]),
                         1)

        # Convert the draft (acceptance + internal confirm + engine mock).
        first.write({"system_calculated_price": 850.0,
                     "quoted_price": 850.0, "state": "quoted",
                     "departure_id": self.departure.id,
                     "pickup_address": PICKUP_ADDRESS,
                     "delivery_address": DELIVERY_ADDRESS})
        first.action_record_customer_acceptance("email")
        first.action_confirm_internally()
        counter = {"calls": 0}
        norm_patch, confirm_patch = self._fake_orchestration(counter)
        with norm_patch, confirm_patch:
            booking = first.action_convert_to_booking()
        self.assertTrue(booking)
        self.assertEqual(first.state, "converted")

        # Only AFTER conversion may a fresh draft be created — and a new
        # cycle starts clean (revision 1, no idempotency carried over).
        fresh = CQ.find_or_create_draft_for_lead(
            self.lead.id, idempotency_key="wave-2-second-cycle")
        self.assertNotEqual(fresh.id, first.id)
        self.assertEqual(fresh.revision_no, 1)
        self.assertEqual(fresh.state, "new")
        self.assertEqual(fresh.idempotency_key, "wave-2-second-cycle")
        self.assertFalse(fresh.revised_from_id)
        again = CQ.find_or_create_draft_for_lead(
            self.lead.id, idempotency_key="wave-2-second-cycle")
        self.assertEqual(again, fresh)
        # The converted row is untouched by the new cycle.
        self.assertEqual(first.state, "converted")
        self.assertEqual(first.booking_id, booking)
        self.assertEqual(self._attempt_count(first), 0)

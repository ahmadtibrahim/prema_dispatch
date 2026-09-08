# -*- coding: utf-8 -*-
"""Phase 3 regression suite — D-9 editable shipment extraction
(action_update_extraction), D-10 pricing state + breakdown (the snapshot
is the single pricing authority), F-1 dispatcher price adjustment,
F-2 deterministic Reply with Quote. F-3 (create booking from email)
lives in this file too — its class was added with the F-3 commit.
"""
import datetime
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError

from .common import InboxTestCase

_PICKUP = {"city": "Toronto", "province": "ON", "postal_code": "M5V3E1"}
_DELIVERY = {"city": "Belleville", "province": "ON", "postal_code": "K8N2S1"}


def _seed_extraction(conv, pickup=_PICKUP, delivery=_DELIVERY, **extra):
    """Write a canned, fully-sourced extraction so pricing runs without the
    AI layer — deterministic for hermetic tests (mock AI would re-derive
    fields from the body on every calculate)."""
    fields_ = {
        "pickup": dict(pickup), "delivery": dict(delivery),
        "pallets": 6, "weight_lbs": 4200, "equipment": "Reefer",
        "temperature_c": 3, "accessorials": ["liftgate"],
    }
    fields_.update(extra)
    conv.write({
        "ai_status": "ready",
        "ai_extraction": {
            "fields": fields_,
            "sources": {k: {"source_msg": None, "provenance": "extracted"}
                        for k in fields_},
            "missing": [], "conflicting": [],
        },
    })
    return conv


def _fake_engine(**over):
    """A PricingResult-shaped object — mirrors what PricingService.calculate
    returns (available/price_lines/schedule/route_snapshot/...)."""
    result = SimpleNamespace(
        available=True, reason=False, calculated_price=223.59,
        price_lines=[{"label": "LTL linehaul", "amount": 203.59},
                     {"label": "Liftgate pickup", "amount": 20.00}],
        schedule=[], delivery_date_estimate=None, route_snapshot={},
        recommend_ftl=False, manual_review_required=False)
    for key, value in over.items():
        setattr(result, key, value)
    return result


class TestExtractionUpdateD9(InboxTestCase):
    """Dispatcher edits extraction fields — validate against the canonical
    schema, mark provenance 'manual', never re-run the AI or re-price."""

    def _conv_with_extraction(self):
        _, conv, _ = self.ingest(
            subject="Rate quote: 6 pallets reefer",
            body="Pickup Toronto. Delivery Belleville. 6 pallets.")
        conv.write({
            "ai_status": "ready",
            "ai_extraction": {
                "fields": {
                    "pickup": {"city": "Toronto", "postal_code": "M5V3E1"},
                    "delivery": {"city": "Belleville",
                                 "postal_code": "K8N2S1"},
                    "pallets": 6, "weight_lbs": 4200, "equipment": "Reefer",
                },
                "sources": {
                    "pallets": {"source_msg": 1, "provenance": "extracted"},
                    "equipment": {"source_msg": 1, "provenance": "extracted"},
                },
                "missing": ["pallets"], "conflicting": [],
            },
        })
        return conv

    def test_manual_override_merges_with_manual_provenance(self):
        conv = self._conv_with_extraction()
        res = conv.action_update_extraction(
            {"pallets": 8, "equipment": "Dry Van"})
        self.assertIn("edited", res)
        self.assertEqual(sorted(res["edited"]), ["equipment", "pallets"])
        fields_ = conv.ai_extraction["fields"]
        self.assertEqual(fields_["pallets"], 8)
        self.assertEqual(fields_["equipment"], "Dry Van")
        # untouched stops survive untouched
        self.assertEqual(conv.ai_extraction["fields"]["pickup"]["city"],
                         "Toronto")
        # manual provenance stamps source_msg None — the AI source citation
        # no longer applies to a dispatcher-typed value
        self.assertEqual(conv.ai_extraction["sources"]["pallets"],
                         {"source_msg": None, "provenance": "manual"})
        # overridden field leaves the missing list
        self.assertNotIn("pallets", conv.ai_extraction["missing"])

    def test_invalid_or_unknown_fields_rejected_atomically(self):
        conv = self._conv_with_extraction()
        before = conv.ai_extraction
        for bad in ({"pallets": "abc"}, {"pallets": 2.5},
                    {"horse": 12}, {"weight_lbs": "heavy"}):
            res = conv.action_update_extraction(bad)
            self.assertIn("error", res)
            self.assertIn("Invalid extraction field", res["error"])
            # nothing was written — one bad key rejects the whole call
            self.assertEqual(conv.ai_extraction, before)
        # a valid integer still passes (schema type check, not a cast)
        self.assertNotIn("error",
                         conv.action_update_extraction({"pallets": 10}))

    def test_nested_stop_keys_skipped_and_noop_errors(self):
        conv = self._conv_with_extraction()
        # the frontend only sends flat top-level keys today — nested stop
        # edits are skipped silently, so a call with ONLY nested keys is a
        # no-op error, never a partial/confusing write
        res = conv.action_update_extraction({"pickup.city": "Mississauga"})
        self.assertIn("error", res)
        self.assertIn("No valid fields", res["error"])
        self.assertEqual(conv.ai_extraction["fields"]["pickup"]["city"],
                         "Toronto")
        res = conv.action_update_extraction({})
        self.assertIn("No updates provided", res["error"])

    def test_update_never_touches_price_snapshot(self):
        """D-9 is a correction step — the dispatcher re-runs calculate after
        fixing fields; updating extraction must not re-price or mutate the
        snapshot (that authority lives in prema.inbox.pricing only)."""
        conv = self._conv_with_extraction()
        snapshot = {"reason": "fsa_unresolved",
                    "reason_text": "Pricing unavailable — pickup location: "
                                   "the postal code / FSA could not be "
                                   "resolved to a serviceable region.",
                    "calculated_at": "2026-09-07T12:00:00"}
        conv.write({"price_snapshot": snapshot})
        conv.action_update_extraction({"pallets": 8})
        self.assertEqual(conv.price_snapshot, snapshot)
        # and no engine price was invented on the side
        self.assertEqual(conv.engine_calculated_price, 0.0)


class TestPricingStateD10(InboxTestCase):
    """pricing_state / price_breakdown derive from the persisted snapshot —
    the panel chip + breakdown never invent a number."""

    def _fresh(self, subject="Rate quote: 6 pallets"):
        _, conv, _ = self.ingest(subject=subject,
                                 body="Pickup Toronto. Delivery Belleville.")
        return conv

    def test_not_priced_state_and_payload(self):
        conv = self._fresh()
        state = self.env["prema.inbox.pricing"].pricing_state(conv)
        self.assertEqual(state, {"state": "NOT_PRICED", "label": "Not priced"})
        self.assertEqual(
            self.env["prema.inbox.pricing"].price_breakdown(conv), [])
        detail = conv.inbox_conversation_detail(conv.id)
        self.assertEqual(detail["pricing"]["state"]["state"], "NOT_PRICED")
        self.assertEqual(detail["pricing"]["breakdown"], [])
        quote = detail["pricing"]["quote"]
        self.assertIsNone(quote["engine_calculated_price"])
        self.assertIsNone(quote["final_quoted_price"])
        self.assertTrue(quote["currency"])

    def test_fsa_unresolved_persists_needs_information(self):
        conv = self._fresh(subject="Hello there")
        # extraction ready but neither stop carries a resolvable postal code
        _seed_extraction(conv, pickup={}, delivery={})
        result = conv.inbox_calculate_price()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "fsa_unresolved")
        self.assertTrue(result["snapshot_saved"])
        self.assertEqual(conv.price_snapshot["reason"], "fsa_unresolved")
        self.assertIn("Pricing unavailable", conv.price_snapshot["reason_text"])
        state = self.env["prema.inbox.pricing"].pricing_state(conv)
        self.assertEqual(state["state"], "NEEDS_INFORMATION")
        # human, side-aware explanation persisted — the UI warn branch
        # renders from the stored snapshot, not from a toast
        self.assertIn("pickup location", conv.price_snapshot["reason_text"])
        self.assertIn("delivery location", conv.price_snapshot["reason_text"])
        self.assertEqual(
            self.env["prema.inbox.pricing"].price_breakdown(conv), [])

    def test_engine_exception_stays_ephemeral(self):
        from odoo.addons.prema_logistics_booking.services.pricing_service import (
            PricingService)
        conv = self._fresh()
        _seed_extraction(conv)
        with mock.patch.object(PricingService, "calculate",
                               side_effect=Exception("boom")):
            result = conv.inbox_calculate_price()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "engine_unavailable")
        self.assertIn("Pricing engine unavailable", result["reason_text"])
        self.assertFalse(result["snapshot_saved"])
        # transient — never persisted, a retry may succeed
        self.assertFalse(conv.price_snapshot)
        # defensive label branch: a snapshot carrying the code (older
        # persisted rows) still maps to the ENGINE_UNAVAILABLE chip
        conv.write({"price_snapshot": {"reason": "engine_unavailable"}})
        state = self.env["prema.inbox.pricing"].pricing_state(conv)
        self.assertEqual(state["state"], "ENGINE_UNAVAILABLE")

    def test_ready_state_breakdown_and_detail_payload(self):
        from odoo.addons.prema_logistics_booking.services.pricing_service import (
            PricingService)
        conv = self._fresh()
        _seed_extraction(conv)
        with mock.patch.object(PricingService, "calculate",
                               return_value=_fake_engine()):
            result = conv.inbox_calculate_price()
        self.assertTrue(result["available"])
        self.assertTrue(conv.price_snapshot)
        self.assertEqual(conv.price_snapshot["calculated_price"], 223.59)
        # engine price copied to its own field — visible even after a
        # dispatcher adjustment (the snapshot itself is never touched)
        self.assertEqual(conv.engine_calculated_price, 223.59)
        state = self.env["prema.inbox.pricing"].pricing_state(conv)
        self.assertEqual(state["state"], "READY")
        pricing = self.env["prema.inbox.pricing"]
        breakdown = pricing.price_breakdown(conv)
        self.assertEqual(len(breakdown), 2)
        self.assertEqual(breakdown[0]["label"], "LTL linehaul")
        self.assertEqual(breakdown[0]["amount"], 203.59)
        self.assertNotIn("kind", breakdown[0])
        detail = conv.inbox_conversation_detail(conv.id)
        self.assertEqual(detail["pricing"]["state"]["state"], "READY")
        self.assertEqual(detail["pricing"]["breakdown"], breakdown)
        quote = detail["pricing"]["quote"]
        self.assertEqual(quote["engine_calculated_price"], 223.59)
        self.assertEqual(quote["final_quoted_price"], 223.59)
        self.assertEqual(quote["currency"], conv.price_snapshot["currency"])
        self.assertEqual(quote["currency"],
                         self.env.company.currency_id.name)
        self.assertEqual(quote["quoted_by"], None)

    def test_partial_estimate_manual_review(self):
        from odoo.addons.prema_logistics_booking.services.pricing_service import (
            PricingService)
        conv = self._fresh()
        _seed_extraction(conv)
        fake = _fake_engine(available=False, calculated_price=0.0,
                            price_lines=[],
                            reason="pickup_fsa_not_supported",
                            manual_review_required=True)
        with mock.patch.object(PricingService, "calculate",
                               return_value=fake):
            result = conv.inbox_calculate_price()
        self.assertFalse(result["available"])
        self.assertTrue(result["snapshot_saved"])
        # a deterministic engine verdict (not an exception) IS persisted
        self.assertEqual(conv.price_snapshot["reason"],
                         "pickup_fsa_not_supported")
        state = self.env["prema.inbox.pricing"].pricing_state(conv)
        self.assertEqual(state["state"], "PARTIAL_ESTIMATE")
        self.assertEqual(state["label"], "Partial estimate")
        # no number was invented — manual-review verdicts carry no price
        self.assertEqual(
            self.env["prema.inbox.pricing"].price_breakdown(conv), [])


class TestQuotedPriceF1(InboxTestCase):
    """Dispatcher adjustment stays SEPARATE from the engine price — the
    snapshot is never overwritten and recalculation cannot lose the quote."""

    def _engine_conv(self):
        _, conv, _ = self.ingest(
            subject="Rate quote: 6 pallets",
            body="Pickup Toronto. Delivery Belleville. 6 pallets.")
        conv.write({
            "price_snapshot": {"calculated_price": 200.0,
                               "currency": "CAD",
                               "price_lines": [{"label": "LTL linehaul",
                                                "amount": 200.0}]},
            "engine_calculated_price": 200.0,
        })
        return conv

    def test_refused_without_engine_price(self):
        _, conv, _ = self.ingest(subject="Hello", body="How are you?")
        res = conv.action_set_quoted_price(50.0, "goodwill")
        self.assertIn("error", res)
        self.assertIn("Review & calculate quote", res["error"])
        self.assertEqual(conv.dispatcher_adjustment, 0.0)
        self.assertFalse(conv.quoted_by)

    def test_adjustment_stored_separately_from_engine(self):
        conv = self._engine_conv()
        state = conv.action_set_quoted_price(25.0, "volume deal")
        # engine price untouched — the dispatcher number applies ON TOP
        self.assertEqual(conv.engine_calculated_price, 200.0)
        self.assertEqual(conv.price_snapshot["calculated_price"], 200.0)
        self.assertEqual(conv.dispatcher_adjustment, 25.0)
        self.assertEqual(conv.adjustment_reason, "volume deal")
        self.assertEqual(conv.quoted_by.id, self.env.user.id)
        self.assertTrue(conv.quoted_at)
        self.assertEqual(conv.final_quoted_price, 225.0)
        self.assertEqual(state["final_quoted_price"], 225.0)
        self.assertEqual(state["dispatcher_adjustment"], 25.0)
        self.assertEqual(state["engine_calculated_price"], 200.0)
        self.assertEqual(state["currency"], "CAD")
        # breakdown exposes the adjustment line with its kind for the UI
        breakdown = self.env["prema.inbox.pricing"].price_breakdown(conv)
        self.assertEqual(breakdown[-1], {"label": "Dispatcher adjustment",
                                         "amount": 25.0, "kind": "adjustment"})

    def test_clearing_adjustment_returns_to_engine_price(self):
        """Adjustment=0 clears the dispatcher number; the engine price
        (copied from the snapshot when the quote is set) survives — the
        quote reverts to the engine number, nothing is lost."""
        _, conv, _ = self.ingest(
            subject="Rate quote: 6 pallets",
            body="Pickup Toronto. Delivery Belleville. 6 pallets.")
        # engine price exists ONLY in the snapshot — action_set_quoted_price
        # must copy it to engine_calculated_price before applying
        conv.write({"price_snapshot": {"calculated_price": 200.0,
                                       "currency": "CAD"}})
        conv.action_set_quoted_price(15.0, "adjusting from snapshot only")
        self.assertEqual(conv.engine_calculated_price, 200.0)
        self.assertEqual(conv.final_quoted_price, 215.0)
        state = conv.action_set_quoted_price(0)
        self.assertEqual(conv.dispatcher_adjustment, 0.0)
        self.assertFalse(conv.adjustment_reason)
        self.assertEqual(conv.final_quoted_price, 200.0)
        self.assertEqual(state["final_quoted_price"], 200.0)
        self.assertIsNone(state["dispatcher_adjustment"])
        self.assertEqual(state["engine_calculated_price"], 200.0)


class TestQuoteReplyF2(InboxTestCase):
    """Reply with Quote builds a deterministic template from records + the
    snapshot — never AI-invented, never auto-sent (the frontend opens the
    composer for the dispatcher to edit and send)."""

    def _quoted_conv(self):
        _, conv, _ = self.ingest(
            subject="Rate quote: 6 pallets reefer",
            body="Pickup Toronto. Delivery Belleville. 6 pallets reefer 3C.")
        _seed_extraction(conv)
        conv.write({
            "engine_calculated_price": 200.0,
            "dispatcher_adjustment": 25.0,
            "adjustment_reason": "volume deal",
            "price_snapshot": {"calculated_price": 200.0, "currency": "CAD",
                               "price_lines": [{"label": "LTL linehaul",
                                                "amount": 200.0}]},
        })
        return conv

    def test_requires_confirmed_partner_first(self):
        _, conv, _ = self.ingest(subject="Hello", body="How are you?")
        conv.write({"partner_id": False})
        res = conv.action_quote_reply()
        self.assertIn("error", res)
        self.assertIn("Confirm the customer first", res["error"])

    def test_requires_engine_price(self):
        _, conv, _ = self.ingest(subject="Hello", body="How are you?")
        res = conv.action_quote_reply()
        self.assertIn("error", res)
        self.assertIn("Review & calculate quote", res["error"])

    def test_deterministic_body_from_records_only(self):
        conv = self._quoted_conv()
        res = conv.action_quote_reply()
        self.assertNotIn("error", res)
        self.assertTrue(res["subject"].startswith("Re: "))
        body = res["body"]
        self.assertIn("Shipment: 6 pallets, 4200 lbs, Reefer, 3 C", body)
        self.assertIn("Pickup:   Toronto, ON, M5V3E1", body)
        self.assertIn("Delivery: Belleville, ON, K8N2S1", body)
        self.assertIn("- LTL linehaul: CAD 200.00", body)
        self.assertIn("- Dispatcher adjustment: CAD 25.00", body)
        self.assertIn("Total quoted price: CAD 225.00", body)
        # the returned quote block is the same one the UI renders
        self.assertEqual(res["quote"]["final_quoted_price"], 225.0)
        # nothing was sent, nothing was written — a reply is composed,
        # never dispatched, by this action
        self.assertEqual(
            len(conv.inbox_message_ids.filtered(
                lambda m: m.direction == "outgoing")), 0)
        self.assertEqual(res["quote"]["adjustment_reason"], "volume deal")


class TestCreateBookingFromEmailF3(InboxTestCase):
    """F-3 — the dispatcher's EXPLICIT click confirms the quoted email as a
    logistics.booking. The booking engine (BookingOrchestrationService) is
    faked exactly like the custom-quote lifecycle suite — these tests assert
    the inbox gates and the request contract, never the engine itself.

    The sudo envelope is part of the contract: inbox users are read-only on
    logistics models, so the service call must ride self.env.sudo() (no ACL
    CSV change) — test 8 pins that with a real inbox-group user.
    """

    def _quoted_conv(self):
        """Confirmed partner + resolvable extraction + a quoted price."""
        _, conv, _ = self.ingest(
            subject="Rate quote: 6 pallets reefer",
            body="Pickup Toronto. Delivery Belleville. 6 pallets.")
        _seed_extraction(conv)
        conv.write({
            "engine_calculated_price": 200.0,
            "price_snapshot": {"calculated_price": 200.0, "currency": "CAD",
                               "price_lines": [{"label": "LTL linehaul",
                                                "amount": 200.0}]},
        })
        return conv

    def _mk_booking(self, env, partner):
        """Minimal booking row exactly like the orchestration service does
        (booking_number is readonly-after-create, so set at create)."""
        return env["logistics.booking"].create({
            "partner_id": partner.id,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 6, "physical_pallets": 6, "weight_lbs": 4200.0,
            "pickup_date": datetime.date(2026, 9, 9),
            "estimated_delivery_date": datetime.date(2026, 9, 9),
            "price_snapshot": [{"line": "F3 email booking test"}],
            "booking_number":
                env["logistics.booking"]._generate_booking_number(),
        })

    @contextmanager
    def _fake_orchestration(self, captured, counter=None):
        """Replace BookingOrchestrationService so the booking engine never
        runs (no geocoding, no pricing) — asserts the F-3 gates and the
        request contract, not the engine. Yields while both patches are
        active (a plain tuple return does not enter with `with`, hence
        @contextmanager)."""
        from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (  # noqa: E501
            BookingOrchestrationService)
        mk_booking = self._mk_booking

        def fake_normalize(self, request, **kwargs):
            captured["request"] = request
            captured["channel"] = kwargs.get("source_channel")
            return {"request": request}

        def fake_confirm(self, norm, **kwargs):
            if counter is not None:
                counter["calls"] += 1
            captured["confirm"] = kwargs
            partner = self.env["res.partner"].browse(
                norm["request"]["partner_id"])
            return mk_booking(self.env, partner)

        with mock.patch.object(
                BookingOrchestrationService, "normalize_request",
                fake_normalize), mock.patch.object(
                BookingOrchestrationService, "confirm_from_internal",
                fake_confirm):
            yield

    def test_trashed_conversation_refused(self):
        conv = self._quoted_conv()
        conv.write({"trashed": True, "trashed_at": fields.Datetime.now()})
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            with self.assertRaisesRegex(ValidationError, "Trash"):
                conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 0)
        self.assertFalse(conv.booking_id)

    def test_provisional_partner_refused(self):
        conv = self._quoted_conv()
        conv.write({"partner_provisional": True,
                    "partner_suggestions": [{"id": conv.partner_id.id}]})
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            with self.assertRaisesRegex(ValidationError,
                                        "Confirm the customer first"):
                conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 0)
        self.assertFalse(conv.booking_id)

    def test_requires_final_quoted_price(self):
        """Never book at an invented number — no engine price, no booking."""
        _, conv, _ = self.ingest(subject="Hello", body="How are you?")
        _seed_extraction(conv)
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            with self.assertRaisesRegex(ValidationError,
                                        "final quoted price"):
                conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 0)

    def test_requires_resolvable_fsas(self):
        conv = self._quoted_conv()
        # valid postal SHAPE, but no logistics.fsa row serves it
        _seed_extraction(conv, delivery={"postal_code": "Z9Z9Z9"})
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            with self.assertRaisesRegex(ValidationError,
                                        "delivery postal code"):
                conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 0)
        self.assertFalse(conv.booking_id)

    def test_builds_email_booking_request_and_payload(self):
        conv = self._quoted_conv()
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            res = conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 1)
        # --- normalize_request contract ------------------------------
        self.assertEqual(captured["channel"], "email")
        req = captured["request"]
        self.assertEqual(req["partner_id"], conv.partner_id.id)
        self.assertEqual(len(req["pickup_stops"]), 1)
        self.assertEqual(len(req["delivery_stops"]), 1)
        pu, dl = req["pickup_stops"][0], req["delivery_stops"][0]
        self.assertEqual(pu["postal_code"], "M5V3E1")
        self.assertEqual(pu["city"], "Toronto")
        self.assertEqual(pu["province"], "ON")
        self.assertEqual(pu["formatted_address"], "Toronto, ON, M5V3E1")
        self.assertEqual(dl["postal_code"], "K8N2S1")
        self.assertEqual(dl["city"], "Belleville")
        self.assertEqual(req["pallets"], 6)
        self.assertEqual(req["weight_lbs"], 4200)
        self.assertEqual(req["load_type"], "ltl")
        # extraction "Reefer" → canonical temperature mode + reefer temp
        self.assertEqual(req["equipment_type"], "reefer")
        self.assertEqual(req["required_temperature_c"], 3)
        self.assertTrue(req["liftgate_pickup"])  # accessorials contain it
        # corridor re-pricing at confirm (resolve_departures) — the §8.7
        # capacity re-check happens NOW, not at quote time
        self.assertEqual(req["pricing_method"], "corridor")
        self.assertEqual(req["agreed_rate"], 200.0)
        self.assertEqual(req["idempotency_key"], "email:%s" % conv.id)
        self.assertEqual(req["source_model"], "prema.inbox.conversation")
        self.assertEqual(req["source_res_id"], conv.id)
        # --- confirm_from_internal contract --------------------------
        conf = captured["confirm"]
        self.assertTrue(conf["skip_invoice"])
        self.assertEqual(conf["sell_price_override"], 200.0)
        self.assertIn(conv.name, conf["sell_price_override_reason"])
        # --- result + conversation state -----------------------------
        self.assertTrue(res["booking_id"])
        booking = conv.booking_id
        self.assertEqual(res["booking_id"], booking.id)
        self.assertEqual(res["number"], booking.booking_number)
        self.assertEqual(res["state"], booking.state)
        self.assertIn("logistics.booking&id=%s" % booking.id, res["url"])
        # audit note on the thread + backlink note on the booking
        self.assertTrue(self.env["mail.message"].search_count([
            ("model", "=", "prema.inbox.conversation"),
            ("res_id", "=", conv.id),
            ("body", "like", "%created from this email request%")]))
        self.assertTrue(self.env["mail.message"].search_count([
            ("model", "=", "logistics.booking"),
            ("res_id", "=", booking.id),
            ("body", "like", "%Dispatch Inbox conversation linked%")]))

    def test_second_call_is_idempotent(self):
        conv = self._quoted_conv()
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            first = conv.action_create_booking_from_email()
            second = conv.action_create_booking_from_email()
        # ONE service call, ONE booking — the booking_id guard returns the
        # same payload (double-click / RPC retry safe) and never re-runs
        # capacity/pricing
        self.assertEqual(counter["calls"], 1)
        self.assertEqual(first["booking_id"], second["booking_id"])
        self.assertEqual(conv.booking_id.id, first["booking_id"])
        self.assertEqual(conv.booking_id.id, second["booking_id"])

    def test_converted_custom_quote_shortcut_skips_service(self):
        """The Rate Confirmation already became a booking — F-3 links the
        thread to it with ZERO engine calls (no second booking, no re-run
        of capacity/pricing)."""
        partner = self.env["res.partner"].create({
            "name": "F3 Shortcut Produce", "is_company": True,
            "email": "shortcut@demo-toronto-produce.test"})
        corridor = self.env["logistics.corridor"].create({
            "name": "F3 Shortcut Corridor", "equipment_type": "dry"})
        departure = self.env["logistics.corridor.departure"].create({
            "corridor_id": corridor.id,
            "departure_date": datetime.date(2026, 9, 15)})
        cq = self.env["logistics.custom.quote"].create({
            "partner_id": partner.id,
            "source": "internal",
            "contact_name": partner.name,
            "contact_email": partner.email,
            "pickup_postal_code": "M5V 3E1",
            "pickup_address": "300 Progress Ave, Toronto, ON M5V3E1",
            "delivery_postal_code": "K8N 2S1",
            "delivery_address": "55 Station St, Belleville, ON K8N2S1",
            "pallets": 4, "weight_lbs": 2000.0,
            "temperature_mode": "dry", "load_type": "ltl",
            "commodity": "Shortcut widgets",
            "system_calculated_price": 850.0,
            "quoted_price": 850.0,
            "departure_id": departure.id,
            "state": "converted",
        })
        booking = self._mk_booking(self.env, partner)
        cq.write({"booking_id": booking.id})
        _, conv, _ = self.ingest(
            email_from="Sender Shortcut <sender@shortcut-produce.test>",
            subject="Rate quote", body="Pickup Toronto. Delivery Belleville.")
        conv.write({"custom_quote_id": cq.id})
        captured, counter = {}, {"calls": 0}
        with self._fake_orchestration(captured, counter):
            res = conv.action_create_booking_from_email()
        self.assertEqual(counter["calls"], 0)
        self.assertEqual(conv.booking_id.id, booking.id)
        self.assertEqual(res["booking_id"], booking.id)

    def test_sudo_envelope_runs_as_inbox_group_user(self):
        """An inbox-group dispatcher (read-only on logistics models) can
        create the booking — the service rides self.env.sudo() inside the
        action, so no ACL CSV change is needed."""
        user = self.make_user(login="ops.f3.booking")
        conv = self._quoted_conv()
        captured, counter = {}, {"calls": 0}
        env = self.env(user=user.id)
        with self._fake_orchestration(captured, counter):
            res = conv.with_env(env).action_create_booking_from_email()
        self.assertEqual(counter["calls"], 1)
        self.assertTrue(res["booking_id"])
        self.assertEqual(conv.booking_id.id, res["booking_id"])

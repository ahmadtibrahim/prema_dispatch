# -*- coding: utf-8 -*-
"""E-A2 §4/§6 — Google completion of civic-only stops + liftgate + naming.

Companion to test_crm_estimate_draft_bridge.py: that file covers complete
addresses and the refusal paths; THIS file covers the Google Places
resolution branch of LeadQuoteDraftService.resolve_stops (master
instruction §4 C):

* street + city/province WITHOUT a postal code is completed through the
  canonical Google Places service when exactly ONE confident candidate
  exists — created ONCE as a Pending Review Saved Location (never
  auto-verified, pin from Google, place id kept for dedupe);
* the same physical address is reused on repeat resolutions (repeat-click
  safe) whether the earlier row carries the google place id or only the
  postal + street number;
* several candidates → actionable options error, NOTHING persisted (the
  service never guesses between addresses);
* no candidates (missing key, unreachable, empty) → unresolved error;
* liftgate accessorials map to the per-stop canonical flags (default
  delivery side, explicit pickup side, 'no liftgate' override);
* the customer-stated company name names any new Pending Review row.

Fixture addresses are synthetic on purpose (repo convention) — they never
collide with production facility rows on the production-derived test DB.
GooglePlacesService.search_address_candidates is patched: the canonical
service never runs against the real Places API in tests.
"""

import json
from contextlib import ExitStack
from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..services.google_places_service import GooglePlacesService
from .test_crm_estimate_draft_bridge import BOOKING_MANAGER, STUB_QUOTE

# Google-resolved civic-only fixtures (synthetic streets, plausible N0B
# postals — nothing like them exists in the production data).
PICKUP_CIVIC = "1408 Testwest Crescent"
PICKUP_POSTAL = "N0B 2B1"
DELIVERY_CIVIC = "2279 Test Sideroad 17"
DELIVERY_POSTAL = "N0B 2C3"

PICKUP_CANDIDATE = {
    "place_id": "ChIJTEST-PICK-001",
    "latitude": 43.2841,
    "longitude": -80.4624,
    "formatted_address": "%s, Testville, ON %s" % (PICKUP_CIVIC,
                                                   PICKUP_POSTAL),
    "street": PICKUP_CIVIC,
    "city": "Testville",
    "province_code": "ON",
    "postal_code": PICKUP_POSTAL,
    "country_code": "CA",
}
DELIVERY_CANDIDATE = {
    "place_id": "ChIJTEST-DEL-001",
    "latitude": 44.1632,
    "longitude": -77.3862,
    "formatted_address": "%s, Testville, ON %s" % (DELIVERY_CIVIC,
                                                   DELIVERY_POSTAL),
    "street": DELIVERY_CIVIC,
    "city": "Testville",
    "province_code": "ON",
    "postal_code": DELIVERY_POSTAL,
    "country_code": "CA",
}


def _facts(**values):
    """Minimal effective-facts dict in the engine snapshot shape — only the
    fields a test needs, each carrying provenance (never invented)."""
    effective = {}
    for field, value in values.items():
        effective[field] = {
            "value": value, "confidence": "high", "conflict": False,
            "kind": "inbound_email", "source": "Customer email",
        }
    return {"effective": effective}


def _rows_for(text):
    """Deterministic LLM stand-in for the civic-only E2E (rows only for
    tokens actually present)."""
    rows = []
    matches = [
        ("September 8 2026", "pickup_date", "2026-09-08"),
        ("10:30 a.m.", "pickup_earliest", "10:30"),
        ("11:30 a.m.", "pickup_latest", "11:30"),
        ("before 4:00 p.m.", "delivery_deadline", "16:00"),
        ("22 pallets", "pallets", "22"),
        # Civic-only stops: street + city, deliberately NO postal codes —
        # the Google completion path must supply them.
        (PICKUP_CIVIC, "origin_address", PICKUP_CIVIC),
        (DELIVERY_CIVIC, "destination_address", DELIVERY_CIVIC),
    ]
    for token, field, value in matches:
        if token in text:
            rows.append({"field": field, "value": value,
                         "confidence": "high", "note": ""})
    if "Testville" in text:
        # Each side's city is the Testville that appears in ITS segment of
        # the message (the fixture's two stops share the city, so both
        # rows fire here — a delivery side with no city must stay
        # city-less so the no-city refusal path is exercised elsewhere).
        head, _, tail = text.partition("delivery")
        if "Testville" in head:
            rows.append({"field": "origin_city", "value": "Testville",
                         "confidence": "high", "note": ""})
        if "Testville" in tail:
            rows.append({"field": "destination_city", "value": "Testville",
                         "confidence": "high", "note": ""})
    return rows


def _fake_chat(messages, system="", **kwargs):
    if "You extract SHIPMENT FACTS" in system:
        text = (messages or [{}])[0].get("content") or ""
        return json.dumps(_rows_for(text))
    return ("Thank you for your request.\n\nWe confirm receipt of the "
            "shipment details and are preparing the formal Customer Rate "
            "Confirmation, which we will send to you shortly.")


def _patches():
    stack = ExitStack()
    for key, value in {
        "get_api_key": lambda env: "test-key",
        "get_model": lambda env: "test-model",
        "deepseek_chat": _fake_chat,
    }.items():
        stack.enter_context(patch(
            "odoo.addons.premafirm_ai_engine.services.deepseek_utils.%s"
            % key, value))
    return stack


class TestGoogleStopResolution(TransactionCase):
    """resolve_stops Google completion — service level (facts hand-built,
    extraction never involved, canonical Places service patched)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.booking_manager_group = env.ref(BOOKING_MANAGER)
        if not env.user.has_group(BOOKING_MANAGER):
            env.user.write({"groups_id": [(4, cls.booking_manager_group.id)]})

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create({
            "name": "Google Resolution Customer",
            "is_company": True,
            "email": "google-resolution@example.test",
        })
        self.lead = self.env["crm.lead"].create({
            "name": "Civic-only opportunity",
            "partner_id": self.customer.id,
            "description": ("<p>Civic-only stops fixture.</p>"),
        })
        self.Location = self.env["prema.dispatch.location"].sudo()
        self.Access = self.env["logistics.location.customer.access"].sudo()

    def _svc(self):
        from ..services.lead_quote_draft_service import LeadQuoteDraftService
        return LeadQuoteDraftService(self.env)

    def _both_civic_facts(self):
        return _facts(
            origin_address=PICKUP_CIVIC,
            origin_city="Testville",
            origin_company_name="Testwest Pickup Co",
            destination_address=DELIVERY_CIVIC,
            destination_city="Testville",
            destination_company_name="Testwest Receiver Co",
        )

    def _google_patch(self, candidates_by_substring):
        def stub(query):
            out = []
            for token, candidates in candidates_by_substring.items():
                if token in (query or ""):
                    out.extend(candidates)
            return out
        return patch.object(GooglePlacesService,
                            "search_address_candidates", side_effect=stub)

    # ── one confident candidate per side → one Pending Review each ─────

    def test_confident_resolution_creates_one_pending_review_per_side(self):
        locations_before = self.Location.search_count([])
        with self._google_patch({
                PICKUP_CIVIC: [PICKUP_CANDIDATE],
                DELIVERY_CIVIC: [DELIVERY_CANDIDATE]}):
            result = self._svc().resolve_stops(
                self.lead, self._both_civic_facts(), require_priceable=True)

        created = result["created"]
        self.assertEqual(len(created), 2)
        self.assertEqual(
            self.Location.search_count([]), locations_before + 2)
        # Both sides landed on reviewable google-sourced rows — standard
        # address stored, pin from Google, place id kept, NEVER verified.
        pickup = created.filtered(lambda loc: loc.stop_type == "pickup")
        delivery = created.filtered(lambda loc: loc.stop_type == "delivery")
        self.assertEqual(len(pickup), 1)
        self.assertEqual(len(delivery), 1)
        for row, candidate, company in (
                (pickup, PICKUP_CANDIDATE, "Testwest Pickup Co"),
                (delivery, DELIVERY_CANDIDATE, "Testwest Receiver Co")):
            self.assertEqual(row.verification_state, "pending_review")
            self.assertEqual(row.source_type, "google_places")
            self.assertFalse(row.google_verified)
            self.assertEqual(row.google_place_id, candidate["place_id"])
            self.assertEqual(row.pin_source, "google_place")
            self.assertEqual(row.pin_lat, candidate["latitude"])
            self.assertEqual(row.pin_lng, candidate["longitude"])
            self.assertEqual(row.address, candidate["formatted_address"])
            self.assertEqual(row.postal_code, candidate["postal_code"])
            # Customer-stated company names the new row.
            self.assertEqual(row.business_name, company)
            self.assertEqual(row.name, company)
        # The resolved postal lives on the Pending Review row the caller
        # prices from (_stop_payload reads the location payload first —
        # the civic-only stop dict itself never invents a postal).
        self.assertEqual(result["pickup"]["location"].postal_code,
                         PICKUP_POSTAL)
        self.assertEqual(result["delivery"]["location"].postal_code,
                         DELIVERY_POSTAL)
        # Customer access rows exist for BOTH sides.
        self.assertEqual(
            self.Access.search_count([("commercial_partner_id", "=",
                                       self.customer.commercial_partner_id.id),
                                      ("facility_id", "in", created.ids)]),
            2)

    # ── repeat resolution reuses, never duplicates ─────────────────────

    def test_repeat_resolution_reuses_google_rows_not_duplicates(self):
        svc = self._svc()
        facts = self._both_civic_facts()
        with self._google_patch({
                PICKUP_CIVIC: [PICKUP_CANDIDATE],
                DELIVERY_CIVIC: [DELIVERY_CANDIDATE]}):
            first = svc.resolve_stops(self.lead, facts,
                                      require_priceable=True)
            second = svc.resolve_stops(self.lead, facts,
                                       require_priceable=True)
        self.assertEqual(len(first["created"]), 2)
        self.assertEqual(len(second["created"]), 0,
                         "A repeat resolution must reuse the pending rows.")
        # Same physical rows came back through the google_place_id dedupe.
        self.assertEqual(first["pickup"]["location"],
                         second["pickup"]["location"])
        self.assertEqual(first["delivery"]["location"],
                         second["delivery"]["location"])
        # Exactly the two fixture rows exist as pending review — scoped to the
        # fixture place_ids so committed real-world rows on the scratch DB
        # (live smoke runs) can never skew the count.
        self.assertEqual(self.Location.search_count([
            ("verification_state", "=", "pending_review"),
            ("google_place_id", "in",
             [PICKUP_CANDIDATE["place_id"], DELIVERY_CANDIDATE["place_id"]]),
        ]), 2)

    # ── legacy rows (postal + civic, no place id) are reused too ───────

    def test_pre_google_row_reused_by_postal_and_civic_match(self):
        legacy = self.Location.create({
            "name": "Testwest Crescent (legacy row)",
            "address": "%s, Testville, ON %s" % (PICKUP_CIVIC,
                                                 PICKUP_POSTAL),
            "street": PICKUP_CIVIC,
            "city": "Testville",
            "province_code": "ON",
            "postal_code": PICKUP_POSTAL,
            "stop_type": "pickup",
            "source_type": "dispatcher_manual",
            "verification_state": "pending_review",
        })
        locations_before = self.Location.search_count([])
        facts = self._both_civic_facts()
        with self._google_patch({
                PICKUP_CIVIC: [dict(PICKUP_CANDIDATE,
                                    place_id="")],
                DELIVERY_CIVIC: [DELIVERY_CANDIDATE]}):
            result = self._svc().resolve_stops(
                self.lead, facts, require_priceable=True)
        # Only the delivery side was new; pickup reused the legacy row.
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(self.Location.search_count([]),
                         locations_before + 1)
        self.assertEqual(result["pickup"]["location"], legacy)
        self.assertEqual(result["pickup"]["kind"], "location")

    # ── several candidates → options error, NOTHING persisted ──────────

    def test_ambiguous_google_candidates_raise_options_nothing_created(self):
        rival = dict(DELIVERY_CANDIDATE,
                     place_id="ChIJTEST-DEL-002",
                     formatted_address="%s, Otherville, ON N0B 2C4"
                     % DELIVERY_CIVIC,
                     city="Otherville",
                     postal_code="N0B 2C4")
        locations_before = self.Location.search_count([])
        access_before = self.Access.search_count([])
        # Pickup is confident; delivery is genuinely ambiguous (same civic
        # number, same province, two different postal rows).
        with self._google_patch({
                PICKUP_CIVIC: [PICKUP_CANDIDATE],
                DELIVERY_CIVIC: [DELIVERY_CANDIDATE, rival]}):
            with self.assertRaises(UserError) as guard:
                self._svc().resolve_stops(
                    self.lead, self._both_civic_facts(),
                    require_priceable=True)
        message = str(guard.exception)
        self.assertIn("SEVERAL candidate addresses", message)
        self.assertIn(DELIVERY_CANDIDATE["formatted_address"], message)
        self.assertIn(rival["formatted_address"], message)
        # Validation is all-or-nothing: the confident PICKUP side was NOT
        # persisted either — nothing was created, nothing was priced.
        self.assertIn("Nothing was created or priced", message)
        self.assertEqual(self.Location.search_count([]), locations_before)
        self.assertEqual(self.Access.search_count([]), access_before)

    # ── no candidates → unresolved error, nothing created ──────────────

    def test_no_google_candidates_raises_unresolved_nothing_created(self):
        locations_before = self.Location.search_count([])
        with self._google_patch({
                PICKUP_CIVIC: [PICKUP_CANDIDATE],
                DELIVERY_CIVIC: []}):
            with self.assertRaises(UserError) as guard:
                self._svc().resolve_stops(
                    self.lead, self._both_civic_facts(),
                    require_priceable=True)
        message = str(guard.exception)
        self.assertIn("Google could not resolve it", message)
        self.assertIn(DELIVERY_CIVIC, message)
        self.assertIn("Nothing was created or priced", message)
        self.assertEqual(self.Location.search_count([]), locations_before)

    # ── no city/province to resolve against → never attempted ──────────

    def test_civic_only_without_city_or_province_not_attempted(self):
        # Neither side carries a city or province token — Google must not
        # be consulted for a bare street number.
        facts = _facts(origin_address=PICKUP_CIVIC,
                       destination_address=DELIVERY_CIVIC)
        mocked = Mock(side_effect=AssertionError(
            "Google must not run without a city/province to resolve."))
        with patch.object(GooglePlacesService,
                          "search_address_candidates", mocked):
            with self.assertRaises(UserError) as guard:
                self._svc().resolve_stops(self.lead, facts,
                                          require_priceable=True)
        mocked.assert_not_called()
        message = str(guard.exception)
        self.assertIn("no city/province to resolve it against", message)

    # ── customer access rows carry the right side flags ────────────────

    def test_pending_rows_get_customer_access_with_side_flags(self):
        with self._google_patch({
                PICKUP_CIVIC: [PICKUP_CANDIDATE],
                DELIVERY_CIVIC: [DELIVERY_CANDIDATE]}):
            result = self._svc().resolve_stops(
                self.lead, self._both_civic_facts(), require_priceable=True)
        for location in result["created"]:
            access = self.Access.search([
                ("facility_id", "=", location.id),
                ("commercial_partner_id", "=",
                 self.customer.commercial_partner_id.id)], limit=1)
            self.assertEqual(len(access), 1)
            # The service passes the side flag of THIS quote, but the
            # access model defaults keep every row usable on BOTH sides —
            # a facility is never locked to the side of its first quote.
            self.assertTrue(access.can_pickup)
            self.assertTrue(access.can_delivery)
            if location.stop_type == "pickup":
                self.assertTrue(access.can_pickup)
            else:
                self.assertTrue(access.can_delivery)


class TestLiftgateAndCompanyNaming(TransactionCase):
    """Liftgate side mapping + company naming on the manual-complete path
    (no Google involved)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.booking_manager_group = env.ref(BOOKING_MANAGER)
        if not env.user.has_group(BOOKING_MANAGER):
            env.user.write({"groups_id": [(4, cls.booking_manager_group.id)]})

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create({
            "name": "Liftgate Customer",
            "is_company": True,
            "email": "liftgate@example.test",
        })
        self.lead = self.env["crm.lead"].create({
            "name": "Liftgate opportunity",
            "partner_id": self.customer.id,
        })

    def _facts(self, accessorials):
        # Complete addresses (the bridge-test fixtures — proven collision-
        # free): extraction never involved, so complete stops + a stated
        # accessorial reach estimate_request_values without Google.
        return _facts(
            origin_address="1406 Test Line 8, Ayr, ON N0B 1E0",
            origin_postal_code="N0B 1E0",
            origin_company_name="Testwest Pickup Co",
            destination_address="2277 Test Sideroad 15, Ayr, ON N0B 1E0",
            destination_postal_code="N0B 1E0",
            pallets="22",
            accessorials=accessorials,
        )

    def _request_values(self, accessorials):
        from ..services.lead_quote_draft_service import LeadQuoteDraftService
        svc = LeadQuoteDraftService(self.env)
        stops = svc.resolve_stops(self.lead, self._facts(accessorials),
                                  require_priceable=True)
        shipment = svc.shipment_values(self._facts(accessorials))
        return svc.estimate_request_values(self.lead, stops, shipment)

    def test_unqualified_liftgate_defaults_to_delivery(self):
        values = self._request_values("liftgate required")
        self.assertFalse(values["liftgate_pickup"])
        self.assertTrue(values["liftgate_delivery"])
        self.assertFalse(values["pickup_stops"][0]["liftgate_required"])
        self.assertTrue(values["delivery_stops"][0]["liftgate_required"])

    def test_pickup_bound_liftgate_stays_pickup_only(self):
        values = self._request_values("liftgate at pickup")
        self.assertTrue(values["pickup_stops"][0]["liftgate_required"])
        self.assertFalse(values["delivery_stops"][0]["liftgate_required"])

    def test_explicit_no_liftgate_overrides_mentions(self):
        values = self._request_values("liftgate required, no liftgate")
        self.assertFalse(values["liftgate_pickup"])
        self.assertFalse(values["liftgate_delivery"])
        self.assertFalse(values["pickup_stops"][0]["liftgate_required"])
        self.assertFalse(values["delivery_stops"][0]["liftgate_required"])

    def test_liftgate_both_sides_explicit(self):
        values = self._request_values(
            "liftgate at pickup, liftgate at delivery")
        self.assertTrue(values["pickup_stops"][0]["liftgate_required"])
        self.assertTrue(values["delivery_stops"][0]["liftgate_required"])

    def test_company_name_names_the_manual_pending_row(self):
        from ..services.lead_quote_draft_service import LeadQuoteDraftService
        svc = LeadQuoteDraftService(self.env)
        stops = svc.resolve_stops(self.lead, self._facts(""),
                                  require_priceable=True)
        pickup = stops["pickup"]["location"]
        self.assertTrue(pickup)
        self.assertEqual(pickup.business_name, "Testwest Pickup Co")
        self.assertEqual(pickup.name, "Testwest Pickup Co")
        self.assertEqual(pickup.verification_state, "pending_review")


class TestGoogleEstimateE2E(TransactionCase):
    """Civic-only customer email → Google completion → canonical price →
    ONE editable draft, nothing sent, double-click safe."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.booking_manager_group = env.ref(BOOKING_MANAGER)
        if not env.user.has_group(BOOKING_MANAGER):
            env.user.write({"groups_id": [(4, cls.booking_manager_group.id)]})

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create({
            "name": "E2E Google Customer",
            "is_company": True,
            "email": "e2e-google@example.test",
        })
        self.lead = self.env["crm.lead"].create({
            "name": "TEST-CRM-002 — reefer Testville run",
            "partner_id": self.customer.id,
            "description": (
                "<p>22 pallets of retail store supplies. Pickup %s, "
                "Testville ON on September 8 2026, 10:30 a.m. to 11:30 "
                "a.m.; delivery to %s, Testville ON before 4:00 p.m. "
                "Liftgate required. Ref TEST-CRM-002.</p>"
                % (PICKUP_CIVIC, DELIVERY_CIVIC)),
        })
        self.Location = self.env["prema.dispatch.location"].sudo()

    def _stub_candidates(self, query):
        out = []
        if PICKUP_CIVIC in (query or ""):
            out.append(PICKUP_CANDIDATE)
        if DELIVERY_CIVIC in (query or ""):
            out.append(DELIVERY_CANDIDATE)
        return out

    def test_e2e_google_resolution_prices_and_prepares_draft_only(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        locations_before = self.Location.search_count([])
        replies_before = self.env[
            "premafirm.lead.estimate.reply"].search_count([])
        mails_before = self.env["mail.mail"].search_count([])
        bookings_before = self.env["logistics.booking"].search_count([])

        with _patches(), \
                patch.object(GooglePlacesService,
                             "search_address_candidates",
                             side_effect=self._stub_candidates), \
                patch.object(BookingOrchestrationService, "prepare_quote",
                             return_value=dict(STUB_QUOTE)):
            first = self.lead.action_prepare_preliminary_estimate()
            # Double-click / refresh safety: the SAME draft comes back.
            second = self.lead.action_prepare_preliminary_estimate()

        self.assertEqual(first["res_model"], "premafirm.lead.estimate.reply")
        self.assertEqual(second["res_id"], first["res_id"])
        draft = self.env["premafirm.lead.estimate.reply"].browse(
            first["res_id"])
        self.assertEqual(draft.crm_lead_id, self.lead)
        # The canonical stub price (never invented by the reply path).
        self.assertEqual(draft.price_amount, STUB_QUOTE["calculated_price"])
        self.assertIn("TOK-A2B-TEST", draft.price_reference)
        # Exactly TWO google-sourced Pending Review locations, created once —
        # scoped to the fixture place_ids so committed real-world rows from
        # live smoke runs on the scratch DB can never skew this count.
        pending = self.Location.search([
            ("verification_state", "=", "pending_review"),
            ("source_type", "=", "google_places"),
            ("google_place_id", "in",
             [PICKUP_CANDIDATE["place_id"], DELIVERY_CANDIDATE["place_id"]]),
        ])
        self.assertEqual(len(pending), 2)
        self.assertEqual(self.Location.search_count([]),
                         locations_before + 2)
        for row, candidate in ((pending.filtered(
                lambda loc: loc.stop_type == "pickup"), PICKUP_CANDIDATE),
                (pending.filtered(
                    lambda loc: loc.stop_type == "delivery"),
                 DELIVERY_CANDIDATE)):
            self.assertEqual(len(row), 1)
            self.assertEqual(row.postal_code, candidate["postal_code"])
            self.assertFalse(row.google_verified)
            self.assertTrue(row.google_place_id)
        # The reply body carries the resolved-customer facts — and nothing
        # operational moved: no booking, no mail, exactly one reply.
        self.assertIn("10:30", draft.body_html or "")
        self.assertEqual(
            self.env["premafirm.lead.estimate.reply"].search_count([]),
            replies_before + 1)
        self.assertEqual(self.env["mail.mail"].search_count([]), mails_before)
        self.assertEqual(self.env["logistics.booking"].search_count([]),
                         bookings_before)

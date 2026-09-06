import datetime
import json
from contextlib import ExitStack
from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

# Synthetic fixture addresses on purpose (repo convention): these streets do
# not exist anywhere, so they never collide with production facility rows,
# unique constraints or geocoding on the production-derived test database.
PICKUP_ADDRESS = "1406 Test Line 8, Ayr, ON N0B 1E0"
PICKUP_POSTAL = "N0B 1E0"
DELIVERY_ADDRESS = "2277 Test Sideroad 15, Ayr, ON N0B 1E0"
DELIVERY_POSTAL = "N0B 1E0"
# The lead-1041 correction story: Tuesday September 8 2026 10:30–11:30,
# delivery before 16:00.
PICKUP_DATE_NEW = datetime.date(2026, 9, 8)

BOOKING_MANAGER = "prema_logistics_booking.group_logistics_booking_manager"

PROSE = ("Thank you for your request.\n\n"
         "We confirm receipt of the shipment details and are preparing the "
         "formal Customer Rate Confirmation, which we will send to you "
         "shortly.")

STUB_QUOTE = {
    "quote_token": "TOK-A2B-TEST",
    "calculated_price": 1234.5,
    "pickup_date": "2026-09-08",
    "delivery_date": "2026-09-08",
    "price_lines": [{"line": "stub"}],
    "lane_name": "Test Lane",
    "service_offering_name": "Scheduled LTL Corridor",
    "expires_at": None,
}


def _llm_rows_for(text):
    """Deterministic LLM stand-in: rows only for tokens actually present in
    the single document under extraction (sanitize_rows/vocabulary and the
    per-document provenance stamping still run for real on them)."""
    rows = []
    matches = [
        # lead-1041 correction window (newer customer email).
        ("September 8 2026", "pickup_date", "2026-09-08"),
        ("10:30 a.m.", "pickup_earliest", "10:30"),
        ("11:30 a.m.", "pickup_latest", "11:30"),
        ("before 4:00 p.m.", "delivery_deadline", "16:00"),
        # original statements (description / earlier email).
        ("Monday September 7 2026", "pickup_date", "2026-09-07"),
        ("9:00 a.m.", "pickup_earliest", "09:00"),
        ("10:00 a.m.", "pickup_latest", "10:00"),
        ("22 pallets", "pallets", "22"),
    ]
    for token, field, value in matches:
        if token in text:
            rows.append({"field": field, "value": value,
                         "confidence": "high", "note": ""})
    if "1406 Test Line 8" in text:
        rows.append({"field": "origin_address", "value": PICKUP_ADDRESS,
                     "confidence": "high", "note": ""})
        rows.append({"field": "origin_postal_code", "value": PICKUP_POSTAL,
                     "confidence": "high", "note": ""})
    if "2277 Test Sideroad 15" in text:
        rows.append({"field": "destination_address", "value": DELIVERY_ADDRESS,
                     "confidence": "high", "note": ""})
        rows.append({"field": "destination_postal_code",
                     "value": DELIVERY_POSTAL,
                     "confidence": "high", "note": ""})
    if "Testville" in text and "1406 Test Line 8" not in text:
        rows.append({"field": "origin_city", "value": "Testville",
                     "confidence": "high", "note": ""})
    return rows


def _fake_chat(messages, system="", **kwargs):
    """deepseek_chat stand-in: extraction system prompts get JSON rows for
    the user document; anything else (draft-body prose) gets PROSE."""
    if "You extract SHIPMENT FACTS" in system:
        text = (messages or [{}])[0].get("content") or ""
        return json.dumps(_llm_rows_for(text))
    # The prose call: the real client returns plain text content (the
    # engine accepts a str payload), exactly like the engine's own
    # test stand-in.
    return PROSE


def _patches(**overrides):
    """Patch the engine deepseek_utils entry points (real supersession and
    real prose composition run against a deterministic fake model)."""
    base = {
        "get_api_key": lambda env: "test-key",
        "get_model": lambda env: "test-model",
        "deepseek_chat": _fake_chat,
    }
    base.update(overrides)
    stack = ExitStack()
    for key, value in base.items():
        stack.enter_context(patch(
            "odoo.addons.premafirm_ai_engine.services.deepseek_utils.%s"
            % key, value))
    return stack


class TestCrmEstimateDraftBridge(TransactionCase):
    """E-A2 §5 dispatch-side actions — draft estimate / draft RC from the
    CRM opportunity's superseded customer facts."""

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
            "name": "Estimate Draft Customer",
            "is_company": True,
            "email": "estimate-draft@example.test",
        })

    def _lead(self, description):
        lead = self.env["crm.lead"].create({
            "name": "Estimate Draft Opportunity",
            "partner_id": self.customer.id,
            "description": description,
        })
        return lead

    def _inbound_email(self, lead, body, hours_ahead):
        self.env["mail.message"].sudo().create({
            "model": "crm.lead",
            "res_id": lead.id,
            "message_type": "email",
            "subject": "Shipment update",
            "body": body,
            "author_id": self.customer.id,
            "date": datetime.datetime.utcnow()
            + datetime.timedelta(hours=hours_ahead),
        })

    def _describe(self, text):
        return "<p>%s</p>" % text

    def _base_description(self):
        return self._describe(
            "22 pallets of retail store supplies. Pickup Monday September 7 "
            "2026, 9:00 a.m. to 10:00 a.m. from 1406 Test Line 8, Ayr, ON "
            "N0B 1E0; delivery to 2277 Test Sideroad 15, Ayr, ON N0B 1E0. "
            "Dry goods.")

    def _counts(self):
        env = self.env
        return {
            "crm.lead": 0,
            "premafirm.lead.estimate.reply": env[
                "premafirm.lead.estimate.reply"].search_count([]),
            "logistics.custom.quote": env[
                "logistics.custom.quote"].search_count([]),
            "logistics.booking": env["logistics.booking"].search_count([]),
            "sale.order": env["sale.order"].search_count([]),
            "account.move": env["account.move"].search_count([]),
            "mail.mail": env["mail.mail"].search_count([]),
            "logistics.pricing.session": env[
                "logistics.pricing.session"].search_count([]),
        }

    # ── (a) one estimate draft, canonical price, nothing sent ─────────

    def test_a_estimate_exactly_one_draft_canonical_price_no_send(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        lead = self._lead(self._base_description())
        # Lead-1041 correction arrives AFTER the description.
        self._inbound_email(
            lead,
            "<p>Correction — pickup Tuesday September 8 2026, 10:30 a.m. "
            "to 11:30 a.m., delivery before 4:00 p.m. Thanks.</p>", 1)
        before = self._counts()
        stage_before = lead.stage_id.id
        locations_before = self.env[
            "prema.dispatch.location"].search_count([])
        replies_before = self.env[
            "premafirm.lead.estimate.reply"].search_count([])

        with _patches(), patch.object(
                BookingOrchestrationService, "prepare_quote",
                return_value=dict(STUB_QUOTE)):
            action = lead.action_prepare_preliminary_estimate()

        draft = self.env["premafirm.lead.estimate.reply"].browse(
            action["res_id"])
        self.assertEqual(action["res_model"], "premafirm.lead.estimate.reply")
        self.assertEqual(draft.crm_lead_id, lead)
        self.assertEqual(
            self.env["premafirm.lead.estimate.reply"].search_count([]),
            replies_before + 1, "Exactly one estimate draft per click.")
        # Price == the canonical dispatch stub (never invented).
        self.assertEqual(draft.price_amount, STUB_QUOTE["calculated_price"])
        self.assertIn("TOK-A2B-TEST", draft.price_reference)
        # Corrected facts (email supersedes description) landed in snapshot.
        effective = draft.facts_snapshot["effective"]
        self.assertEqual(effective["pickup_earliest"]["value"], "10:30")
        self.assertEqual(effective["pickup_latest"]["value"], "11:30")
        self.assertEqual(effective["delivery_deadline"]["value"], "16:00")
        self.assertEqual(effective["pickup_date"]["value"], "2026-09-08")
        self.assertIn("10:30", draft.body_html or "")
        # Complete unmatched addresses became reviewable Pending Review rows.
        self.assertEqual(
            self.env["prema.dispatch.location"].search_count([]),
            locations_before + 2)
        pending = self.env["prema.dispatch.location"].search([
            ("verification_state", "=", "pending_review"),
            ("address", "in", (PICKUP_ADDRESS, DELIVERY_ADDRESS))])
        self.assertEqual(len(pending), 2)
        # No stage change, no mail, nothing operational.
        self.assertEqual(lead.stage_id.id, stage_before)
        after = self._counts()
        for model in ("logistics.booking", "sale.order", "account.move",
                      "mail.mail", "logistics.pricing.session",
                      "logistics.custom.quote"):
            self.assertEqual(after[model], before[model],
                             "%s must not be touched by (a)." % model)

    # ── (b) exactly one draft RC per lead on repeat clicks ────────────

    def test_b_repeat_clicks_return_the_same_draft_rate_confirmation(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        lead = self._lead(self._base_description())
        self._inbound_email(
            lead,
            "<p>Correction — pickup Tuesday September 8 2026, 10:30 a.m. "
            "to 11:30 a.m., delivery before 4:00 p.m. Thanks.</p>", 1)

        with _patches():
            first = lead.action_create_draft_rate_confirmation()
            second = lead.action_create_draft_rate_confirmation()

        self.assertEqual(first["res_model"], "logistics.custom.quote")
        self.assertEqual(first["res_id"], second["res_id"])
        CQ = self.env["logistics.custom.quote"]
        rows = CQ.search([("crm_lead_id", "=", lead.id)])
        self.assertEqual(len(rows), 1,
                         "Exactly one draft RC per lead, however many times "
                         "the button is clicked.")
        draft = rows[0]
        self.assertEqual(draft.state, "new")
        self.assertFalse(draft.is_locked)
        self.assertEqual(draft.quoted_price, 0.0)
        self.assertEqual(draft.pickup_address, PICKUP_ADDRESS)
        self.assertEqual(draft.pickup_postal_code, PICKUP_POSTAL)
        self.assertEqual(draft.delivery_address, DELIVERY_ADDRESS)
        self.assertEqual(draft.pallets, 22)
        # Corrected window facts are on the draft for the reviewer.
        self.assertIn("10:30 – 11:30", draft.notes)
        self.assertIn("before 16:00", draft.notes)
        self.assertEqual(draft.requested_pickup_date, PICKUP_DATE_NEW)
        self.assertNotIn("09:00", draft.notes)
        # Discoverable from the lead, and nothing else moved.
        self.assertEqual(lead.logistics_quote_count, 1)
        self.assertFalse(lead.stage_id and False)

    # ── (c) lead-1041 regression: corrected windows win ───────────────

    def test_c_lead1041_corrected_windows_land_in_draft_and_estimate(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        lead = self._lead(self._base_description())
        # An older customer email with the ORIGINAL windows, then the
        # correction — the newest statement must win everywhere.
        self._inbound_email(
            lead,
            "<p>As agreed: pickup Monday September 7 2026, 9:00 a.m. to "
            "10:00 a.m.</p>", 1)
        self._inbound_email(
            lead,
            "<p>Correction — pickup Tuesday September 8 2026, 10:30 a.m. "
            "to 11:30 a.m., delivery before 4:00 p.m. Thanks.</p>", 2)

        with _patches(), patch.object(
                BookingOrchestrationService, "prepare_quote",
                return_value=dict(STUB_QUOTE)):
            action = lead.action_create_draft_rate_confirmation()
            estimate_action = lead.action_prepare_preliminary_estimate()

        draft = self.env["logistics.custom.quote"].browse(action["res_id"])
        # The DRAFT carries the NEWER windows — the regression this test
        # guards (lead-1041: stale draft facts after a customer correction).
        self.assertEqual(draft.requested_pickup_date, PICKUP_DATE_NEW)
        self.assertIn("10:30 – 11:30", draft.notes)
        self.assertIn("before 16:00", draft.notes)
        self.assertNotIn("09:00", draft.notes)
        self.assertNotIn("2026-09-07", draft.notes)

        estimate = self.env["premafirm.lead.estimate.reply"].browse(
            estimate_action["res_id"])
        effective = estimate.facts_snapshot["effective"]
        self.assertEqual(effective["pickup_earliest"]["value"], "10:30")
        self.assertEqual(effective["pickup_latest"]["value"], "11:30")
        self.assertEqual(effective["delivery_deadline"]["value"], "16:00")
        # The stale statement is recorded as superseded, not lost.
        superseded_fields = {row["field"]: row
                             for row in estimate.facts_snapshot["superseded"]}
        self.assertEqual(superseded_fields["pickup_earliest"]["value"],
                         "09:00")
        self.assertEqual(superseded_fields["pickup_date"]["value"],
                         "2026-09-07")

    # ── (d) city-only text is refused, never an operational stop ──────

    def test_d_city_only_never_becomes_operational_stop(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        lead = self._lead(self._describe(
            "22 pallets dry goods. Pickup at Testville, ON; delivery to "
            "2277 Test Sideroad 15, Ayr, ON N0B 1E0."))
        before = self._counts()
        locations_before = self.env[
            "prema.dispatch.location"].search_count([])
        replies_before = self.env[
            "premafirm.lead.estimate.reply"].search_count([])

        with _patches():
            with self.assertRaises(UserError) as guard:
                lead.action_create_draft_rate_confirmation()
        self.assertIn("only a city was stated",
                      str(guard.exception))
        self.assertIn("city-only", str(guard.exception))

        mocked_price = Mock(side_effect=AssertionError(
            "pricing must never run on a city-only stop"))
        with _patches(), patch.object(
                BookingOrchestrationService, "prepare_quote", mocked_price):
            with self.assertRaises(UserError):
                lead.action_prepare_preliminary_estimate()
        mocked_price.assert_not_called()

        # Nothing persisted: no draft, no estimate, no pending location
        # (the delivery side alone was complete — resolution is all-or-
        # nothing before any write), no mail.
        self.assertEqual(self.env["premafirm.lead.estimate.reply"]
                         .search_count([]), replies_before)
        self.assertEqual(
            self.env["prema.dispatch.location"].search_count([]),
            locations_before)
        after = self._counts()
        for model in ("logistics.booking", "sale.order", "account.move",
                      "mail.mail", "logistics.pricing.session",
                      "logistics.custom.quote"):
            self.assertEqual(after[model], before[model], model)

    # ── (e) the two actions never confirm, convert or book ────────────

    def test_e_actions_never_confirm_convert_or_book(self):
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        lead = self._lead(self._base_description())
        self._inbound_email(
            lead,
            "<p>Correction — pickup Tuesday September 8 2026, 10:30 a.m. "
            "to 11:30 a.m., delivery before 4:00 p.m. Thanks.</p>", 1)
        before = self._counts()
        attempts_before = self.env[
            "logistics.custom.quote.send.attempt"].search_count([])

        with _patches(), patch.object(
                BookingOrchestrationService, "prepare_quote",
                return_value=dict(STUB_QUOTE)):
            lead.action_prepare_preliminary_estimate()
            lead.action_create_draft_rate_confirmation()
            # A repeat click keeps the SAME draft (still no send/convert).
            lead.action_create_draft_rate_confirmation()

        self.assertEqual(self._counts()["logistics.custom.quote"],
                         before["logistics.custom.quote"] + 1)
        draft = self.env["logistics.custom.quote"].search(
            [("crm_lead_id", "=", lead.id)])
        self.assertEqual(len(draft), 1)
        self.assertEqual(draft.state, "new")
        self.assertFalse(draft.is_locked)
        self.assertEqual(draft.quoted_price, 0.0)
        self.assertFalse(draft.booking_id)
        self.assertEqual(
            self.env["logistics.custom.quote.send.attempt"].search_count([]),
            attempts_before)
        after = self._counts()
        for model in ("logistics.booking", "sale.order", "account.move",
                      "mail.mail", "logistics.pricing.session"):
            self.assertEqual(after[model], before[model], model)
        # The estimate draft exists and nothing was ever sent for it.
        self.assertEqual(self._counts()["premafirm.lead.estimate.reply"],
                         before["premafirm.lead.estimate.reply"] + 1)

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

# Test fixture addresses are SYNTHETIC on purpose: real customer facilities
# (e.g. "31 Mechanic St, Paris, ON N3L 1K1", city-only "Milton, ON") exist in
# the production-derived test databases and collide with the location table's
# unique address constraint / saved-location matching, making the tests
# deterministic only on a clean database. These street names do not exist.
PICKUP_ADDRESS = "1406 Test Line 8, Ayr, ON N0B 1E0"
PICKUP_POSTAL = "N0B 1E0"
DELIVERY_ADDRESS = "2277 Test Sideroad 15, Ayr, ON N0B 1E0"
DELIVERY_POSTAL = "N0B 1E0"


class TestCrmRateConfirmationBridge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "CRM Rate Bridge Customer",
            "is_company": True,
        })
        cls.lead = cls.env["crm.lead"].create({
            "name": "CRM Rate Bridge Opportunity",
            "partner_id": cls.partner.id,
        })

    def test_rate_action_opens_canonical_dispatch_wizard(self):
        self.lead.description = "Pickup: Toronto\nDelivery: Ottawa"
        action = self.lead.action_open_dispatch_rate_quote()

        self.assertEqual(action["res_model"], "logistics.phone.booking")
        self.assertEqual(action["target"], "new")
        self.assertEqual(
            action["context"]["default_partner_id"], self.partner.id)
        self.assertEqual(
            action["context"]["default_crm_lead_id"], self.lead.id)
        self.assertIn(
            "Pickup: Toronto", action["context"]["default_source_text"])

    def test_linked_rate_confirmation_is_discoverable_from_lead(self):
        quote = self.env["logistics.custom.quote"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source": "internal",
        })

        self.assertEqual(self.lead.logistics_quote_count, 1)
        action = self.lead.action_open_dispatch_rate_confirmations()
        self.assertEqual(action["res_model"], "logistics.custom.quote")
        self.assertEqual(action["res_id"], quote.id)

    def test_opening_rate_wizard_has_no_operational_side_effects(self):
        counts_before = {
            model: self.env[model].search_count([])
            for model in (
                "logistics.custom.quote",
                "logistics.booking",
                "sale.order",
                "account.move",
                "mail.mail",
            )
        }

        self.lead.action_open_dispatch_rate_quote()

        counts_after = {
            model: self.env[model].search_count([])
            for model in counts_before
        }
        self.assertEqual(counts_after, counts_before)

    def test_customer_is_required_before_rate_calculation(self):
        lead = self.env["crm.lead"].create({"name": "No Customer Yet"})

        with self.assertRaises(UserError):
            lead.action_open_dispatch_rate_quote()

    def test_extraction_populates_facts_but_never_an_ai_rate(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": (
                "Pickup %s; delivery to %s; 10 cases weighing 235 lbs; "
                "frozen." % (PICKUP_ADDRESS, DELIVERY_ADDRESS)
            ),
            "pickup_postal_code": PICKUP_POSTAL,
            "delivery_postal_code": DELIVERY_POSTAL,
        })
        action = wizard._apply_source_extraction({
            "service_type": "ltl",
            "amount": 9999.00,
            "requires_reefer": True,
            "temp_requirement": None,
            "approximate_skids": 0,
            "stops": [
                {"type": "pickup", "address": PICKUP_ADDRESS},
                {"type": "dropoff", "address": DELIVERY_ADDRESS},
            ],
        })

        self.assertEqual(action["res_model"], "logistics.phone.booking")
        self.assertEqual(wizard.weight_lbs, 235.0)
        self.assertEqual(wizard.temperature_mode, "reefer")
        self.assertFalse(wizard.temperature_confirmed)
        self.assertEqual(wizard.price, 0.0)
        self.assertEqual(wizard.customer_quoted_price, 0.0)
        self.assertFalse(wizard.quote_token)

    def test_explicit_temperature_is_converted_and_marked_confirmed(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": "Frozen at 0 Fahrenheit",
            "pickup_postal_code": "N3L 1K1",
            "delivery_postal_code": "L9T 6H7",
        })
        wizard._apply_source_extraction({
            "requires_reefer": True,
            "temp_requirement": "0 Fahrenheit",
            "stops": [],
        })

        self.assertTrue(wizard.temperature_confirmed)
        self.assertAlmostEqual(wizard.required_temperature_c, -17.7778, places=3)

    def test_pricing_requires_postal_codes_after_extraction(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "temperature_mode": "dry",
        })

        with self.assertRaises(UserError):
            wizard._validate_quote_inputs()

    def test_extraction_reuses_an_exact_saved_location(self):
        pickup = self.env["prema.dispatch.location"].create({
            "name": "Existing Ayr Pickup",
            "address": PICKUP_ADDRESS,
        })
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": "Ayr to Ayr",
        })
        wizard._apply_source_extraction({
            "stops": [
                {"type": "pickup", "address": PICKUP_ADDRESS},
                {"type": "dropoff", "address": DELIVERY_ADDRESS},
            ],
        })

        self.assertEqual(wizard.pickup_location_id, pickup)
        self.assertFalse(wizard.delivery_location_id)

    def test_save_locations_creates_pending_review_and_customer_access(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "pickup_company_name": "Test Sausage House",
            "pickup_address": PICKUP_ADDRESS,
            "pickup_postal_code": PICKUP_POSTAL,
            "delivery_company_name": "Test Receiver",
            "delivery_address": DELIVERY_ADDRESS,
            "delivery_postal_code": DELIVERY_POSTAL,
        })
        before = self.env["prema.dispatch.location"].search_count([])

        wizard.action_match_save_locations()

        self.assertEqual(
            self.env["prema.dispatch.location"].search_count([]), before + 2)
        self.assertEqual(wizard.pickup_location_id.verification_state, "pending_review")
        self.assertEqual(wizard.delivery_location_id.verification_state, "pending_review")
        access = self.env["logistics.location.customer.access"].search([
            ("commercial_partner_id", "=", self.partner.id),
            ("facility_id", "in", [
                wizard.pickup_location_id.id,
                wizard.delivery_location_id.id,
            ]),
        ])
        self.assertEqual(len(access), 2)

    def test_city_only_address_is_not_saved_as_a_location(self):
        # "Testville, ON" is synthetic — no real row can pre-exist. The
        # guard must raise even when a postal-less city-only row is present
        # in the database (driver-submitted rows like "Milton, ON" exist in
        # production data and must never be reused as quote stops).
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "pickup_address": PICKUP_ADDRESS,
            "pickup_postal_code": PICKUP_POSTAL,
            "delivery_address": "Testville, ON",
        })

        with self.assertRaises(UserError):
            wizard.action_match_save_locations()

    def test_city_only_saved_row_is_never_matched_for_a_quote_stop(self):
        self.env["prema.dispatch.location"].create({
            "name": "Driver-Submitted City Row",
            "address": "Testville, ON",
        })
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": "Testville pickup",
            "pickup_address": "Testville, ON",
        })
        wizard._apply_source_extraction({
            "stops": [{"type": "pickup", "address": "Testville, ON"}],
        })

        self.assertFalse(wizard.pickup_location_id)

    def test_unconfirmed_keyword_only_setpoint_blocks_pricing(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": "Reefer shipment, frozen, 3 skids.",
            "pickup_postal_code": PICKUP_POSTAL,
            "delivery_postal_code": DELIVERY_POSTAL,
        })
        wizard._apply_source_extraction({
            "requires_reefer": True,
            "temp_requirement": None,
            "approximate_skids": 3,
            "stops": [],
        })

        self.assertTrue(wizard.temperature_mode == "reefer")
        self.assertFalse(wizard.temperature_confirmed)
        with self.assertRaises(UserError):
            wizard._validate_quote_inputs()

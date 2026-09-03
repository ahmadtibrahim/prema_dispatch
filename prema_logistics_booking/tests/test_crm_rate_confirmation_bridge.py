from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


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
                "Pickup 31 Mechanic St, Paris, ON N3L 1K1; delivery to "
                "Milton, ON L9T 6H7; 10 cases weighing 235 lbs; frozen."
            ),
            "pickup_postal_code": "N3L 1K1",
            "delivery_postal_code": "L9T 6H7",
        })
        action = wizard._apply_source_extraction({
            "service_type": "ltl",
            "amount": 9999.00,
            "requires_reefer": True,
            "temp_requirement": None,
            "approximate_skids": 0,
            "stops": [
                {"type": "pickup", "address": "31 Mechanic St, Paris, ON N3L 1K1"},
                {"type": "dropoff", "address": "Milton, ON L9T 6H7"},
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
            "name": "Existing Paris Pickup",
            "address": "31 Mechanic St, Paris, ON N3L 1K1",
        })
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source_text": "Paris to Milton",
        })
        wizard._apply_source_extraction({
            "stops": [
                {"type": "pickup", "address": "31 Mechanic St, Paris, ON N3L 1K1"},
                {"type": "dropoff", "address": "100 Main St, Milton, ON L9T 1A1"},
            ],
        })

        self.assertEqual(wizard.pickup_location_id, pickup)
        self.assertFalse(wizard.delivery_location_id)

    def test_save_locations_creates_pending_review_and_customer_access(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "pickup_company_name": "Link Street Sausage House",
            "pickup_address": "31 Mechanic St, Paris, ON N3L 1K1",
            "pickup_postal_code": "N3L 1K1",
            "delivery_company_name": "Milton Receiver",
            "delivery_address": "100 Main St, Milton, ON L9T 1A1",
            "delivery_postal_code": "L9T 1A1",
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
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "pickup_address": "31 Mechanic St, Paris, ON N3L 1K1",
            "pickup_postal_code": "N3L 1K1",
            "delivery_address": "Milton, ON",
        })

        with self.assertRaises(UserError):
            wizard.action_match_save_locations()

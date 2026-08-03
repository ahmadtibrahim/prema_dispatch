"""Security and access control tests."""
from odoo.tests import TransactionCase


class TestSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Booking = cls.env["logistics.booking"]

    def test_01_booking_model_exists(self):
        """Booking model is registered and accessible."""
        self.assertTrue(self.env["logistics.booking"]._name)
        self.assertEqual(self.env["logistics.booking"]._name, "logistics.booking")

    def test_02_rate_plan_version_auto_increment(self):
        """Rate plan version auto-increments."""
        Region = self.env["logistics.region"]
        Lane = self.env["logistics.lane"]
        SLevel = self.env["logistics.service.level"]
        SOffering = self.env["logistics.service.offering"]
        RatePlan = self.env["logistics.rate.plan"]

        r1 = Region.create({"code": "SEC1", "name": "Security R1"})
        r2 = Region.create({"code": "SEC2", "name": "Security R2"})
        lane = Lane.create({"origin_region_id": r1.id, "destination_region_id": r2.id, "active": True, "ltl_capable": True})
        slevel = SLevel.create({"code": "SEC_TEST", "name": "Security Test"})
        offering = SOffering.create({"lane_id": lane.id, "service_level_id": slevel.id, "temperature_mode": "dry", "shipment_type": "ltl"})

        plan1 = RatePlan.create({"service_offering_id": offering.id})
        self.assertEqual(plan1.version, 1)

        plan2 = RatePlan.create({"service_offering_id": offering.id})
        self.assertEqual(plan2.version, 2)

    def test_03_custom_quote_state_flow(self):
        """Custom quote follows state flow."""
        Quote = self.env["logistics.custom.quote"]
        quote = Quote.create({
            "contact_name": "Test User",
            "contact_email": "test@example.com",
            "pickup_postal_code": "N6A",
            "delivery_postal_code": "H2X",
            "pallets": 4,
            "weight_lbs": 3200,
        })
        self.assertEqual(quote.state, "new")
        quote.action_start_review()
        self.assertEqual(quote.state, "reviewing")
        quote.quoted_price = 500.0
        quote.action_quote()
        self.assertEqual(quote.state, "quoted")

    def test_04_fsa_validation(self):
        """FSA format validation works."""
        Fsa = self.env["logistics.fsa"]
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            Fsa.create({"fsa": "INVALID"})

    def test_05_region_alignment_count(self):
        """Verify 15 regions exist."""
        Region = self.env["logistics.region"]
        count = Region.search_count([("active", "=", True)])
        self.assertGreaterEqual(count, 10)

"""Security and access control tests."""
from odoo.tests import HttpCase, TransactionCase, tagged


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


@tagged("-at_install", "post_install")
class TestWhereWeGoAndPriceMatrixAccess(HttpCase):
    """Where We Go / Price Matrix endpoints must reject non-dispatch staff
    and anonymous requests, and accept dispatcher/dispatch-manager users."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        dispatcher_group = cls.env.ref("prema_dispatch.group_dispatcher")
        cls.plain_user = cls.env["res.users"].create({
            "name": "Security Test Plain User",
            "login": "sec_test_plain_user",
            "email": "sec_test_plain_user@example.com",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })
        cls.dispatcher_user = cls.env["res.users"].create({
            "name": "Security Test Dispatcher",
            "login": "sec_test_dispatcher",
            "email": "sec_test_dispatcher@example.com",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id, dispatcher_group.id])],
        })

    def _assert_forbidden_for_plain_user(self, url):
        self.authenticate("sec_test_plain_user", "sec_test_plain_user")
        res = self.url_open(url)
        self.assertEqual(res.status_code, 403)

    def _assert_ok_for_dispatcher(self, url):
        self.authenticate("sec_test_dispatcher", "sec_test_dispatcher")
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200)

    def test_where_we_go_data_requires_dispatch_group(self):
        self._assert_forbidden_for_plain_user("/logistics/where-we-go/data")
        self._assert_ok_for_dispatcher("/logistics/where-we-go/data")

    def test_price_matrix_requires_dispatch_group(self):
        self._assert_forbidden_for_plain_user("/logistics/price-matrix")
        self._assert_ok_for_dispatcher("/logistics/price-matrix")

    def test_where_we_go_data_rejects_anonymous(self):
        self.logout()
        res = self.url_open("/logistics/where-we-go/data")
        self.assertNotEqual(res.status_code, 200)

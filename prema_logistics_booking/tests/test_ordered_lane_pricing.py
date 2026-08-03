"""Ordered-lane rate-plan pricing — isolated fixture tests."""
from datetime import date, timedelta
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.route_resolver import RouteResolver
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestOrderedLanePricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ── Isolated test fixtures: regions, FSAs, hub, lanes, rate plans ──
        cls.r_a = cls.env['logistics.region'].create({'code': 'OTPA', 'name': 'OTP Region A'})
        cls.r_b = cls.env['logistics.region'].create({'code': 'OTPB', 'name': 'OTP Region B'})
        cls.r_c = cls.env['logistics.region'].create({'code': 'OTPC', 'name': 'OTP Region C (Hub)'})

        cls.fsa_a = cls.env['logistics.fsa'].create({
            'fsa': 'O1A', 'region_id': cls.r_a.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa_b = cls.env['logistics.fsa'].create({
            'fsa': 'O2B', 'region_id': cls.r_b.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa_c = cls.env['logistics.fsa'].create({
            'fsa': 'O3C', 'region_id': cls.r_c.id, 'pickup_supported': True, 'delivery_supported': True,
        })

        # Hub with explicit canonical region
        cls.hub = cls.env['logistics.hub'].create({
            'name': 'OTP Test Hub', 'public_name': 'OTP Test Hub', 'code': 'OTP-HUB',
            'canonical_region_id': cls.r_c.id, 'is_default': True,
        })

        # Direct lane A→B
        cls.lane_ab = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r_a.id, 'destination_region_id': cls.r_b.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        # Direct lane B→A (different price)
        cls.lane_ba = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r_b.id, 'destination_region_id': cls.r_a.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        # Leg lanes for hub transfer
        cls.lane_ac = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r_a.id, 'destination_region_id': cls.r_c.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        cls.lane_cb = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r_c.id, 'destination_region_id': cls.r_b.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })

        slevel = cls.env['logistics.service.level'].search([
            ('code', '=', 'OTP_LVL')
        ], limit=1)
        if not slevel:
            slevel = cls.env['logistics.service.level'].create({
                'code': 'OTP_LVL', 'name': 'OTP Test Level', 'reefer_food_eligible': True,
            })

        def _make_rp(lane, revenue, tlq):
            offering = cls.env['logistics.service.offering'].create({
                'lane_id': lane.id, 'service_level_id': slevel.id,
                'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
            })
            cls.env['logistics.lane.schedule'].create({
                'service_offering_id': offering.id, 'cutoff_time': 16.0,
                'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
                'pickup_thursday': True, 'pickup_friday': True,
                'delivery_offset_type': 'next_day', 'active': True,
            })
            return cls.env['logistics.rate.plan'].create({
                'service_offering_id': offering.id, 'revenue_target': revenue,
                'target_load_quantity': tlq, 'active': True,
                'effective_from': date.today() - timedelta(days=30),
            })

        # Rate plans: A→B=$200, B→A=$250, A→C=$100, C→B=$150
        cls.rp_ab = _make_rp(cls.lane_ab, 1600.0, 8)  # $200/pallet
        cls.rp_ba = _make_rp(cls.lane_ba, 2000.0, 8)  # $250/pallet
        cls.rp_ac = _make_rp(cls.lane_ac, 800.0, 8)   # $100/pallet
        cls.rp_cb = _make_rp(cls.lane_cb, 1200.0, 8)  # $150/pallet

        cls.svc = PricingService(cls.env)
        cls.resolver = RouteResolver(cls.env)

    # ── Directional pricing ───────────────────────────────────────────

    def test_01_a_to_b_direct(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, r.reason)
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)
        self.assertIsNotNone(r.rate_plan)
        self.assertIsNotNone(r.pickup_date)
        self.assertIsNotNone(r.delivery_date_estimate)

    def test_02_b_to_a_different_price(self):
        r = self.svc.calculate(self.fsa_b, self.fsa_a, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, r.reason)
        self.assertAlmostEqual(r.calculated_price, 250.00, places=2)
        self.assertNotEqual(r.calculated_price, 200.00)

    def test_03_ab_not_equal_ba(self):
        r1 = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        r2 = self.svc.calculate(self.fsa_b, self.fsa_a, "ltl", "dry", 1, 500)
        self.assertTrue(r1.available and r2.available)
        self.assertNotEqual(r1.calculated_price, r2.calculated_price)

    # ── Hub transfer ──────────────────────────────────────────────────

    def test_04_hub_transfer_two_legs(self):
        """A→C→B via hub: $100 + $150 = $250."""
        # Remove direct A→B to force hub transfer
        self.rp_ab.active = False
        try:
            route = self.resolver.resolve(self.fsa_a, self.fsa_b, 1, 500)
            self.assertTrue(route.available, route.reason)
            self.assertEqual(len(route.legs), 2, "Must have 2 legs for hub transfer")
            leg_sum = sum(leg["price"] for leg in route.legs)
            self.assertAlmostEqual(leg_sum, 250.00, places=2)
        finally:
            self.rp_ab.active = True

    def test_05_hub_transfer_leg_sum_matches_calculate(self):
        self.rp_ab.active = False
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
            self.assertTrue(r.available, r.reason)
            leg_sum = 100.00 + 150.00  # A→C + C→B
            self.assertAlmostEqual(r.calculated_price, leg_sum, places=2)
        finally:
            self.rp_ab.active = True

    # ── Direct lane precedence ────────────────────────────────────────

    def test_06_direct_preferred_over_hub(self):
        """Direct lane must be used even when hub transfer is available."""
        route = self.resolver.resolve(self.fsa_a, self.fsa_b, 1, 500)
        self.assertTrue(route.available)
        self.assertEqual(len(route.legs), 1, "Direct lane should be preferred")
        self.assertEqual(route.legs[0]["rate_plan"], self.rp_ab)

    # ── Rate plan inactivity/expiry ───────────────────────────────────

    def test_07_inactive_rate_plan_request_quote(self):
        self.rp_ab.active = False
        self.rp_ac.active = False  # Block hub transfer too
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
            self.assertFalse(r.available)
            self.assertEqual(r.reason, "request_quote")
        finally:
            self.rp_ab.active = True
            self.rp_ac.active = True

    def test_08_future_rate_plan_not_used(self):
        """Rate plan effective_from in the future must not be selected."""
        self.rp_ab.effective_from = date.today() + timedelta(days=30)
        self.rp_ac.active = False  # Block hub
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
            self.assertFalse(r.available, "Future rate plan must not price")
        finally:
            self.rp_ab.effective_from = date.today() - timedelta(days=30)
            self.rp_ac.active = True

    def test_09_expired_rate_plan_not_used(self):
        self.rp_ab.effective_to = date.today() - timedelta(days=1)
        self.rp_ac.active = False
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
            self.assertFalse(r.available)
        finally:
            self.rp_ab.effective_to = False
            self.rp_ac.active = True

    # ── Customer rate with effective dates ────────────────────────────

    def test_10_customer_rate_precedence(self):
        partner = self.env['res.partner'].create({'name': 'TEST-CR-Partner'})
        cr = self.env['logistics.customer.rate'].create({
            'partner_id': partner.id, 'lane_id': self.lane_ab.id,
            'discount_pct': 10.0, 'active': True,
            'effective_from': date.today() - timedelta(days=30),
        })
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500, partner=partner)
            self.assertTrue(r.available)
            self.assertAlmostEqual(r.calculated_price, 180.00, places=2)
        finally:
            cr.active = False
            partner.active = False

    def test_11_customer_rate_wrong_lane_rejected(self):
        """Customer rate for wrong lane must not apply."""
        partner = self.env['res.partner'].create({'name': 'TEST-WRONG-Partner'})
        cr = self.env['logistics.customer.rate'].create({
            'partner_id': partner.id, 'lane_id': self.lane_ba.id,  # B→A, not A→B
            'discount_pct': 50.0, 'active': True,
        })
        try:
            r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500, partner=partner)
            self.assertTrue(r.available)
            self.assertAlmostEqual(r.calculated_price, 200.00, places=2,
                msg="Wrong-lane customer rate must not apply")
        finally:
            cr.active = False
            partner.active = False

    # ── Missing hub mapping ───────────────────────────────────────────

    def test_12_missing_hub_region_request_quote(self):
        self.hub.canonical_region_id = False
        self.rp_ab.active = False  # Block direct
        try:
            route = self.resolver.resolve(self.fsa_a, self.fsa_b, 1, 500)
            self.assertFalse(route.available, "Missing hub region must fail")
            self.assertEqual(route.reason, "request_quote")
        finally:
            self.hub.canonical_region_id = self.r_c.id
            self.rp_ab.active = True

    # ── LTL/FTL and Dry/Reefer ────────────────────────────────────────

    def test_13_reefer_same_price_as_dry(self):
        r_dry = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        r_reefer = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "reefer", 1, 500)
        self.assertTrue(r_dry.available and r_reefer.available)
        self.assertAlmostEqual(r_dry.calculated_price, r_reefer.calculated_price, places=2)

    # ── Capacity gate ─────────────────────────────────────────────────

    def test_14_thirteen_pallets_request_quote(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 13, 6500)
        self.assertFalse(r.available)
        self.assertIn("pallets", r.reason)

    def test_15_twelve_pallets_accepted(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 12, 6000)
        self.assertTrue(r.available)

    # ── No per-km or accessorial leakage ──────────────────────────────

    def test_16_no_per_km_leakage(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    def test_17_no_accessorial_leakage(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500,
                               liftgate_pickup=True, liftgate_delivery=True, residential=True)
        self.assertTrue(r.available)
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    # ── Route snapshot in PricingResult ───────────────────────────────

    def test_18_route_snapshot_present(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        snap = r.route_snapshot
        self.assertTrue(snap, "route_snapshot must not be empty")
        self.assertIn("legs", snap)
        self.assertEqual(snap["leg_count"], 1)
        self.assertIn("calculated_price", snap)
        leg = snap["legs"][0]
        self.assertEqual(leg["rate_plan_id"], self.rp_ab.id)
        self.assertEqual(leg["rate_plan_version"], self.rp_ab.version)
        self.assertEqual(leg["origin_region"], 'OTPA')
        self.assertEqual(leg["dest_region"], 'OTPB')

    # ── Schedule and dates ────────────────────────────────────────────

    def test_19_pickup_and_delivery_dates_present(self):
        r = self.svc.calculate(self.fsa_a, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        self.assertIsNotNone(r.pickup_date, "pickup_date must not be None")
        self.assertIsNotNone(r.delivery_date_estimate, "delivery_date_estimate must not be None")
        self.assertTrue(r.pickup_date <= r.delivery_date_estimate,
                        f"pickup {r.pickup_date} must be <= delivery {r.delivery_date_estimate}")

    # ── Unresolvable routes ───────────────────────────────────────────

    def test_20_unsupported_fsa_request_quote(self):
        bad = self.env['logistics.fsa'].create({
            'fsa': 'Z9Z', 'pickup_supported': False, 'delivery_supported': False,
        })
        r = self.svc.calculate(bad, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertTrue(r.reason)

    def test_21_unmapped_region_request_quote(self):
        no_region = self.env['logistics.fsa'].create({
            'fsa': 'Z9Y', 'pickup_supported': True, 'delivery_supported': True,
        })
        r = self.svc.calculate(no_region, self.fsa_b, "ltl", "dry", 1, 500)
        self.assertFalse(r.available)

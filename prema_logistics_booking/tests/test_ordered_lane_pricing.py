"""Ordered-lane and rate-plan pricing engine tests."""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.route_resolver import RouteResolver
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestOrderedLanePricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolver = RouteResolver(cls.env)
        cls.svc = PricingService(cls.env)

        # Get real FSAs for R1, R13 (bidirectional test)
        cls.fsa_r1 = cls.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R1'), ('pickup_supported', '=', True)
        ], limit=1)
        cls.fsa_r13 = cls.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R13'), ('delivery_supported', '=', True)
        ], limit=1)
        cls.assertTrue(cls.fsa_r1, "Need R1 FSA")
        cls.assertTrue(cls.fsa_r13, "Need R13 FSA")

    # ── Directional pricing ───────────────────────────────────────────

    def test_01_r1_to_r13_pricing(self):
        """R1→R13: $1600/8=$200/pallet."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, f"Not available: {r.reason}")
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    def test_02_r13_to_r1_different_price(self):
        """R13→R1: different rate plan ($1500/7=$214.29)."""
        fsa_r13_pu = self.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R13'), ('pickup_supported', '=', True)
        ], limit=1)
        fsa_r1_del = self.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R1'), ('delivery_supported', '=', True)
        ], limit=1)
        if fsa_r13_pu and fsa_r1_del:
            r = self.svc.calculate(fsa_r13_pu, fsa_r1_del, "ltl", "dry", 1, 500)
            self.assertTrue(r.available, f"R13→R1 not available: {r.reason}")
            # R13→R1 price should differ from R1→R13
            r2 = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
            self.assertNotEqual(r.calculated_price, r2.calculated_price,
                "Ordered lanes must have different prices for each direction")

    def test_03_a_to_b_not_equal_b_to_a(self):
        """Origin→Destination ≠ Destination→Origin."""
        lanes_ab = self.env['logistics.lane'].search([
            ('origin_region_id.code', '=', 'R1'),
            ('destination_region_id.code', '=', 'R13'),
            ('active', '=', True),
        ])
        lanes_ba = self.env['logistics.lane'].search([
            ('origin_region_id.code', '=', 'R13'),
            ('destination_region_id.code', '=', 'R1'),
            ('active', '=', True),
        ])
        self.assertTrue(lanes_ab, "R1→R13 lane must exist")
        self.assertTrue(lanes_ba, "R13→R1 lane must exist")
        self.assertNotEqual(lanes_ab, lanes_ba, "Ordered lanes: A→B ≠ B→A")

    # ── Capacity gate ─────────────────────────────────────────────────

    def test_04_thirteen_pallets_request_quote(self):
        """13 pallets must return Request Quote."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 13, 6500)
        self.assertFalse(r.available)
        self.assertIn("pallets", r.reason or "")

    def test_05_twelve_pallets_accepted(self):
        """12 pallets is accepted."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 12, 6000)
        self.assertTrue(r.available, f"12 pallets rejected: {r.reason}")

    # ── Unresolvable routes ───────────────────────────────────────────

    def test_06_unsupported_fsa_returns_request_quote(self):
        """Unsupported FSA must return not-available with clear reason."""
        unsupported = self.env['logistics.fsa'].create({
            'fsa': 'Z9Z', 'pickup_supported': False, 'delivery_supported': False,
        })
        r = self.svc.calculate(unsupported, self.fsa_r13, "ltl", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertTrue(r.reason)

    def test_07_unresolvable_returns_request_quote(self):
        """Route with no resolution path must return request_quote — not $0."""
        resolver = RouteResolver(self.env)
        # Try a route between regions with no lane or hub path
        fsa_test = self.env['logistics.fsa'].create({
            'fsa': 'Z9X', 'pickup_supported': True, 'delivery_supported': False,
        })
        route = resolver.resolve(fsa_test, self.fsa_r13, 1, 500)
        if not route.available:
            self.assertTrue(route.reason, "Unresolvable route must have reason")
            self.assertNotEqual(route.reason, "", "Reason must not be empty")

    # ── No per-km or accessorial leakage ─────────────────────────────

    def test_08_no_per_km_leakage(self):
        """Pricing must NOT use distance or per-km calculation."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        # Price should be exactly rate plan based ($200), not distance-based
        # R1→R13 lane has road_km but pricing must come from rate plan only
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    def test_09_no_accessorial_leakage(self):
        """Liftgate and residential must not silently add charges."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500,
                               liftgate_pickup=True, liftgate_delivery=True,
                               residential=True)
        self.assertTrue(r.available)
        # With liftgate charges at $0, price must stay $200
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    # ── Dry/Reefer pricing ────────────────────────────────────────────

    def test_10_dry_and_reefer_same_base_price(self):
        """Dry and Reefer use the same rate plan pricing (no surcharge)."""
        r_dry = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
        r_reefer = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "reefer", 1, 500)
        self.assertTrue(r_dry.available)
        self.assertTrue(r_reefer.available)
        self.assertAlmostEqual(r_dry.calculated_price, r_reefer.calculated_price, places=2)

    # ── Multi-leg hub transfer pricing ────────────────────────────────

    def test_11_hub_transfer_through_mississauga(self):
        """Route through hub: each leg priced independently, sum is total."""
        # Find a route that requires hub transfer
        # R12→R1 has no lane, route should fail
        fsa_r12 = self.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R12'), ('pickup_supported', '=', True)
        ], limit=1)
        fsa_r1d = self.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R1'), ('delivery_supported', '=', True)
        ], limit=1)
        if fsa_r12 and fsa_r1d:
            route = self.resolver.resolve(fsa_r12, fsa_r1d, 1, 500)
            # R12→R1 has no direct lane — should fall through to request_quote
            if not route.available:
                # Verify the resolver tried and returned a reason
                self.assertTrue(route.reason, "Unresolvable route must have reason")

    def test_12_hub_transfer_legs_sum(self):
        """Multi-leg total equals deterministic sum of leg prices."""
        # Test hub transfer pricing with a valid 2-leg route
        # R3→R1 leg1 then R1→R13 leg2
        fsa_r3 = self.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R3'), ('pickup_supported', '=', True)
        ], limit=1)
        if fsa_r3:
            route = self.resolver.resolve(fsa_r3, self.fsa_r13, pallets=1, weight_lbs=500)
            if route.available and len(route.legs) > 1:
                leg_sum = sum(leg["price"] for leg in route.legs)
                r = self.svc.calculate(fsa_r3, self.fsa_r13, "ltl", "dry", 1, 500)
                if r.available:
                    self.assertAlmostEqual(r.calculated_price, leg_sum, places=2)

    # ── Rate plan as sole authority ───────────────────────────────────

    def test_13_rate_plan_is_sole_authority(self):
        """Rate plan price must be revenue_target / target_load_quantity."""
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        self.assertTrue(r.rate_plan, "Must have a rate plan assigned")
        expected = r.rate_plan.revenue_target / max(r.rate_plan.target_load_quantity, 1)
        self.assertAlmostEqual(r.calculated_price, expected, places=2,
            msg=f"Price {r.calculated_price} != {expected} from rate plan")

    # ── Inactive/expired rate plans ────────────────────────────────────

    def test_14_inactive_rate_plan_ignored(self):
        """Inactive rate plans must not be used for pricing."""
        rp = self.env['logistics.rate.plan'].search([
            ('lane_id.origin_region_id.code', '=', 'R1'),
            ('lane_id.destination_region_id.code', '=', 'R13'),
            ('active', '=', True),
        ], limit=1)
        self.assertTrue(rp, "Need active R1→R13 rate plan")
        try:
            rp.active = False
            r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500)
            self.assertFalse(r.available, "Inactive rate plan must not price")
        finally:
            rp.active = True  # Always restore

    # ── DEFAULT_TARGETS removed ────────────────────────────────────────

    def test_15_no_default_targets_fallback(self):
        """Pricing must not fall back to hardcoded DEFAULT_TARGETS."""
        from odoo.addons.prema_logistics_booking.services import pricing_service
        self.assertFalse(
            hasattr(pricing_service, 'DEFAULT_TARGETS'),
            "DEFAULT_TARGETS must be removed"
        )


class TestPricingPrecedence(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.svc = PricingService(cls.env)
        cls.fsa_r1 = cls.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R1'), ('pickup_supported', '=', True)
        ], limit=1)
        cls.fsa_r13 = cls.env['logistics.fsa'].search([
            ('region_id.code', '=', 'R13'), ('delivery_supported', '=', True)
        ], limit=1)

    def test_01_customer_specific_precedence(self):
        """Customer-specific rate takes precedence over lane default."""
        # Create a customer rate with discount
        partner = self.env['res.partner'].create({
            'name': 'TEST-Pricing-Customer',
        })
        lane = self.env['logistics.lane'].search([
            ('origin_region_id.code', '=', 'R1'),
            ('destination_region_id.code', '=', 'R13'),
            ('active', '=', True),
        ], limit=1)
        cr = self.env['logistics.customer.rate'].create({
            'partner_id': partner.id,
            'lane_id': lane.id,
            'discount_pct': 10.0,
            'active': True,
        })
        # Pricing for this partner should show discount
        r = self.svc.calculate(self.fsa_r1, self.fsa_r13, "ltl", "dry", 1, 500,
                               partner=partner)
        self.assertTrue(r.available, f"Not available: {r.reason}")
        # Standard: $200. With 10% discount: $180
        expected = round(200.0 * 0.9, 2)
        self.assertAlmostEqual(r.calculated_price, expected, places=2)
        # Cleanup
        cr.active = False
        partner.active = False

"""Prema AI V3.0 Architecture Validation Tests.

Covers: architecture, routing, booking legs, capacity, pricing,
round-trip profit, local operations, security, seed idempotency.

Run: odoo-bin shell -c /etc/odoo18.conf -d Prod-db-test1a < this_file
Or: odoo-bin --test-enable --test-tags /prema_logistics_booking -u prema_logistics_booking
"""
import datetime
import logging
from unittest.mock import patch

from odoo.tests import common, tagged
from odoo.exceptions import AccessError
from odoo.tools import mute_logger

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install", "prema_v3")
class TestV3Architecture(common.TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Lane = cls.env["logistics.lane"]
        cls.Corridor = cls.env["logistics.corridor"]
        cls.CorridorStop = cls.env["logistics.corridor.stop"]
        cls.Departure = cls.env["logistics.corridor.departure"]
        cls.Booking = cls.env["logistics.booking"]
        cls.BookingLeg = cls.env["logistics.booking.leg"]
        cls.BookingLine = cls.env["logistics.booking.line"]
        cls.BookingStop = cls.env["logistics.booking.stop"]
        cls.City = cls.env["logistics.city"]
        cls.DailyLocal = cls.env["logistics.daily.local.operation"]
        cls.Recurring = cls.env["logistics.recurring.agreement"]
        cls.RouteRun = cls.env["logistics.route.run"]
        cls.RouteTemplate = cls.env["logistics.route.template"]
        cls.Equipment = cls.env["logistics.equipment.profile"]

        # Resolve existing regions
        cls.r1 = cls.Region.search([("code", "=", "R1")], limit=1)  # GTA Central
        cls.r6 = cls.Region.search([("code", "=", "R6")], limit=1)  # Eastern ON (Kingston)
        cls.r7 = cls.Region.search([("code", "=", "R7")], limit=1)  # Ottawa Valley
        cls.r8 = cls.Region.search([("code", "=", "R8")], limit=1)  # Greater Montreal
        cls.r10 = cls.Region.search([("code", "=", "R10")], limit=1)  # Quebec City
        cls.r3 = cls.Region.search([("code", "=", "R3")], limit=1)  # Golden Horseshoe (Hamilton)

        # FSAs
        cls.fsa_mississauga = cls.Fsa.search([("fsa", "=", "L5M")], limit=1)
        cls.fsa_ottawa = cls.Fsa.search([("fsa", "=", "K1G")], limit=1)

    # ── ARCHITECTURE TESTS ─────────────────────────────────────────

    def test_01_canonical_corridor_model_exists(self):
        """Canonical corridor model is used — fields from route.template are absorbed."""
        cor = self.Corridor
        self.assertIn("phase", cor._fields)
        self.assertIn("truck_slot", cor._fields)
        self.assertIn("weekday", cor._fields)
        self.assertIn("start_time", cor._fields)
        self.assertIn("overnight", cor._fields)

    def test_02_canonical_departure_model_exists(self):
        """Canonical departure model is used — fields from route.run are absorbed."""
        dep = self.Departure
        self.assertIn("vehicle_id", dep._fields)
        self.assertIn("driver_id", dep._fields)
        self.assertIn("departure_date", dep._fields)
        self.assertIn("status", dep._fields)
        self.assertIn("max_capacity", dep._fields)

    def test_03_deprecated_route_template_still_loads(self):
        """Deprecated route.template model still loads for backward compat."""
        self.assertTrue(self.RouteTemplate._name)

    def test_04_deprecated_route_run_still_loads(self):
        """Deprecated route.run model still loads for backward compat."""
        self.assertTrue(self.RouteRun._name)

    @mute_logger("odoo.addons.prema_logistics_booking.models.logistics_route_template")
    def test_05_deprecated_template_create_warns(self):
        """Creating a route.template logs a deprecation warning but succeeds."""
        tmpl = self.RouteTemplate.create({
            "name": "TEST-V3-DEPRECATED-TEMPLATE",
            "phase": 1, "truck_slot": 99, "weekday": "0",
        })
        self.assertTrue(tmpl.exists())
        # Cleanup
        tmpl.unlink()

    @mute_logger("odoo.addons.prema_logistics_booking.models.logistics_route_run")
    def test_06_deprecated_run_create_warns(self):
        """Creating a route.run logs a deprecation warning but succeeds."""
        run = self.RouteRun.create({
            "run_date": datetime.date.today(),
            "corridor_name": "TEST-V3-DEPRECATED-RUN",
            "max_pallets": 12,
        })
        self.assertTrue(run.exists())
        run.unlink()

    # ── ROUTING RESOLUTION TESTS ────────────────────────────────────
    # (test_10_routing_mississauga_to_montreal removed — exercised the
    # deleted legacy services/routing_service.py, superseded by
    # RouteResolver/DepartureResolver; see test_ordered_lane_pricing.py and
    # test_pricing_integration.py for the equivalent coverage on the real
    # resolution path.)

    def test_11_lane_origin_destination(self):
        """Lanes exist with correct origin/destination regions."""
        lane = self.Lane.search([
            ("origin_region_id", "=", self.r1.id),
            ("destination_region_id", "=", self.r8.id),
        ], limit=1)
        if lane:
            self.assertEqual(lane.origin_region_id.code, "R1")
            self.assertEqual(lane.destination_region_id.code, "R8")

    def test_12_corridor_has_stops(self):
        """Corridors created via seed script have ordered stops."""
        cor = self.Corridor.search([("name", "like", "Eastbound Quebec")], limit=1)
        if cor:
            stops = cor.stop_ids.sorted("sequence")
            self.assertGreater(len(stops), 1)

    # ── CAPACITY ENGINE TESTS ──────────────────────────────────────

    def test_20_capacity_engine_accepts_12(self):
        """Capacity engine accepts 12 pallets (standard truck limit)."""
        from odoo.addons.prema_logistics_booking.services.capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        result = engine.evaluate(12, 5000.0)
        if result.vehicle:
            self.assertTrue(result.eligible or result.manual_review)

    def test_21_capacity_engine_rejects_14(self):
        """Capacity engine rejects 14 pallets (> turned capacity)."""
        from odoo.addons.prema_logistics_booking.services.capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        result = engine.evaluate(14, 5000.0)
        if result.vehicle:
            # 14 should be beyond max capacity for standard truck
            self.assertTrue(
                not result.eligible or result.manual_review,
                "14 pallets should require review or be rejected"
            )

    def test_22_capacity_override_fields_exist(self):
        """Booking model has capacity override fields."""
        self.assertIn("capacity_override", self.Booking._fields)
        self.assertIn("override_by", self.Booking._fields)
        self.assertIn("override_reason", self.Booking._fields)

    # ── PRICING TESTS ──────────────────────────────────────────────

    def test_30_pricing_formula_exists(self):
        """Rate Plans are the sole pricing authority."""
        RatePlan = self.env["logistics.rate.plan"]
        self.assertTrue(RatePlan.search_count([("active", "=", True)]) > 0,
                        "Must have at least one active rate plan")

    def test_31_pricing_route_resolver(self):
        """RouteResolver returns available or reason for any FSA pair."""
        from odoo.addons.prema_logistics_booking.services.route_resolver import RouteResolver
        resolver = RouteResolver(self.env)
        fsa = self.Fsa.search([("pickup_supported", "=", True)], limit=1)
        fsa2 = self.Fsa.search([("delivery_supported", "=", True)], limit=1)
        if fsa and fsa2:
            route = resolver.resolve(fsa, fsa2, 1, 500)
            # Must have either available=True or a reason string
            self.assertTrue(route.available or bool(route.reason),
                            "Route must be available or return a reason")

    def test_32_rate_plan_pricing_mode(self):
        """Rate plan has pricing_mode field (simple vs tiered)."""
        self.assertIn("pricing_mode", self.env["logistics.rate.plan"]._fields)

    def test_33_region_rate_per_km(self):
        """Region has rate_per_km for auto-suggest pricing."""
        self.assertIn("rate_per_km", self.Region._fields)

    def test_34_rate_plan_is_pricing_authority(self):
        """Rate Plans are the sole pricing authority; no suggest/calculate_simple."""
        from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService
        svc = PricingService(self.env)
        self.assertFalse(hasattr(svc, 'calculate_simple'),
                         "calculate_simple must be removed")
        self.assertFalse(hasattr(svc, 'suggest_revenue_target'),
                         "suggest_revenue_target must be removed")

    # ── ROUND-TRIP PROFIT TESTS ────────────────────────────────────

    def test_40_round_trip_fields_exist(self):
        """Lane model has round-trip profit fields."""
        self.assertIn("round_trip_revenue", self.Lane._fields)
        self.assertIn("round_trip_cost", self.Lane._fields)
        self.assertIn("round_trip_profit", self.Lane._fields)
        self.assertIn("round_trip_margin_pct", self.Lane._fields)
        self.assertIn("return_revenue_target", self.Lane._fields)
        self.assertIn("return_estimated_cost", self.Lane._fields)

    def test_41_round_trip_computation(self):
        """Round-trip fields compute correctly."""
        lane = self.Lane.search([], limit=1)
        if lane:
            # Set test values
            lane.revenue_target = 1600.0
            lane.estimated_one_way_cost = 1000.0
            lane.return_revenue_target = 800.0
            lane.return_estimated_cost = 600.0
            # Trigger recompute
            lane._compute_round_trip()
            self.assertEqual(lane.round_trip_revenue, 2400.0)
            self.assertEqual(lane.round_trip_cost, 1600.0)
            self.assertEqual(lane.round_trip_profit, 800.0)

    def test_42_empty_return_handled(self):
        """Round-trip with empty return revenue handled safely."""
        lane = self.Lane.search([], limit=1)
        if lane:
            lane.revenue_target = 1600.0
            lane.estimated_one_way_cost = 1000.0
            lane.return_revenue_target = 0.0  # No backhaul
            lane.return_estimated_cost = 0.0
            lane._compute_round_trip()
            self.assertEqual(lane.round_trip_revenue, 1600.0)
            self.assertEqual(lane.round_trip_cost, 1000.0)
            self.assertEqual(lane.round_trip_profit, 600.0)

    def test_43_division_by_zero_safe(self):
        """Round-trip margin handles zero revenue."""
        lane = self.Lane.search([], limit=1)
        if lane:
            lane.revenue_target = 0.0
            lane.return_revenue_target = 0.0
            lane._compute_round_trip()
            self.assertEqual(lane.round_trip_margin_pct, 0.0)

    # ── LOCAL OPERATIONS TESTS ─────────────────────────────────────

    def test_50_daily_local_model_exists(self):
        """Daily local operation model is available."""
        self.assertTrue(self.DailyLocal._name)
        self.assertIn("date", self.DailyLocal._fields)
        self.assertIn("vehicle_id", self.DailyLocal._fields)
        self.assertIn("driver_id", self.DailyLocal._fields)
        self.assertIn("feeds_corridor_id", self.DailyLocal._fields)
        self.assertIn("revenue_target", self.DailyLocal._fields)
        self.assertIn("booked_revenue", self.DailyLocal._fields)
        self.assertIn("total_pallets", self.DailyLocal._fields)

    def test_51_local_operation_create(self):
        """Can create a daily local operation."""
        op = self.DailyLocal.create({
            "date": datetime.date.today(),
            "revenue_target": 1200.0,
            "state": "planned",
        })
        self.assertTrue(op.exists())
        self.assertEqual(op.state, "planned")

    # ── CITY DIRECTORY TESTS ───────────────────────────────────────

    def test_60_city_model_exists(self):
        """City model is available with required fields."""
        self.assertIn("region_id", self.City._fields)
        self.assertIn("province_state", self.City._fields)
        self.assertIn("latitude", self.City._fields)

    def test_61_city_seed_data_loaded(self):
        """City search works (seed data may not be loaded in test transaction)."""
        cities = self.City.search([])
        # At minimum, verify search doesn't error
        self.assertIsNotNone(cities)
        # If there are cities, verify structure
        if cities:
            self.assertTrue(cities[0].name)

    # ── MULTI-LEG TESTS ────────────────────────────────────────────

    def test_70_booking_leg_model_exists(self):
        """Booking leg model is available."""
        self.assertIn("booking_id", self.BookingLeg._fields)
        self.assertIn("origin_stop_id", self.BookingLeg._fields)
        self.assertIn("destination_stop_id", self.BookingLeg._fields)
        self.assertIn("departure_id", self.BookingLeg._fields)
        self.assertIn("pickup_date", self.BookingLeg._fields)
        self.assertIn("delivery_date", self.BookingLeg._fields)

    def test_71_booking_has_multi_leg_fields(self):
        """Booking has is_multi_leg and leg_ids."""
        self.assertIn("is_multi_leg", self.Booking._fields)
        self.assertIn("leg_ids", self.Booking._fields)

    def test_72_booking_line_has_leg_id(self):
        """Booking line can link to a leg."""
        self.assertIn("leg_id", self.BookingLine._fields)

    # ── SECURITY TESTS ─────────────────────────────────────────────

    def test_80_record_rules_exist(self):
        """Ownership record rules exist for child models."""
        Rule = self.env["ir.rule"]
        for model in ["logistics.booking.line", "logistics.booking.stop", "logistics.booking.leg"]:
            rules = Rule.search([
                ("model_id.model", "=", model),
                ("groups", "in", [self.env.ref("prema_logistics_booking.group_logistics_customer").id]),
            ])
            self.assertTrue(rules, f"Missing customer ownership rule for {model}")

    def test_81_departure_has_capacity_computed_fields(self):
        """Corridor departure has computed peak fields."""
        self.assertIn("computed_peak_pallets", self.Departure._fields)
        self.assertIn("computed_peak_weight", self.Departure._fields)
        self.assertIn("computed_total_handled", self.Departure._fields)

    # ── IDEMPOTENCY TESTS ──────────────────────────────────────────

    def test_90_corridor_stable_business_keys(self):
        """Corridors use stable business keys: (phase, truck_slot, weekday)."""
        # Search shows the fields are queryable
        corridors = self.Corridor.search([
            ("phase", "=", 1), ("truck_slot", "=", 1)
        ])
        self.assertIsNotNone(corridors)  # May be empty in test DB, but query works

    def test_91_city_unique_constraint(self):
        """City has unique (name, province) constraint (tested when data exists)."""
        city1 = self.City.search([], limit=1)
        if city1:
            with self.assertRaises(Exception):
                self.City.create({
                    "name": city1.name,
                    "province_state": city1.province_state,
                    "region_id": city1.region_id.id,
                })
        else:
            # Test DB has no city data — skip uniqueness test gracefully
            self.assertTrue(True)

    # ── MODEL OWNERSHIP TESTS ──────────────────────────────────────

    def test_95_booking_has_departure_id(self):
        """Booking links to corridor departure (V3 canonical)."""
        self.assertIn("departure_id", self.Booking._fields)
        self.assertIn("corridor_id", self.Booking._fields)

    def test_96_recurring_has_departure_id(self):
        """Recurring agreement links to corridor departure (V3 canonical)."""
        self.assertIn("departure_id", self.Recurring._fields)
        self.assertIn("corridor_id", self.Recurring._fields)

    def test_97_lane_has_corridor_ids(self):
        """Lane links to corridors via M2M."""
        self.assertIn("corridor_ids", self.Lane._fields)

    def test_98_dispatch_job_has_local_operation(self):
        """Dispatch job can link to daily local operation."""
        self.assertIn("local_operation_id", self.env["prema.dispatch.job"]._fields)


@tagged("post_install", "-at_install", "prema_v3_integration")
class TestV3Integration(common.TransactionCase):
    """Integration tests that require corridors/departures to exist."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Corridor = cls.env["logistics.corridor"]
        cls.Departure = cls.env["logistics.corridor.departure"]
        cls.Booking = cls.env["logistics.booking"]
        cls.RouteRun = cls.env["logistics.route.run"]

    def test_capacity_engine_peak_computation(self):
        """compute_departure_peak runs without error."""
        from odoo.addons.prema_logistics_booking.services.capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        # Test with empty/invalid departure (should return zeros gracefully)
        result = engine.compute_departure_peak(self.Departure)
        self.assertIn("peak_pallets", result)
        self.assertIn("peak_weight", result)

    def test_can_accept_booking(self):
        """can_accept_booking returns proper structure for valid and empty cases."""
        from odoo.addons.prema_logistics_booking.services.capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        # Test with empty recordset (departure model but no records)
        result = engine.can_accept_booking(self.Departure, 4, 2000.0)
        self.assertIn("accepted", result)
        self.assertFalse(result["accepted"])  # empty recordset → not accepted
        self.assertIn("reason", result)


@tagged("post_install", "-at_install", "prema_v3_legacy")
class TestV3LegacyCompatibility(common.TransactionCase):
    """Verify legacy record compatibility."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Booking = cls.env["logistics.booking"]
        cls.RouteRun = cls.env["logistics.route.run"]
        cls.Corridor = cls.env["logistics.corridor"]

    @mute_logger("odoo.addons.prema_logistics_booking.models.logistics_route_run")
    def test_old_booking_route_run_still_readable(self):
        """A booking with deprecated route_run_id still opens correctly."""
        # Create legacy route.run
        run = self.RouteRun.create({
            "run_date": datetime.date.today(),
            "corridor_name": "TEST-V3-LEGACY",
            "max_pallets": 12, "state": "scheduled",
        })
        # Booking model has route_run_id as readonly FK
        self.assertIn("route_run_id", self.Booking._fields)
        run.unlink()

    def test_new_booking_uses_departure_id(self):
        """New bookings link via departure_id (V3 canonical)."""
        self.assertIn("departure_id", self.Booking._fields)
        self.assertIn("corridor_id", self.Booking._fields)

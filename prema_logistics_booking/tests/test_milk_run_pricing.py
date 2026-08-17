"""Milk-run route-level pricing — furthest served point (Manual-UAT Part 4).

Booking 185 (the primary test booking): United Dairy Brampton pickup →
Healthy Planet Belleville + McDonough's Ottawa + NOFRILLS Belleville
deliveries, 4 pallets / 2,000 lb.

The bug fixed here: movement_v1 quoted first pickup → LAST-ENTERED delivery
($397.93 — the wrong, Belleville-priced number). The policy now in force
for scheduled LTL: price at ROUTE LEVEL through the FURTHEST SERVED POINT
($647.19 — Ottawa). Both numbers must emerge from LIVE corridor
configuration — corridor segment distance, $/km, planned pallets and
volume tiers — never a hardcoded figure. The fixtures below mirror
Prod-db corridor 9 ('GTA → Ottawa → Montréal → Québec City (Eastbound)',
rate 3.0/8 pallets, tiers 10/15/30%, FTL threshold 10 auto_price, stop
charge 0) with its live segment distances: SEO (Belleville) 312.1 km,
Ottawa 507.6 km.
"""
import datetime

from odoo.tests import TransactionCase
from odoo.exceptions import UserError

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestMilkRunPricing(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        cls.country_ca = cls.env.ref("base.ca")
        cls.country_ca.logistics_network_enabled = True
        cls.state_on = cls.env["res.country.state"].search(
            [("country_id", "=", cls.country_ca.id), ("code", "=", "ON")], limit=1,
        )
        cls.state_on.logistics_network_enabled = True

        def _square(lng, lat, dx=0.1, dy=0.05):
            import json
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - dx, lat - dy], [lng + dx, lat - dy],
                [lng + dx, lat + dy], [lng - dx, lat + dy],
                [lng - dx, lat - dy],
            ]]})

        # Real Booking-185 coordinates:
        #   Brampton (43.755959, -79.692568), Belleville (44.183661, -77.394851),
        #   Ottawa (45.4215, -75.6972).
        cls.gta = Region.create({
            "code": "MRP-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id,
            "polygon_geojson": _square(-79.692568, 43.755959),
        })
        cls.seo = Region.create({
            "code": "MRP-SEO", "name": "Southeastern Ontario",
            "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id,
            "polygon_geojson": _square(-77.394851, 44.183661),
        })
        cls.ott = Region.create({
            "code": "MRP-OTT", "name": "Ottawa Region",
            "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id,
            "polygon_geojson": _square(-75.6972, 45.4215),
        })
        cls.env["logistics.hub"].create({
            "name": "MRP Hub", "public_name": "MRP Hub", "code": "MRP-HUB",
            "canonical_region_id": cls.gta.id, "is_default": True,
            "latitude": 43.755959, "longitude": -79.692568,
        })

        # Mirrors Prod-db corridor 9 exactly (live config read at quote
        # time — segment distances, rate, planned pallets, tiers, min).
        cls.pickup_date = datetime.date(2026, 8, 19)  # Wednesday
        day_field = "operate_%s" % cls.pickup_date.strftime("%A").lower()
        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "185-Eastbound",
            "direction": "eastbound",
            "rate_per_km": 3.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
            day_field: True,
            "enable_volume_discounts": True,
            "enable_ftl": True,
            "ftl_threshold_pallets": 10,
            "ftl_behavior": "auto_price",
            "ltl_additional_stop_charge": 0.0,
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.seo.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 312.1},
            {"corridor_id": cls.corridor.id, "sequence": 30, "region_id": cls.ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 507.6},
        ])
        cls.env["logistics.pallet.volume.tier"].create([
            {"corridor_id": cls.corridor.id, "min_pallets": 2, "max_pallets": 3,
             "discount_pct": 10.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 4, "max_pallets": 6,
             "discount_pct": 15.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 7, "max_pallets": 9,
             "discount_pct": 30.0, "pricing_type": "ltl"},
        ])

        cls.partner = cls.env["res.partner"].create({"name": "185 Customer"})

        # Booking-185 style route, in customer order: pickup Brampton →
        # Belleville → Ottawa → Belleville (NOFRILLS).
        cls.route_stops = [
            {"stop_type": "pickup", "stop_key": "PU-UD",
             "location_name": "United Dairy", "city": "Brampton",
             "latitude": 43.755959, "longitude": -79.692568,
             "pallets": 4, "weight_lbs": 2000.0},
            {"stop_type": "delivery", "stop_key": "D-HP",
             "location_name": "Healthy Planet", "city": "Belleville",
             "latitude": 44.183661, "longitude": -77.394851,
             "pallets": 1, "weight_lbs": 500.0},
            {"stop_type": "delivery", "stop_key": "D-MC",
             "location_name": "McDonough's", "city": "Ottawa",
             "latitude": 45.4215, "longitude": -75.6972,
             "pallets": 2, "weight_lbs": 1000.0},
            {"stop_type": "delivery", "stop_key": "D-NF",
             "location_name": "NOFRILLS 211 Bell Blvd", "city": "Belleville",
             "latitude": 44.1936, "longitude": -77.3930,
             "pallets": 1, "weight_lbs": 500.0},
        ]
        cls.movements = [
            {"pallet_id": "P-1", "pickup_stop_key": "PU-UD", "delivery_stop_key": "D-HP",
             "weight_lbs": 500.0},
            {"pallet_id": "P-2", "pickup_stop_key": "PU-UD", "delivery_stop_key": "D-MC",
             "weight_lbs": 500.0},
            {"pallet_id": "P-3", "pickup_stop_key": "PU-UD", "delivery_stop_key": "D-MC",
             "weight_lbs": 500.0},
            {"pallet_id": "P-4", "pickup_stop_key": "PU-UD", "delivery_stop_key": "D-NF",
             "weight_lbs": 500.0},
        ]

    # ── helpers ─────────────────────────────────────────────────────

    def _quote(self, route_stops=None, deliveries=None, pallets=4, weight=2000.0):
        svc = BookingOrchestrationService(self.env)
        if route_stops is None:
            route_stops = self.route_stops
        pickups = [s for s in route_stops if s["stop_type"] == "pickup"]
        if deliveries is None:
            deliveries = [s for s in route_stops if s["stop_type"] == "delivery"]
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": pickups,
            "delivery_stops": deliveries,
            "route_stops": route_stops,
            "route_model_version": "movement_v1",
            "pallet_movements": self.movements,
            "pallets": pallets, "physical_pallets": pallets,
            "weight_lbs": weight,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": self.pickup_date,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1)
        return result, session

    # ── Route-level pricing through the furthest served point ───────

    def test_01_movement_v1_prices_through_furthest_served_point(self):
        """Booking 185: Brampton → Belleville → Ottawa must price through
        Ottawa — 507.6 km × ($3.00/8 per pallet) × 4 pallets = $761.40,
        −15% volume tier (4-6) = $647.19. All from live corridor config."""
        result, session = self._quote()
        self.assertTrue(session)
        snapshot = session.route_snapshot or {}
        milk_run = snapshot.get("milk_run") or {}

        # Furthest served point is Ottawa — not the last-entered delivery
        # (which is a second Belleville stop).
        self.assertEqual(milk_run["furthest_stop_key"], "D-MC")
        self.assertEqual(milk_run["furthest_city"], "Ottawa")
        self.assertAlmostEqual(milk_run["furthest_billable_km"], 507.6, places=1)
        # Per-stop billable distances, in route order.
        self.assertEqual(
            [(e["stop_key"], e["billable_km"]) for e in milk_run["per_stop"]],
            [("D-HP", 312.1), ("D-MC", 507.6), ("D-NF", 312.1)])
        self.assertFalse(milk_run["backtracking_deliveries"])
        self.assertFalse(milk_run["unreachable_deliveries"])
        self.assertFalse(milk_run["manual_review_required"])
        self.assertEqual(milk_run["basis"], "route_level_furthest_point")

        # The quoted price: the corridor's own 4-6-pallet tier, min floor
        # untouched — never a hardcoded figure.
        self.assertAlmostEqual(result["calculated_price"], 647.19, places=2)
        self.assertAlmostEqual(session.calculated_price, 647.19, places=2)
        # The leg itself prices the furthest corridor segment.
        leg_line = [l for l in result["price_lines"] if l["label"].startswith("Leg")]
        self.assertEqual(len(leg_line), 1)
        self.assertAlmostEqual(leg_line[0]["distance_km"], 507.6, places=1)
        self.assertAlmostEqual(leg_line[0]["amount"], 761.40, places=2)
        discount_line = [l for l in result["price_lines"]
                         if l["label"].startswith("Volume discount")]
        self.assertEqual(len(discount_line), 1)
        self.assertAlmostEqual(discount_line[0]["amount"], -114.21, places=2)
        # Pricing basis recorded in the session's pricing snapshot.
        self.assertEqual((snapshot.get("pricing") or {}).get("basis"),
                         "route_level_furthest_point")

    def test_02_stop_cost_allocations_sum_exactly_to_subtotal(self):
        """Explanatory per-stop allocations decompose the authoritative
        discounted subtotal EXACTLY (no rounding drift), weighted by
        billable distance × pallets."""
        result, session = self._quote()
        allocations = [
            p for p in session.price_snapshot
            if isinstance(p, dict) and "_stop_cost_allocations" in p
        ]
        self.assertEqual(len(allocations), 1)
        allocs = allocations[0]["_stop_cost_allocations"]
        self.assertEqual(
            [a["stop_key"] for a in allocs], ["D-HP", "D-MC", "D-NF"])
        # Sum is EXACT (cents), equals the route subtotal.
        total = sum(a["amount"] for a in allocs)
        self.assertAlmostEqual(total, 647.19, places=2)
        self.assertAlmostEqual(total, round(total, 2), places=2)
        # Distance × pallets weighting: Ottawa (2 pallets, 507.6 km) is the
        # largest share; the two Belleville stops share the rest.
        by_key = {a["stop_key"]: a["amount"] for a in allocs}
        self.assertGreater(by_key["D-MC"], by_key["D-HP"] + by_key["D-NF"])
        self.assertAlmostEqual(
            by_key["D-HP"] + by_key["D-MC"] + by_key["D-NF"], 647.19, places=2)

    def test_03_legacy_first_to_last_pricing_unchanged(self):
        """Legacy delivery-only requests (no route_stops) keep the existing
        first-pickup → last-delivery corridor pricing — the fix is scoped to
        movement_v1 only."""
        svc = BookingOrchestrationService(self.env)
        deliveries = [self.route_stops[1], self.route_stops[2], self.route_stops[3]]
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [self.route_stops[0]],
            "delivery_stops": deliveries,
            "route_model_version": "legacy",
            "pallets": 4, "physical_pallets": 4,
            "weight_lbs": 2000.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": self.pickup_date,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        # Last-entered delivery is Belleville → 312.1 km × 0.375 × 4 =
        # 468.15 −15% = 397.93 (the pre-fix Booking 185 quote).
        self.assertAlmostEqual(result["calculated_price"], 397.93, places=2)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1)
        self.assertNotIn("milk_run", session.route_snapshot or {})

    # ── Backtracking / detour flags ─────────────────────────────────

    def test_04_backtracking_order_flagged_but_priced_route_level(self):
        """Ottawa entered before Belleville is a detour back toward the
        origin: flagged for manual review, never silently priced as a
        second route — the route still prices through the furthest point."""
        stops = [self.route_stops[0], self.route_stops[2], self.route_stops[1]]
        result, session = self._quote(route_stops=stops)
        milk_run = (session.route_snapshot or {}).get("milk_run") or {}
        self.assertTrue(milk_run["backtracking_deliveries"])
        self.assertEqual(
            milk_run["backtracking_deliveries"][0]["stop_key"], "D-HP")
        self.assertTrue(milk_run["manual_review_required"])
        self.assertTrue(any(
            "backtracking" in r for r in milk_run["manual_review_reasons"]))
        # Still prices through Ottawa.
        self.assertAlmostEqual(result["calculated_price"], 647.19, places=2)

    def test_05_unreachable_delivery_flagged_but_quote_proceeds(self):
        """A delivery outside every scheduled corridor is flagged for manual
        review; the quote still prices through the furthest REACHABLE point."""
        stops = [self.route_stops[0], self.route_stops[1],
                 dict(self.route_stops[2], city="Montréal",
                      latitude=45.5089, longitude=-73.5540)]  # no corridor
        result, session = self._quote(route_stops=stops)
        milk_run = (session.route_snapshot or {}).get("milk_run") or {}
        self.assertEqual(
            [e["stop_key"] for e in milk_run["unreachable_deliveries"]],
            ["D-MC"])
        self.assertTrue(milk_run["manual_review_required"])
        # Furthest reachable point: Healthy Planet Belleville.
        self.assertEqual(milk_run["furthest_stop_key"], "D-HP")
        self.assertAlmostEqual(result["calculated_price"],
                               312.1 * (3.0 / 8) * 4 * 0.85, places=2)
        self.assertAlmostEqual(result["calculated_price"], 397.93, places=2)

    def test_06_all_deliveries_unreachable_propagates_failure(self):
        """When NO delivery is reachable, the quote fails exactly like a
        single plan_route failure — customer-friendly message, no quote."""
        svc = BookingOrchestrationService(self.env)
        stops = [self.route_stops[0],
                 dict(self.route_stops[1], city="Mississauga",
                      latitude=43.5890, longitude=-79.6441),
                 dict(self.route_stops[2], city="Winnipeg",
                      latitude=49.8951, longitude=-97.1384)]
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [stops[0]],
            "delivery_stops": stops[1:],
            "route_stops": stops,
            "route_model_version": "movement_v1",
            "pallet_movements": self.movements,
            "pallets": 4, "physical_pallets": 4,
            "weight_lbs": 2000.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": self.pickup_date,
        }, source_channel="portal")
        with self.assertRaises(UserError):
            svc.prepare_quote(norm)

    # ── Pricing basis configuration ─────────────────────────────────

    def test_07_unimplemented_basis_flags_manual_review(self):
        """'segment_pallet_occupancy' is a FUTURE basis: configuring it must
        flag the quote for manual review, never silently switch pricing."""
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("prema_logistics_booking.pricing_basis",
                        "segment_pallet_occupancy")
        try:
            result, session = self._quote()
            milk_run = (session.route_snapshot or {}).get("milk_run") or {}
            self.assertEqual(milk_run["basis"], "segment_pallet_occupancy")
            self.assertTrue(milk_run["manual_review_required"])
            self.assertTrue(any(
                "not implemented" in r for r in milk_run["manual_review_reasons"]))
            # Still priced route-level at 647.19 (the flag is the guard).
            self.assertAlmostEqual(result["calculated_price"], 647.19, places=2)
        finally:
            Param.set_param("prema_logistics_booking.pricing_basis",
                            "route_level_furthest_point")

    def test_08_direct_service_asserts_expected_live_math(self):
        """The 647.19 expectation recomputed from LIVE corridor config —
        guards the fixture itself against silent drift."""
        rate = self.corridor.rate_per_km
        planned = self.corridor.planned_pallets
        seg = self.env["logistics.corridor.stop"].search([
            ("corridor_id", "=", self.corridor.id),
            ("region_id", "=", self.ott.id),
        ], limit=1)
        distance = seg.distance_from_origin_km
        tier = self.env["logistics.pallet.volume.tier"].get_discount_for_pallets(
            self.corridor.id, 4)
        expected = distance * (rate / planned) * 4 * (100.0 - tier) / 100.0
        self.assertAlmostEqual(expected, 647.19, places=2)

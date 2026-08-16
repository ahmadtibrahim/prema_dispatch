"""LTL routing/pricing fixes — direction-aware corridor selection, no
phantom same-region feeder, canonical segment distance, and the LTL
pallet-volume discount.

Reproduction fixture mirrors corridor 9:
    eastbound GTA 0 → ... → Ottawa 507.6 km, $3.50/km ÷ 8 planned pallets,
    volume tiers 2–3 = 5%, 4–6 = 10%, 7–9 = 15%.
"""
import datetime
import json

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.shipment_routing_service import (
    ShipmentRoutingService,
)


class TestLtlRoutingFixes(TransactionCase):

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

        def _square(lng, lat):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - 0.1, lat - 0.05], [lng + 0.1, lat - 0.05],
                [lng + 0.1, lat + 0.05], [lng - 0.1, lat + 0.05],
                [lng - 0.1, lat - 0.05],
            ]]})

        def _region(code, lng, lat):
            return Region.create({
                "code": code, "name": code, "is_official_ltl_region": True,
                "boundary_status": "approved", "country_id": cls.country_ca.id,
                "state_id": cls.state_on.id, "polygon_geojson": _square(lng, lat),
            })

        cls.gta = _region("LRF-GTA", -87.5, 49.0)
        cls.ott = _region("LRF-OTT", -86.5, 49.8)
        cls.feed = _region("LRF-FEED", -85.5, 49.0)
        cls.feed2 = _region("LRF-FEED2", -84.5, 49.8)
        cls.ott3 = _region("LRF-OTT3", -83.5, 49.0)

        cls.hub = cls.env["logistics.hub"].create({
            "name": "LRF Hub", "public_name": "LRF Hub", "code": "LRF-HUB",
            "canonical_region_id": cls.gta.id, "is_default": True,
            "latitude": 49.0, "longitude": -87.5,
        })

        Corridor = cls.env["logistics.corridor"]
        Stop = cls.env["logistics.corridor.stop"]

        # Westbound created FIRST (lower id) — a reverse corridor for
        # GTA → Ottawa; operates Tuesday AND Wednesday.
        cls.westbound = Corridor.create({
            "name": "LRF-Westbound", "direction": "westbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "operate_tuesday": True, "operate_wednesday": True,
        })
        Stop.create([
            {"corridor_id": cls.westbound.id, "sequence": 10, "region_id": cls.ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.westbound.id, "sequence": 20, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 971.8},
        ])

        # Eastbound — Wednesday only (mirrors corridor 9's day pattern).
        cls.eastbound = Corridor.create({
            "name": "LRF-Eastbound", "direction": "eastbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
        })
        Stop.create([
            {"corridor_id": cls.eastbound.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.eastbound.id, "sequence": 20, "region_id": cls.ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 507.6},
        ])
        Tier = cls.env["logistics.pallet.volume.tier"]
        Tier.create([
            {"corridor_id": cls.eastbound.id, "min_pallets": 2, "max_pallets": 3,
             "discount_pct": 5.0, "pricing_type": "ltl"},
            {"corridor_id": cls.eastbound.id, "min_pallets": 4, "max_pallets": 6,
             "discount_pct": 10.0, "pricing_type": "ltl"},
            {"corridor_id": cls.eastbound.id, "min_pallets": 7, "max_pallets": 9,
             "discount_pct": 15.0, "pricing_type": "ltl"},
        ])

        # Feeder corridor: FEED → GTA (outside-corridor origin).
        cls.feeder = Corridor.create({
            "name": "LRF-Feeder", "direction": "eastbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "operate_wednesday": True,
        })
        Stop.create([
            {"corridor_id": cls.feeder.id, "sequence": 10, "region_id": cls.feed.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.feeder.id, "sequence": 20, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 50.0},
        ])

        # Minimum-floor corridor: GTA → FEED2 (50 km), tiers on.
        cls.mincor = Corridor.create({
            "name": "LRF-Min", "direction": "eastbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "minimum_booking_charge": 150.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
        })
        Stop.create([
            {"corridor_id": cls.mincor.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.mincor.id, "sequence": 20, "region_id": cls.feed2.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 50.0},
        ])
        Tier.create({"corridor_id": cls.mincor.id, "min_pallets": 2, "max_pallets": 3,
                     "discount_pct": 5.0, "pricing_type": "ltl"})

        # Fallback corridor: pickup not allowed at origin → no canonical
        # segment → straight-line estimate must be used.
        cls.nofall = Corridor.create({
            "name": "LRF-NoFall", "direction": "eastbound",
            "rate_per_km": 3.5, "planned_pallets": 8,
            "operate_wednesday": True,
        })
        Stop.create([
            {"corridor_id": cls.nofall.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.nofall.id, "sequence": 20, "region_id": cls.ott3.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 90.0},
        ])

        # Departure on the eastbound corridor for the Wednesday pickup date
        # (needed by the two-leg feeder flow's onward-departure search).
        cls.wednesday = cls._wednesday()
        cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.eastbound.id,
            "departure_date": cls.wednesday,
            "departure_time": 7.0,
            "status": "scheduled",
            "max_capacity": 12,
        })

        cls.svc = ShipmentRoutingService(cls.env)

    @classmethod
    def _wednesday(cls):
        today = datetime.date.today()
        while today.strftime("%A").lower() != "wednesday":
            today += datetime.timedelta(days=1)
        return today

    def _plan(self, pickup, delivery, pallets=4, weight_lbs=2000.0, date=None):
        return self.svc.plan_route(
            pickup[0], pickup[1], delivery[0], delivery[1],
            pallets=pallets, weight_lbs=weight_lbs,
            requested_pickup_date=date or self.wednesday,
            equipment="dry",
        )

    # ── A/B: direction-aware corridor selection ─────────────────────

    def test_A_gtas_to_ottawa_never_uses_reverse_corridor(self):
        # Both corridors operate Wednesday; the westbound has the lower id
        # yet must be rejected for a GTA → Ottawa shipment.
        route = self._plan((49.0, -87.5), (49.8, -86.5))
        self.assertTrue(route.available, route.reason)
        self.assertEqual(route.legs[0].corridor_id, self.eastbound.id)
        self.assertEqual(route.legs[0].origin_region_code, "LRF-GTA")
        self.assertEqual(route.legs[0].dest_region_code, "LRF-OTT")

    def test_B_reverse_corridor_not_substituted_on_its_operating_day(self):
        # Tuesday: eastbound does not operate, westbound does. The reverse
        # corridor must NOT be substituted — the route is unavailable.
        tuesday = self.wednesday - datetime.timedelta(days=1)
        route = self._plan((49.0, -87.5), (49.8, -86.5), date=tuesday)
        self.assertFalse(route.available)

    # ── C/D: phantom feeder logic ───────────────────────────────────

    def test_C_same_region_pickup_generates_no_feeder_leg(self):
        route = self._plan((49.0, -87.5), (49.8, -86.5))
        self.assertTrue(route.available, route.reason)
        self.assertEqual(len(route.legs), 1)
        self.assertNotEqual(route.legs[0].leg_type, "feeder_to_hub")

    def test_D_outside_region_origin_keeps_feeder_leg(self):
        route = self._plan((49.0, -85.5), (49.8, -86.5))
        self.assertTrue(route.available, route.reason)
        self.assertEqual(len(route.legs), 2)
        self.assertEqual(route.legs[0].leg_type, "feeder_to_hub")
        self.assertEqual(route.legs[0].corridor_id, self.feeder.id)
        self.assertEqual(route.legs[1].corridor_id, self.eastbound.id)

    # ── E/F: distance source ────────────────────────────────────────

    def test_E_canonical_segment_distance_used(self):
        route = self._plan((49.0, -87.5), (49.8, -86.5))
        self.assertTrue(route.available, route.reason)
        self.assertAlmostEqual(route.legs[0].estimated_distance_km, 507.6, places=1)

    def test_F_straight_line_fallback_when_no_canonical_segment(self):
        route = self._plan((49.0, -87.5), (49.0, -83.5))
        self.assertTrue(route.available, route.reason)
        # No canonical segment (pickup not allowed at origin stop):
        # straight-line × 1.4 from the two polygon centers.
        import math
        pickup_lat, pickup_lng, delivery_lat, delivery_lng = 49.0, -87.5, 49.0, -83.5
        dx = (delivery_lng - pickup_lng) * 111.32 * math.cos(
            math.radians((pickup_lat + delivery_lat) / 2))
        dy = (delivery_lat - pickup_lat) * 111.32
        expected = math.sqrt(dx ** 2 + dy ** 2) * 1.4
        self.assertAlmostEqual(route.legs[0].estimated_distance_km, expected, places=1)

    # ── G/H/I/J: volume discount ────────────────────────────────────

    def test_G_four_pallets_receives_10_percent_discount(self):
        """The $915.13 reproduction: 507.6 km × $3.50 ÷ 8 × 4 = $888.30,
        × 0.90 = $799.47 — no feeder leg, canonical segment distance."""
        route = self._plan((49.0, -87.5), (49.8, -86.5), pallets=4, weight_lbs=2000.0)
        self.assertTrue(route.available, route.reason)
        self.assertEqual(len(route.legs), 1)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["leg_total"], 888.30, places=2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 10.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 799.47, places=2)

    def test_H_two_pallets_receives_5_percent_discount(self):
        route = self._plan((49.0, -87.5), (49.8, -86.5), pallets=2, weight_lbs=1000.0)
        self.assertTrue(route.available, route.reason)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["leg_total"], 444.15, places=2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 5.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 421.94, places=2)

    def test_I_seven_pallets_receives_15_percent_discount(self):
        route = self._plan((49.0, -87.5), (49.8, -86.5), pallets=7, weight_lbs=3500.0)
        self.assertTrue(route.available, route.reason)
        pricing = route.routing_snapshot["pricing"]
        expected = round(pricing["leg_total"] * 0.85, 2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 15.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], expected, places=2)

    def test_J_volume_discounts_disabled_no_discount(self):
        self.eastbound.enable_volume_discounts = False
        route = self._plan((49.0, -87.5), (49.8, -86.5), pallets=4, weight_lbs=2000.0)
        self.assertTrue(route.available, route.reason)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["volume_discount_pct"], 0.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 888.30, places=2)

    # ── K: booking minimum remains a floor only ─────────────────────

    def test_K_booking_minimum_is_floor_only(self):
        route = self._plan((49.0, -87.5), (49.8, -84.5), pallets=2, weight_lbs=1000.0)
        self.assertTrue(route.available, route.reason)
        pricing = route.routing_snapshot["pricing"]
        # 50 km × $0.4375 × 2 = 43.75 → 5% → 41.56 → floored at 150,
        # never 150 + 41.56.
        self.assertAlmostEqual(pricing["leg_total"], 43.75, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 150.00, places=2)

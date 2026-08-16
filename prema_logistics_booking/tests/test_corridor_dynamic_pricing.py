"""Configuration-driven pricing — every corridor prices a shipment from
its OWN configuration (rate, planned-pallet divisor, volume tiers,
minimum charge). No corridor id, pallet range, percentage, or rate may be
hardcoded in the engine.

Corridor A: $3.00/km ÷ 8, min $250, tiers 2–3=10% / 4–6=20% / 7–9=30%
Corridor B: $4.00/km ÷ 10, min $100, tiers 2–4=5% / 5–7=15% / 8–9=25%
"""
import datetime
import json

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.shipment_routing_service import (
    ShipmentRoutingService,
)


class TestCorridorDynamicPricing(TransactionCase):

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
                [lng - 0.05, lat - 0.04], [lng + 0.05, lat - 0.04],
                [lng + 0.05, lat + 0.04], [lng - 0.05, lat + 0.04],
                [lng - 0.05, lat - 0.04],
            ]]})

        def _region(code, lng, lat):
            return Region.create({
                "code": code, "name": code, "is_official_ltl_region": True,
                "boundary_status": "approved", "country_id": cls.country_ca.id,
                "state_id": cls.state_on.id, "polygon_geojson": _square(lng, lat),
            })

        cls.a1 = _region("CDP-A1", -87.5, 49.0)
        cls.a2 = _region("CDP-A2", -87.0, 49.5)
        cls.b1 = _region("CDP-B1", -86.0, 49.0)
        cls.b2 = _region("CDP-B2", -85.5, 49.5)

        Corridor = cls.env["logistics.corridor"]
        Stop = cls.env["logistics.corridor.stop"]
        Tier = cls.env["logistics.pallet.volume.tier"]
        Rule = cls.env["logistics.direct.delivery.rule"]

        def _corridor(name, origin, dest, rate, planned, minimum, tiers, dist):
            corridor = Corridor.create({
                "name": name, "direction": "eastbound",
                "rate_per_km": rate, "planned_pallets": planned,
                "minimum_booking_charge": minimum,
                "operate_wednesday": True,
                "enable_volume_discounts": True,
            })
            Stop.create([
                {"corridor_id": corridor.id, "sequence": 10, "region_id": origin.id,
                 "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
                {"corridor_id": corridor.id, "sequence": 20, "region_id": dest.id,
                 "pickup_allowed": True, "delivery_allowed": True,
                 "distance_from_origin_km": dist},
            ])
            for low, high, pct in tiers:
                Tier.create({"corridor_id": corridor.id, "min_pallets": low,
                             "max_pallets": high, "discount_pct": pct,
                             "pricing_type": "ltl"})
            Rule.create({
                "origin_region_id": origin.id, "destination_region_id": dest.id,
                "applicable_corridor_id": corridor.id,
                "direct_same_day_allowed": True, "hub_transfer_required": False,
            })
            return corridor

        cls.corridor_a = _corridor(
            "CDP-Corridor-A", cls.a1, cls.a2,
            rate=3.0, planned=8, minimum=250.0,
            tiers=[(2, 3, 10.0), (4, 6, 20.0), (7, 9, 30.0)], dist=300.0,
        )
        cls.corridor_b = _corridor(
            "CDP-Corridor-B", cls.b1, cls.b2,
            rate=4.0, planned=10, minimum=100.0,
            tiers=[(2, 4, 5.0), (5, 7, 15.0), (8, 9, 25.0)], dist=400.0,
        )

        cls.svc = ShipmentRoutingService(cls.env)
        cls.wednesday = cls._next("wednesday")

    @classmethod
    def _next(cls, weekday):
        day = datetime.date.today()
        while day.strftime("%A").lower() != weekday:
            day += datetime.timedelta(days=1)
        return day

    def _quote(self, pickup, delivery, pallets):
        route = self.svc.plan_route(
            pickup[0], pickup[1], delivery[0], delivery[1],
            pallets=pallets, weight_lbs=pallets * 500.0,
            requested_pickup_date=self.wednesday, equipment="dry",
        )
        self.assertTrue(route.available, route.reason)
        return route

    def test_01_corridor_a_uses_its_own_configuration(self):
        # 2 pallets: 300 × (3.0 ÷ 8) × 2 = 225.00 → 10% = 202.50
        #            → floored at corridor A's OWN minimum $250.
        route = self._quote((49.0, -87.5), (49.5, -87.0), 2)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["leg_total"], 225.00, places=2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 10.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 250.00, places=2)

        # 4 pallets: 300 × 0.375 × 4 = 450.00 → 20% = 360.00.
        route = self._quote((49.0, -87.5), (49.5, -87.0), 4)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["final_transportation"], 360.00, places=2)

        # 7 pallets: 787.50 → 30% = 551.25.
        route = self._quote((49.0, -87.5), (49.5, -87.0), 7)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["final_transportation"], 551.25, places=2)

    def test_02_corridor_b_uses_its_own_configuration(self):
        # 4 pallets fall in B's 2–4 tier (5%), NOT A's 4–6 tier (20%):
        # 400 × (4.0 ÷ 10) × 4 = 640.00 → 5% = 608.00.
        route = self._quote((49.0, -86.0), (49.5, -85.5), 4)
        pricing = route.routing_snapshot["pricing"]
        self.assertEqual(route.legs[0].corridor_id, self.corridor_b.id)
        self.assertAlmostEqual(route.legs[0].pallet_rate_per_km, 0.4, places=4)
        self.assertAlmostEqual(pricing["leg_total"], 640.00, places=2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 5.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 608.00, places=2)

        # 8 pallets: 1,280.00 → 25% = 960.00.
        route = self._quote((49.0, -86.0), (49.5, -85.5), 8)
        pricing = route.routing_snapshot["pricing"]
        self.assertAlmostEqual(pricing["volume_discount_pct"], 25.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 960.00, places=2)

    def test_03_changing_corridor_b_never_affects_corridor_a(self):
        before = self._quote((49.0, -87.5), (49.5, -87.0), 4)
        before_final = before.routing_snapshot["pricing"]["final_transportation"]
        self.assertAlmostEqual(before_final, 360.00, places=2)

        # Reconfigure B entirely: different rate, divisor, tiers, minimum.
        self.corridor_b.write({
            "rate_per_km": 9.99, "planned_pallets": 20, "minimum_booking_charge": 999.0,
        })
        tier = self.env["logistics.pallet.volume.tier"].search(
            [("corridor_id", "=", self.corridor_b.id)], limit=1)
        tier.discount_pct = 99.0

        after = self._quote((49.0, -87.5), (49.5, -87.0), 4)
        after_final = after.routing_snapshot["pricing"]["final_transportation"]
        self.assertAlmostEqual(after_final, before_final, places=2)

    def test_04_newly_created_corridor_prices_immediately(self):
        """A corridor created after module installation, with its own custom
        tiers, prices correctly with zero code changes."""
        Region = self.env["logistics.region"]
        c1 = Region.create({
            "code": "CDP-C1", "name": "CDP-C1", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": self.country_ca.id,
            "state_id": self.state_on.id,
            "polygon_geojson": json.dumps({"type": "Polygon", "coordinates": [[
                [-84.55, 48.96], [-84.45, 48.96], [-84.45, 49.04],
                [-84.55, 49.04], [-84.55, 48.96]]]}),
        })
        c2 = Region.create({
            "code": "CDP-C2", "name": "CDP-C2", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": self.country_ca.id,
            "state_id": self.state_on.id,
            "polygon_geojson": json.dumps({"type": "Polygon", "coordinates": [[
                [-84.05, 49.46], [-83.95, 49.46], [-83.95, 49.54],
                [-84.05, 49.54], [-84.05, 49.46]]]}),
        })
        fresh = self.env["logistics.corridor"].create({
            "name": "CDP-Fresh", "direction": "eastbound",
            "rate_per_km": 2.5, "planned_pallets": 6,
            "minimum_booking_charge": 50.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
        })
        self.env["logistics.corridor.stop"].create([
            {"corridor_id": fresh.id, "sequence": 10, "region_id": c1.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": fresh.id, "sequence": 20, "region_id": c2.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 120.0},
        ])
        self.env["logistics.pallet.volume.tier"].create([
            {"corridor_id": fresh.id, "min_pallets": 2, "max_pallets": 3,
             "discount_pct": 7.0, "pricing_type": "ltl"},
            {"corridor_id": fresh.id, "min_pallets": 4, "max_pallets": 5,
             "discount_pct": 11.0, "pricing_type": "ltl"},
        ])
        self.env["logistics.direct.delivery.rule"].create({
            "origin_region_id": c1.id, "destination_region_id": c2.id,
            "applicable_corridor_id": fresh.id,
            "direct_same_day_allowed": True, "hub_transfer_required": False,
        })

        route = self.svc.plan_route(
            49.0, -84.5, 49.5, -84.0,
            pallets=2, weight_lbs=1000.0,
            requested_pickup_date=self.wednesday, equipment="dry",
        )
        self.assertTrue(route.available, route.reason)
        pricing = route.routing_snapshot["pricing"]
        # 120 × (2.5 ÷ 6) × 2 = 100.00 → 7% = 93.00 (own tier, no deployment).
        self.assertEqual(route.legs[0].corridor_id, fresh.id)
        self.assertAlmostEqual(pricing["leg_total"], 100.00, places=2)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 7.0, places=2)
        self.assertAlmostEqual(pricing["final_transportation"], 93.00, places=2)

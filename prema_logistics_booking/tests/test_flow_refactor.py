from odoo.tests import common

from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService
from odoo.addons.prema_logistics_booking.services.route_resolver import RouteResolver


class TestFullFlowRefactor(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Region = cls.env["logistics.region"]
        Fsa = cls.env["logistics.fsa"]
        Corridor = cls.env["logistics.corridor"]
        Stop = cls.env["logistics.corridor.stop"]

        cls.origin = Region.create({"code": "RF-O", "name": "Refactor Origin"})
        cls.hub_region = Region.create({"code": "RF-H", "name": "Refactor Hub"})
        cls.destination = Region.create({"code": "RF-D", "name": "Refactor Destination"})
        cls.direct_dest = Region.create({"code": "RF-X", "name": "Refactor Direct Destination"})

        cls.fsa_origin = Fsa.create({
            "fsa": "R1A", "region_id": cls.origin.id,
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.fsa_direct = Fsa.create({
            "fsa": "R2A", "region_id": cls.direct_dest.id,
            "pickup_supported": True, "delivery_supported": True,
        })

        cls.direct = Corridor.create({
            "name": "RF Direct", "direction": "eastbound", "equipment_type": "dry",
            "rate_per_km": 4.0, "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "enable_volume_discounts": True,
            "excess_weight_rate_per_lb": 0.10,
            "enable_ftl": True, "ftl_threshold_pallets": 10,
            "ftl_rate_per_km": 3.0, "ftl_minimum_charge": 0.0,
            "ftl_behavior": "auto_price",
        })
        Stop.create([
            {"corridor_id": cls.direct.id, "sequence": 10, "region_id": cls.origin.id,
             "pickup_allowed": True, "delivery_allowed": False, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.direct.id, "sequence": 20, "region_id": cls.direct_dest.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 100.0},
        ])
        cls.env["logistics.pallet.volume.tier"].create({
            "corridor_id": cls.direct.id,
            "min_pallets": 2, "max_pallets": 3,
            "discount_pct": 10.0, "pricing_type": "ltl",
        })

        cls.hub = cls.env["logistics.hub"].create({
            "name": "RF Hub", "public_name": "RF Hub",
            "code": "RF-HUB", "canonical_region_id": cls.hub_region.id,
            "is_default": True,
        })
        cls.feeder = Corridor.create({
            "name": "RF Feeder", "direction": "local", "equipment_type": "dry",
            "rate_per_km": 3.0, "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
        })
        cls.mainline = Corridor.create({
            "name": "RF Mainline", "direction": "eastbound", "equipment_type": "dry",
            "rate_per_km": 4.0, "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
        })
        Stop.create([
            {"corridor_id": cls.feeder.id, "sequence": 10, "region_id": cls.origin.id,
             "pickup_allowed": True, "delivery_allowed": False, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.feeder.id, "sequence": 20, "region_id": cls.hub_region.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 70.0},
            {"corridor_id": cls.mainline.id, "sequence": 10, "region_id": cls.hub_region.id,
             "pickup_allowed": True, "delivery_allowed": False, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.mainline.id, "sequence": 20, "region_id": cls.destination.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 500.0},
        ])

    def test_volume_discount_then_one_time_excess_weight(self):
        result = PricingService(self.env).calculate(
            self.fsa_origin, self.fsa_direct, "ltl", "dry", 2, 1200,
        )
        self.assertTrue(result.available)
        # Base = 100 km * 2 * (4/8) = 100
        # 10% volume discount = -10
        # Excess = 1200 - (2*500) = 200 lb * .10 = 20
        self.assertAlmostEqual(result.calculated_price, 110.0, places=2)
        self.assertAlmostEqual(result.route_snapshot["excess_weight_charge"], 20.0, places=2)

    def test_ftl_uses_dedicated_direct_rate(self):
        result = PricingService(self.env).calculate(
            self.fsa_origin, self.fsa_direct, "ftl", "dry", 10, 5000,
        )
        self.assertTrue(result.available)
        self.assertEqual(result.route_snapshot["shipment_type"], "ftl")
        self.assertAlmostEqual(result.calculated_price, 300.0, places=2)

    def test_transfer_requires_explicit_service_connection(self):
        resolver = RouteResolver(self.env)
        before = resolver.resolve_regions(self.origin, self.destination)
        self.assertFalse(before.available)
        self.assertEqual(before.reason, "no_configured_corridor_connection")

        self.feeder.write({"connected_corridor_ids": [(4, self.mainline.id)]})
        after = resolver.resolve_regions(self.origin, self.destination)
        self.assertTrue(after.available)
        self.assertEqual(len(after.legs), 2)
        self.assertEqual(after.legs[0]["corridor_id"], self.feeder.id)
        self.assertEqual(after.legs[1]["corridor_id"], self.mainline.id)

    def test_dispatch_item_has_canonical_booking_operation_fields(self):
        Item = self.env["prema.dispatch.item"]
        self.assertIn("logistics_booking_id", Item._fields)
        self.assertIn("operation_job_ids", Item._fields)

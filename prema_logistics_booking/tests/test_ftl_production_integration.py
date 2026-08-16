"""FTL Regional Pricing — production integration tests.

The booking pricing flow must call the corridor's compute_ftl_price()
regional pricing (flat rate / per km / corridor default) whenever a
shipment is treated as FTL.

Fixtures:
    corridor1: GTA 0 → York 33.5 → Northumberland 130 → Québec City 300
               → Ottawa 450 → Montérégie 600 (LTL $4.00/km ÷ 8 pallets)
    corridor2: Ottawa → Montérégie (210 km)
"""
import datetime
import json

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService
from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestFtlProductionIntegration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        # Network-enabled country/province for the region resolver's
        # polygon path (used only by the route-planner test).
        cls.country_ca = cls.env.ref("base.ca")
        cls.country_ca.logistics_network_enabled = True
        cls.state_on = cls.env["res.country.state"].search(
            [("country_id", "=", cls.country_ca.id), ("code", "=", "ON")], limit=1,
        )
        cls.state_on.logistics_network_enabled = True

        # GTA / Northumberland carry approved polygons around remote
        # northern-Ontario coordinates so the region resolver matches them
        # without colliding with production polygons.
        def _square(lng, lat):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - 0.1, lat - 0.05], [lng + 0.1, lat - 0.05],
                [lng + 0.1, lat + 0.05], [lng - 0.1, lat + 0.05],
                [lng - 0.1, lat - 0.05],
            ]]})

        cls.gta = Region.create({
            "code": "FTI-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-87.5, 49.0),
        })
        cls.northumberland = Region.create({
            "code": "FTI-NOR", "name": "Northumberland", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-86.5, 49.8),
        })
        cls.ottawa = Region.create({"code": "FTI-OTT", "name": "Ottawa", "is_official_ltl_region": True})
        cls.monteregie = Region.create({"code": "FTI-MON", "name": "Montérégie", "is_official_ltl_region": True})
        cls.quebec = Region.create({"code": "FTI-QUE", "name": "Québec City", "is_official_ltl_region": True})
        cls.york = Region.create({"code": "FTI-YRK", "name": "York", "is_official_ltl_region": True})

        def _fsa(code, region):
            return cls.env["logistics.fsa"].create({
                "fsa": code, "region_id": region.id,
                "pickup_supported": True, "delivery_supported": True,
            })

        cls.fsa_gta = _fsa("F1A", cls.gta)
        cls.fsa_nor = _fsa("F1B", cls.northumberland)
        cls.fsa_ott = _fsa("F1C", cls.ottawa)
        cls.fsa_mon = _fsa("F1D", cls.monteregie)
        cls.fsa_que = _fsa("F1E", cls.quebec)
        cls.fsa_yrk = _fsa("F1F", cls.york)

        # ── Corridor 1: GTA → York → Northumberland → Québec → Ottawa → Montérégie ──
        cls.corridor1 = cls.env["logistics.corridor"].create({
            "name": "FTI-East",
            "direction": "eastbound",
            "rate_per_km": 4.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "enable_ftl": True,
            "ftl_threshold_pallets": 10,
            "ftl_rate_per_km": 3.0,
            "ftl_minimum_charge": 750.0,  # legacy — must never participate
            "ftl_reserve_entire_truck": True,
            "ftl_behavior": "auto_price",
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor1.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor1.id, "sequence": 20, "region_id": cls.york.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 33.5},
            {"corridor_id": cls.corridor1.id, "sequence": 30, "region_id": cls.northumberland.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 130.0},
            {"corridor_id": cls.corridor1.id, "sequence": 40, "region_id": cls.quebec.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 300.0},
            {"corridor_id": cls.corridor1.id, "sequence": 50, "region_id": cls.ottawa.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 450.0},
            {"corridor_id": cls.corridor1.id, "sequence": 60, "region_id": cls.monteregie.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 600.0},
        ])
        cls.rule_flat = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor1.id,
            "origin_region_id": cls.gta.id,
            "destination_region_id": cls.northumberland.id,
            "pricing_type": "flat_rate",
            "flat_rate": 550.0,
        })
        cls.rule_per_km = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor1.id,
            "origin_region_id": cls.gta.id,
            "destination_region_id": cls.ottawa.id,
            "pricing_type": "per_km",
            "ftl_rate_per_km_override": 3.00,
        })
        cls.rule_default = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor1.id,
            "origin_region_id": cls.gta.id,
            "destination_region_id": cls.quebec.id,
            "pricing_type": "corridor_default",
            "ftl_rate_per_km_override": 9.99,  # must be ignored
        })

        # ── Corridor 2: Ottawa → Montérégie (210 km), per-km override ──
        cls.corridor2 = cls.env["logistics.corridor"].create({
            "name": "FTI-OttMon",
            "direction": "eastbound",
            "rate_per_km": 4.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "enable_ftl": True,
            "ftl_threshold_pallets": 10,
            "ftl_rate_per_km": 3.0,
            "ftl_behavior": "auto_price",
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor2.id, "sequence": 10, "region_id": cls.ottawa.id,
             "pickup_allowed": True, "delivery_allowed": False, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor2.id, "sequence": 20, "region_id": cls.monteregie.id,
             "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 210.0},
        ])
        cls.rule_ott_mon = cls.env["logistics.ftl.regional.minimum"].create({
            "corridor_id": cls.corridor2.id,
            "origin_region_id": cls.ottawa.id,
            "destination_region_id": cls.monteregie.id,
            "pricing_type": "per_km",
            "ftl_rate_per_km_override": 3.25,
        })

        cls.svc = PricingService(cls.env)

    # ── FTL regional pricing through the production pricing service ───

    def test_01_flat_rate_rule_returns_exact_flat_rate(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        self.assertTrue(result.route_snapshot.get("ftl_priced"))
        # 130 km segment — flat rate $550 regardless of distance.
        self.assertAlmostEqual(result.calculated_price, 550.00, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertEqual(formula["pricing_method"], "ftl_regional_minimum")
        self.assertEqual(formula["pricing_type"], "flat_rate")
        self.assertAlmostEqual(formula["flat_rate"], 550.00, places=2)
        self.assertEqual(formula["regional_rule_id"], self.rule_flat.id)

    def test_02_per_km_rule_uses_regional_rate(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_ott, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        # 450 km × $3.00 = $1,350.
        self.assertAlmostEqual(result.calculated_price, 1350.00, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertEqual(formula["pricing_type"], "per_km")
        self.assertAlmostEqual(formula["rate_per_km"], 3.00, places=6)
        self.assertAlmostEqual(formula["distance_price"], 1350.00, places=2)
        self.assertEqual(formula["regional_rule_id"], self.rule_per_km.id)

    def test_03_regional_per_km_override_used(self):
        result = self.svc.calculate(self.fsa_ott, self.fsa_mon, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        # 210 km × $3.25 = $682.50 — no minimum floor in the new model.
        self.assertAlmostEqual(result.calculated_price, 682.50, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertEqual(formula["pricing_type"], "per_km")
        self.assertAlmostEqual(formula["rate_per_km"], 3.25, places=6)
        self.assertAlmostEqual(formula["distance_price"], 682.50, places=2)
        self.assertEqual(formula["regional_rule_id"], self.rule_ott_mon.id)

    def test_04_corridor_default_rule_uses_corridor_rate(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_que, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        # 300 km × corridor $3.00 = $900 — the rule's 9.99 override ignored.
        self.assertAlmostEqual(result.calculated_price, 900.00, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertEqual(formula["pricing_type"], "corridor_default")
        self.assertAlmostEqual(formula["rate_per_km"], 3.00, places=6)
        self.assertAlmostEqual(formula["distance_price"], 900.00, places=2)
        self.assertEqual(formula["regional_rule_id"], self.rule_default.id)

    def test_05_no_matching_rule_uses_distance_times_corridor_rate(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_mon, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        # 600 km × $3.00 = $1,800 with no regional rule.
        self.assertAlmostEqual(result.calculated_price, 1800.00, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertFalse(formula["regional_rule_id"])
        self.assertEqual(formula["pricing_type"], "corridor_default")
        self.assertAlmostEqual(formula["distance_price"], 1800.00, places=2)

    def test_06_legacy_minimum_charge_never_read(self):
        # Rule-level legacy value must not floor or cap the flat rate.
        self.rule_flat.minimum_ftl_charge = 9999.0
        flat = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ftl", "dry", 5, 2500)
        self.assertTrue(flat.available, flat.reason)
        self.assertAlmostEqual(flat.calculated_price, 550.00, places=2)

        # Corridor-level legacy $750 must not floor a small ruleless segment.
        self.assertEqual(self.corridor1.ftl_minimum_charge, 750.0)
        no_rule = self.svc.calculate(self.fsa_gta, self.fsa_yrk, "ftl", "dry", 5, 2500)
        self.assertTrue(no_rule.available, no_rule.reason)
        # 33.5 km × $3.00 = $100.50 — no legacy floor to $750.
        self.assertAlmostEqual(no_rule.calculated_price, 100.50, places=2)

    def test_07_customer_contracted_price_overrides_ftl(self):
        """agreed_rate wins over the FTL engine result in the production
        confirm_from_internal flow."""
        partner = self.env["res.partner"].create({"name": "FTI-Customer"})
        brand = self.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            self.env["fleet.vehicle.model.brand"].create({"name": "FTI-Brand"})
        model = self.env["fleet.vehicle.model"].create({"name": "FTI-Model", "brand_id": brand.id})
        vehicle = self.env["fleet.vehicle"].create({
            "name": "FTI-Truck", "license_plate": "FTITEST", "model_id": model.id,
            "x_operational_logistics": True, "x_max_payload_lbs": 20000.0,
            "straight_pallet_capacity": 12, "pin_wheel_pallet_capacity": 13,
        })
        self.env["logistics.corridor.departure"].create({
            "corridor_id": self.corridor1.id,
            "departure_date": datetime.date.today() + datetime.timedelta(days=1),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": vehicle.id,
            "max_capacity": 12,
        })

        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": partner.id,
            "pickup_stops": [{"postal_code": "F1A"}],
            "delivery_stops": [{"postal_code": "F1B"}],
            "pallets": 5,
            "weight_lbs": 2500,
            "load_type": "ftl",
            "equipment_type": "dry",
            "pricing_method": "contract",
            "agreed_rate": 1500.0,
        }, source_channel="internal")
        booking = svc.confirm_from_internal(norm, skip_invoice=True)

        # The engine calculated the FTL regional flat rate...
        self.assertEqual(booking.shipment_type, "ftl")
        engine_leg = booking.route_snapshot["legs"][0]
        self.assertAlmostEqual(engine_leg["price"], 550.00, places=2)
        # ...but the customer's contracted price wins.
        self.assertAlmostEqual(booking.calculated_price, 1500.00, places=2)

    # ── LTL and threshold semantics remain unchanged ─────────────────

    def test_08_ltl_booking_unchanged(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ltl", "dry", 4, 2000)
        self.assertTrue(result.available, result.reason)
        self.assertFalse(result.route_snapshot.get("ftl_priced"))
        # LTL formula: 130 km × 4 pallets × ($4.00 ÷ 8) = $260.
        self.assertAlmostEqual(result.calculated_price, 260.00, places=2)

    def test_09_below_threshold_remains_ltl(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ltl", "dry", 9, 4500)
        self.assertTrue(result.available, result.reason)
        self.assertFalse(result.route_snapshot.get("ftl_priced"))
        self.assertFalse(result.recommend_ftl)
        # LTL formula: 130 km × 9 pallets × ($4.00 ÷ 8) = $585.
        self.assertAlmostEqual(result.calculated_price, 585.00, places=2)

    def test_10_at_threshold_follows_ftl_behavior(self):
        # auto_price → FTL pricing kicks in at the threshold.
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ltl", "dry", 10, 5000)
        self.assertTrue(result.available, result.reason)
        self.assertTrue(result.route_snapshot.get("ftl_priced"))
        self.assertAlmostEqual(result.calculated_price, 550.00, places=2)

        # recommend → price stays LTL, quote carries the recommendation.
        self.corridor1.ftl_behavior = "recommend"
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ltl", "dry", 10, 5000)
        self.assertTrue(result.available, result.reason)
        self.assertFalse(result.route_snapshot.get("ftl_priced"))
        self.assertTrue(result.recommend_ftl)
        self.assertAlmostEqual(result.calculated_price, 650.00, places=2)

        # dispatcher_approval → blocked for manual review.
        self.corridor1.ftl_behavior = "dispatcher_approval"
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ltl", "dry", 10, 5000)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "ftl_dispatcher_approval_required")

    def test_11_inactive_regional_rule_ignored(self):
        self.rule_flat.active = False
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        # No active rule → no flat rate: 130 × $3.00 = $390.
        self.assertAlmostEqual(result.calculated_price, 390.00, places=2)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        self.assertFalse(formula["regional_rule_id"])

    def test_12_exact_origin_destination_pairing_respected(self):
        north = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ftl", "dry", 5, 2500)
        self.assertTrue(north.available, north.reason)
        north_rule = north.route_snapshot["legs"][0]["pricing_formula"]["regional_rule_id"]
        # GTA → Northumberland must use its own flat-rate rule, not the
        # GTA → Ottawa per-km rule.
        self.assertEqual(north_rule, self.rule_flat.id)
        self.assertNotEqual(north_rule, self.rule_per_km.id)
        self.assertAlmostEqual(north.calculated_price, 550.00, places=2)

        ottawa = self.svc.calculate(self.fsa_gta, self.fsa_ott, "ftl", "dry", 5, 2500)
        self.assertTrue(ottawa.available, ottawa.reason)
        ottawa_rule = ottawa.route_snapshot["legs"][0]["pricing_formula"]["regional_rule_id"]
        self.assertEqual(ottawa_rule, self.rule_per_km.id)
        self.assertAlmostEqual(ottawa.calculated_price, 1350.00, places=2)

    def test_13_booking_segment_distance_used_not_full_corridor(self):
        result = self.svc.calculate(self.fsa_gta, self.fsa_nor, "ftl", "dry", 5, 2500)
        self.assertTrue(result.available, result.reason)
        formula = result.route_snapshot["legs"][0]["pricing_formula"]
        # Corridor 1 reaches 600 km — the GTA → Northumberland booking must
        # use its own 130 km segment.
        self.assertAlmostEqual(formula["distance_km"], 130.0, places=2)
        self.assertAlmostEqual(result.calculated_price, 550.00, places=2)

        quebec = self.svc.calculate(self.fsa_gta, self.fsa_que, "ftl", "dry", 5, 2500)
        self.assertTrue(quebec.available, quebec.reason)
        quebec_formula = quebec.route_snapshot["legs"][0]["pricing_formula"]
        self.assertAlmostEqual(quebec_formula["distance_km"], 300.0, places=2)
        self.assertAlmostEqual(quebec.calculated_price, 900.00, places=2)

    def test_14_route_planner_uses_ftl_regional_pricing(self):
        """The coordinates quote path (ShipmentRoutingService.plan_route)
        prices an FTL shipment with the same corridor regional pricing
        method and the corridor's own segment distance."""
        from odoo.addons.prema_logistics_booking.services.shipment_routing_service import (
            ShipmentRoutingService,
        )
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        day_field = "operate_" + tomorrow.strftime("%A").lower()
        self.corridor1.write({day_field: True})
        self.env["logistics.direct.delivery.rule"].create({
            "origin_region_id": self.gta.id,
            "destination_region_id": self.northumberland.id,
            "applicable_corridor_id": self.corridor1.id,
            "direct_same_day_allowed": True,
            "hub_transfer_required": False,
        })

        route = ShipmentRoutingService(self.env).plan_route(
            49.0, -87.5, 49.8, -86.5,
            pallets=5, weight_lbs=2500,
            requested_pickup_date=tomorrow,
            equipment="dry", shipment_type="ftl",
        )
        self.assertTrue(route.available, route.reason)
        self.assertEqual(route.routing_snapshot.get("pricing_mode"), "ftl")
        self.assertEqual(route.routing_snapshot["ftl_pricing"]["pricing_type"], "flat_rate")
        # Flat rate $550 for the GTA → Northumberland pair.
        self.assertAlmostEqual(route.legs[0].leg_price, 550.00, places=2)
        self.assertAlmostEqual(route.legs[0].estimated_distance_km, 130.0, places=1)
        self.assertEqual(route.routing_snapshot["pricing"]["booking_minimum"], 0.0)

    def test_15_normal_ltl_booking_through_confirmation(self):
        """A normal LTL booking confirmed end-to-end stays on the untouched
        LTL calculation with the FTL wiring in place."""
        partner = self.env["res.partner"].create({"name": "FTI-LTL-Customer"})
        brand = self.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            self.env["fleet.vehicle.model.brand"].create({"name": "FTI-Brand"})
        model = self.env["fleet.vehicle.model"].create({"name": "FTI-Model-2", "brand_id": brand.id})
        vehicle = self.env["fleet.vehicle"].create({
            "name": "FTI-LTL-Truck", "license_plate": "FTILTL1", "model_id": model.id,
            "x_operational_logistics": True, "x_max_payload_lbs": 20000.0,
            "straight_pallet_capacity": 12, "pin_wheel_pallet_capacity": 13,
        })
        self.env["logistics.corridor.departure"].create({
            "corridor_id": self.corridor1.id,
            "departure_date": datetime.date.today() + datetime.timedelta(days=2),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": vehicle.id,
            "max_capacity": 12,
        })

        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": partner.id,
            "pickup_stops": [{"postal_code": "F1A"}],
            "delivery_stops": [{"postal_code": "F1B"}],
            "pallets": 4,
            "weight_lbs": 2000,
            "load_type": "ltl",
            "equipment_type": "dry",
            "pricing_method": "corridor",
        }, source_channel="internal")
        booking = svc.confirm_from_internal(norm, skip_invoice=True)

        self.assertEqual(booking.shipment_type, "ltl")
        self.assertFalse((booking.route_snapshot or {}).get("ftl_priced"))
        # LTL formula: 130 km × 4 pallets × ($4.00 ÷ 8) = $260.
        self.assertAlmostEqual(booking.calculated_price, 260.00, places=2)

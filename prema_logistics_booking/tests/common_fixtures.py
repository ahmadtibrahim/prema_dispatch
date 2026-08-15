"""Deterministic test fixtures for all prema_logistics_booking tests.

Creates a complete mini-logistics-network in setUpClass so tests never
depend on production database records.

Usage:
    from odoo.addons.prema_logistics_booking.tests.common_fixtures import LogisticsTestFixtures

    class TestMyThing(LogisticsTestFixtures, common.TransactionCase):
        def test_something(self):
            self.assertTrue(self.r1.exists())
"""
import datetime
from odoo.tests import common


class LogisticsTestFixtures:
    """Mixin that creates all required fixtures in setUpClass."""

    @classmethod
    def _create_fixtures(cls):
        """Create the full logistics fixture graph. Idempotent via search-before-create."""
        env = cls.env

        # ── 1. Regions (R1-R10) ──────────────────────────────────────
        cls.r1 = env["logistics.region"].search([("code", "=", "ZR1")], limit=1)
        if not cls.r1:
            cls.r1 = env["logistics.region"].create({"code": "ZR1", "name": "GTA Central", "hub_name": "Mississauga", "rate_per_km": 3.00})
        cls.r6 = env["logistics.region"].search([("code", "=", "ZR2")], limit=1)
        if not cls.r6:
            cls.r6 = env["logistics.region"].create({"code": "ZR2", "name": "Eastern Ontario", "hub_name": "Kingston", "rate_per_km": 2.80})
        cls.r7 = env["logistics.region"].search([("code", "=", "ZR3")], limit=1)
        if not cls.r7:
            cls.r7 = env["logistics.region"].create({"code": "ZR3", "name": "Ottawa Valley", "hub_name": "Ottawa", "rate_per_km": 2.80})
        cls.r8 = env["logistics.region"].search([("code", "=", "ZR4")], limit=1)
        if not cls.r8:
            cls.r8 = env["logistics.region"].create({"code": "ZR4", "name": "Greater Montreal", "hub_name": "Montreal", "rate_per_km": 2.80})
        cls.r3 = env["logistics.region"].search([("code", "=", "ZR5")], limit=1)
        if not cls.r3:
            cls.r3 = env["logistics.region"].create({"code": "ZR5", "name": "Golden Horseshoe South", "hub_name": "Hamilton", "rate_per_km": 3.00})

        # ── 2. FSAs ──────────────────────────────────────────────────
        cls.fsa_mississauga = env["logistics.fsa"].search([("fsa", "=", "Z9Z")], limit=1)
        if not cls.fsa_mississauga:
            cls.fsa_mississauga = env["logistics.fsa"].create({
                "fsa": "Z9Z", "province": "ON", "display_city": "Mississauga",
                "region_id": cls.r1.id, "pickup_supported": True, "delivery_supported": True,
            })
        cls.fsa_ottawa = env["logistics.fsa"].search([("fsa", "=", "Z8Z")], limit=1)
        if not cls.fsa_ottawa:
            cls.fsa_ottawa = env["logistics.fsa"].create({
                "fsa": "Z8Z", "province": "ON", "display_city": "Ottawa",
                "region_id": cls.r7.id, "pickup_supported": True, "delivery_supported": True,
            })
        cls.fsa_kingston = env["logistics.fsa"].search([("fsa", "=", "Z7Z")], limit=1)
        if not cls.fsa_kingston:
            cls.fsa_kingston = env["logistics.fsa"].create({
                "fsa": "Z7Z", "province": "ON", "display_city": "Kingston",
                "region_id": cls.r6.id, "pickup_supported": True, "delivery_supported": True,
            })
        cls.fsa_montreal = env["logistics.fsa"].search([("fsa", "=", "Z6Z")], limit=1)
        if not cls.fsa_montreal:
            cls.fsa_montreal = env["logistics.fsa"].create({
                "fsa": "Z6Z", "province": "QC", "display_city": "Montreal",
                "region_id": cls.r8.id, "pickup_supported": True, "delivery_supported": True,
            })
        cls.fsa_stcatharines = env["logistics.fsa"].search([("fsa", "=", "Z5Z")], limit=1)
        if not cls.fsa_stcatharines:
            cls.fsa_stcatharines = env["logistics.fsa"].create({
                "fsa": "Z5Z", "province": "ON", "display_city": "St Catharines",
                "region_id": cls.r3.id, "pickup_supported": True, "delivery_supported": True,
            })
        cls.fsa_hamilton = env["logistics.fsa"].search([("fsa", "=", "Z4Z")], limit=1)
        if not cls.fsa_hamilton:
            cls.fsa_hamilton = env["logistics.fsa"].create({
                "fsa": "Z4Z", "province": "ON", "display_city": "Hamilton",
                "region_id": cls.r3.id, "pickup_supported": True, "delivery_supported": True,
            })

        # ── 3. Equipment Profile ─────────────────────────────────────
        cls.equipment = env["logistics.equipment.profile"].with_context(active_test=False).search([], limit=1)
        if not cls.equipment:
            cls.equipment = env["logistics.equipment.profile"].create({
                "name": "Test 53ft Dry Van",
                "is_requirement_class": True,
                "min_pallets": 12, "min_payload_lbs": 11000.0,
            })

        # ── 4. Lanes ─────────────────────────────────────────────────
        cls.lane_r1_r8 = env["logistics.lane"].search([
            ("origin_region_id", "=", cls.r1.id),
            ("destination_region_id", "=", cls.r8.id),
        ], limit=1)
        if not cls.lane_r1_r8:
            cls.lane_r1_r8 = env["logistics.lane"].create({
                "origin_region_id": cls.r1.id,
                "destination_region_id": cls.r8.id,
                "road_km": 540.0, "revenue_target": 1600.0,
                "target_load_pallets": 8, "ltl_capable": True,
                "equipment_profile_id": cls.equipment.id,
            })

        cls.lane_r6_r8 = env["logistics.lane"].search([
            ("origin_region_id", "=", cls.r6.id),
            ("destination_region_id", "=", cls.r8.id),
        ], limit=1)
        if not cls.lane_r6_r8:
            cls.lane_r6_r8 = env["logistics.lane"].create({
                "origin_region_id": cls.r6.id,
                "destination_region_id": cls.r8.id,
                "road_km": 290.0, "revenue_target": 800.0,
                "target_load_pallets": 8, "ltl_capable": True,
            })

        cls.lane_r1_r7 = env["logistics.lane"].search([
            ("origin_region_id", "=", cls.r1.id),
            ("destination_region_id", "=", cls.r7.id),
        ], limit=1)
        if not cls.lane_r1_r7:
            cls.lane_r1_r7 = env["logistics.lane"].create({
                "origin_region_id": cls.r1.id,
                "destination_region_id": cls.r7.id,
                "road_km": 450.0, "revenue_target": 1200.0,
                "target_load_pallets": 8, "ltl_capable": True,
            })

        # ── 5. Service Levels ────────────────────────────────────────
        cls.service_nextday = env["logistics.service.level"].search([("code", "=", "NEXT_DAY")], limit=1)
        if not cls.service_nextday:
            cls.service_nextday = env["logistics.service.level"].create({
                "code": "NEXT_DAY", "name": "Next Day", "sequence": 10,
            })

        # ── 6. Service Offerings ─────────────────────────────────────
        cls.offering_r1r8_dry = env["logistics.service.offering"].search([
            ("lane_id", "=", cls.lane_r1_r8.id), ("temperature_mode", "=", "dry"),
        ], limit=1)
        if not cls.offering_r1r8_dry:
            cls.offering_r1r8_dry = env["logistics.service.offering"].create({
                "lane_id": cls.lane_r1_r8.id,
                "service_level_id": cls.service_nextday.id,
                "temperature_mode": "dry", "shipment_type": "ltl",
            })

        # ── 7. Rate Plans ────────────────────────────────────────────
        cls.rate_plan_r1r8 = env["logistics.rate.plan"].search([
            ("service_offering_id", "=", cls.offering_r1r8_dry.id),
        ], limit=1)
        if not cls.rate_plan_r1r8:
            cls.rate_plan_r1r8 = env["logistics.rate.plan"].create({
                "service_offering_id": cls.offering_r1r8_dry.id,
                "version": 1,
                "revenue_target": 1600.0, "planned_pallets": 8,
                "pricing_mode": "simple",
            })

        # ── 8. Corridors + Stops ─────────────────────────────────────
        cls.cor_quebec = env["logistics.corridor"].search([("name", "=", "TEST-Eastbound-Quebec")], limit=1)
        if not cls.cor_quebec:
            cls.cor_quebec = env["logistics.corridor"].create({
                "name": "TEST-Eastbound-Quebec", "direction": "eastbound",
                "phase": 1, "truck_slot": 1, "operate_tuesday": True,
                "full_distance_km": 600.0, "full_revenue_target": 2300.0,
                "planned_pallets": 8,
            })
            # Create stops
            env["logistics.corridor.stop"].create([
                {"corridor_id": cls.cor_quebec.id, "sequence": 10, "region_id": cls.r1.id, "name": "Mississauga", "pickup_allowed": True, "delivery_allowed": False, "distance_from_origin_km": 0},
                {"corridor_id": cls.cor_quebec.id, "sequence": 20, "region_id": cls.r6.id, "name": "Kingston", "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 280},
                {"corridor_id": cls.cor_quebec.id, "sequence": 30, "region_id": cls.r8.id, "name": "Montreal", "pickup_allowed": False, "delivery_allowed": True, "distance_from_origin_km": 600},
            ])

        # ── 9. Departure (next Tuesday) ──────────────────────────────
        cls.next_tuesday = cls._next_weekday(1)  # 1=Tuesday
        cls.dep_quebec = env["logistics.corridor.departure"].search([
            ("corridor_id", "=", cls.cor_quebec.id),
            ("departure_date", "=", cls.next_tuesday),
        ], limit=1)
        if not cls.dep_quebec:
            cls.dep_quebec = env["logistics.corridor.departure"].create({
                "corridor_id": cls.cor_quebec.id,
                "departure_date": cls.next_tuesday,
                "departure_time": 1.0, "cutoff_time": 16.0,
                "status": "scheduled", "max_capacity": 12,
            })

        # ── 10. Test Partners ────────────────────────────────────────
        cls.partner_a = env["res.partner"].search([("name", "=", "TEST-Customer-A-V3")], limit=1)
        if not cls.partner_a:
            cls.partner_a = env["res.partner"].create({
                "name": "TEST-Customer-A-V3", "is_company": True,
                "logistics_pricing_status": "approved",
            })
        cls.partner_b = env["res.partner"].search([("name", "=", "TEST-Customer-B-V3")], limit=1)
        if not cls.partner_b:
            cls.partner_b = env["res.partner"].create({
                "name": "TEST-Customer-B-V3", "is_company": True,
                "logistics_pricing_status": "approved",
            })

        # ── 11. Products ─────────────────────────────────────────────
        cls.product_freight = env["product.product"].search([("name", "=", "TEST-LTL-Freight")], limit=1)
        if not cls.product_freight:
            cls.product_freight = env["product.product"].create({
                "name": "TEST-LTL-Freight", "type": "service",
                "list_price": 200.0,
            })

        # Flush
        env.cr.commit()

    @staticmethod
    def _next_weekday(target_weekday):
        """Return the next date matching target_weekday (0=Mon...6=Sun)."""
        today = datetime.date.today()
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return today + datetime.timedelta(days=days_ahead)

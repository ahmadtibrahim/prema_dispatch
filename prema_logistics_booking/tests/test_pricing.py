"""Pricing engine tests — simplified formula: Revenue Target / Planned Pallets × Pallets."""
from datetime import date, timedelta

from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestPricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Lane = cls.env["logistics.lane"]
        cls.SLevel = cls.env["logistics.service.level"]
        cls.SOffering = cls.env["logistics.service.offering"]
        cls.RatePlan = cls.env["logistics.rate.plan"]
        cls.Schedule = cls.env["logistics.lane.schedule"]
        cls.SurchargeType = cls.env["logistics.surcharge.type"]

        # Create operational vehicle for capacity engine (required for pricing)
        Vehicle = cls.env["fleet.vehicle"]
        VehicleModel = cls.env["fleet.vehicle.model"]
        cls.vehicle = Vehicle.search([("x_operational_logistics", "=", True)], limit=1)
        if not cls.vehicle:
            model = VehicleModel.search([], limit=1)
            if not model:
                brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1)
                if not brand:
                    brand = cls.env["fleet.vehicle.model.brand"].create({"name": "TEST-Brand"})
                model = VehicleModel.create({"name": "TEST-Model", "brand_id": brand.id})
            cls.vehicle = Vehicle.create({
                "name": "TEST-V3-Truck",
                "license_plate": "TESTV3",
                "model_id": model.id,
                "x_operational_logistics": True,
                "x_max_pallets": 14,
                "straight_pallet_capacity": 12,
                "pin_wheel_pallet_capacity": 13,
                "turned_pallet_capacity": 14,
            })
        # Equipment profile linked to operational vehicle
        EquipProfile = cls.env["logistics.equipment.profile"]
        cls.equipment = EquipProfile.search([("fleet_vehicle_id", "=", cls.vehicle.id)], limit=1)
        if not cls.equipment:
            cls.equipment = EquipProfile.create({
                "name": "TEST-V3-Equip-Profile",
                "fleet_vehicle_id": cls.vehicle.id,
                "max_pallets": 14,
            })

        # Regions
        cls.r1 = cls.Region.create({"code": "T1", "name": "Test Region 1"})
        cls.r2 = cls.Region.create({"code": "T2", "name": "Test Region 2"})

        # FSAs
        cls.fsa1 = cls.Fsa.create({
            "fsa": "T1A", "region_id": cls.r1.id, "display_city": "Test City 1",
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.fsa2 = cls.Fsa.create({
            "fsa": "T2B", "region_id": cls.r2.id, "display_city": "Test City 2",
            "pickup_supported": True, "delivery_supported": True,
        })

        # Lane
        cls.lane = cls.Lane.create({
            "origin_region_id": cls.r1.id, "destination_region_id": cls.r2.id,
            "active": True, "ltl_capable": True, "ftl_capable": True,
            "max_pallets": 12, "equipment_profile_id": cls.equipment.id,
        })

        # Service level
        cls.slevel = cls.SLevel.create({
            "code": "TEST_NEXT_DAY", "name": "Test Next Day",
            "reefer_food_eligible": True,
        })

        # Offering
        cls.offering = cls.SOffering.create({
            "lane_id": cls.lane.id, "service_level_id": cls.slevel.id,
            "temperature_mode": "dry", "shipment_type": "both",
        })

        # Schedule
        cls.Schedule.create({
            "service_offering_id": cls.offering.id, "cutoff_time": 16.0,
            "pickup_monday": True, "pickup_tuesday": True, "pickup_wednesday": True,
            "pickup_thursday": True, "pickup_friday": True,
            "delivery_offset_type": "next_day", "active": True,
        })

        # Rate Plan: $1600 revenue target ÷ 8 planned pallets = $200/pallet
        cls.plan = cls.RatePlan.create({
            "service_offering_id": cls.offering.id,
            "revenue_target": 1600.0,
            "planned_pallets": 8,
        })

        # TEMP_REEFER surcharge at 0% (search-or-create to avoid unique-constraint collisions)
        cls.reefer_st = cls.SurchargeType.search([("code", "=", "TEMP_REEFER")], limit=1)
        if not cls.reefer_st:
            cls.reefer_st = cls.SurchargeType.create({
                "code": "TEMP_REEFER", "name": "Reefer",
                "calc_type": "percent", "default_amount": 0.0, "is_global": True,
            })

    # ── Simple formula tests (matching spec: $1600/8 = $200/pallet) ──

    def test_01_one_pallet_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 800)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_02_two_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 2, 1600)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 400.00, places=2)

    def test_03_five_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 5, 4000)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1000.00, places=2)

    def test_04_eight_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 8, 6400)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    def test_05_thirteen_pallets_dry(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 13, 10400)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 2600.00, places=2)

    # ── Reefer tests ──

    def test_06_one_pallet_reefer_zero_percent(self):
        """Reefer at 0% surcharge = same as dry."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "reefer", 1, 800)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_07_eight_pallets_reefer_zero_percent(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "reefer", 8, 6400)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 1600.00, places=2)

    # ── Chilled / Frozen: no surcharge applied (surcharges deactivated) ──

    def test_08_chilled_no_surcharge(self):
        """With TEMP_CHILLED deactivated, chilled = same as dry."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "chilled", 1, 800)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_09_frozen_no_surcharge(self):
        """With TEMP_FROZEN deactivated, frozen = same as dry."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "frozen", 1, 800)
        self.assertTrue(result.available)
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    # ── No legacy additions ──

    def test_10_no_zone_adjustment(self):
        """Price must be exactly formula-based, no extra amounts."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 800)
        self.assertEqual(len(result.price_lines), 2)  # Linehaul + Final only
        self.assertAlmostEqual(result.calculated_price, 200.00, places=2)

    def test_11_same_price_different_pickup_fsa_same_region(self):
        """Two FSAs in same region must give same price (no zone/FSA adjustment)."""
        fsa1b = self.Fsa.create({
            "fsa": "T1B", "region_id": self.r1.id, "display_city": "Test City 1B",
            "pickup_supported": True, "delivery_supported": True,
        })
        r1 = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 800)
        r2 = PricingService(self.env).calculate(fsa1b, self.fsa2, "ltl", "dry", 1, 800)
        self.assertTrue(r1.available and r2.available)
        self.assertAlmostEqual(r1.calculated_price, r2.calculated_price, places=2)

    # ── Edge cases ──

    def test_12_lane_not_supported(self):
        unsupported = self.Fsa.create({
            "fsa": "Z9Z", "pickup_supported": False, "delivery_supported": False,
        })
        result = PricingService(self.env).calculate(unsupported, self.fsa2, "ltl", "dry", 1, 800)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "pickup_fsa_not_supported")

    def test_13_pallets_over_cap(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 15, 12000)
        self.assertFalse(result.available)

    def test_14_ftl_available_if_lane_supports(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ftl", "dry", 12, 9600)
        self.assertTrue(result.available)

    # ── Immutable quote: price lines match final total ──

    def test_15_price_lines_match_total(self):
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 3, 2400)
        self.assertTrue(result.available)
        line_sum = sum(line["amount"] for line in result.price_lines[:-1])  # exclude "Final"
        final_line = result.price_lines[-1]["amount"]
        self.assertAlmostEqual(line_sum, final_line, places=2)
        self.assertAlmostEqual(final_line, result.calculated_price, places=2)

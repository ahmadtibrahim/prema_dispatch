"""Booking confirmation tests."""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestBooking(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Lane = cls.env["logistics.lane"]
        cls.SLevel = cls.env["logistics.service.level"]
        cls.SOffering = cls.env["logistics.service.offering"]
        cls.RatePlan = cls.env["logistics.rate.plan"]
        cls.Tier = cls.env["logistics.rate.tier"]
        cls.Schedule = cls.env["logistics.lane.schedule"]

        # Create operational vehicle for capacity engine
        Vehicle = cls.env["fleet.vehicle"]
        VehicleModel = cls.env["fleet.vehicle.model"]
        vehicle = Vehicle.search([("x_operational_logistics", "=", True)], limit=1)
        if not vehicle:
            model = VehicleModel.search([], limit=1)
            if not model:
                brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1)
                if not brand:
                    brand = cls.env["fleet.vehicle.model.brand"].create({"name": "TEST-Brand"})
                model = VehicleModel.create({"name": "TEST-Model", "brand_id": brand.id})
            vehicle = Vehicle.create({
                "name": "TEST-V3-BOOK-Truck", "license_plate": "TESTBOOK",
                "model_id": model.id, "x_operational_logistics": True,
                "straight_pallet_capacity": 12, "pin_wheel_pallet_capacity": 13,
                "turned_pallet_capacity": 14,
            })
        Equip = cls.env["logistics.equipment.profile"]
        equip = Equip.search([("fleet_vehicle_id", "=", vehicle.id)], limit=1)
        if not equip:
            equip = Equip.create({"name": "TEST-Booking-Equip", "fleet_vehicle_id": vehicle.id, "max_pallets": 14})

        r1 = cls.Region.create({"code": "B1", "name": "Booking R1"})
        r2 = cls.Region.create({"code": "B2", "name": "Booking R2"})
        cls.fsa1 = cls.Fsa.create({"fsa": "B1A", "region_id": r1.id, "display_city": "B City 1", "pickup_supported": True, "delivery_supported": True})
        cls.fsa2 = cls.Fsa.create({"fsa": "B2B", "region_id": r2.id, "display_city": "B City 2", "pickup_supported": True, "delivery_supported": True})

        lane = cls.Lane.create({"origin_region_id": r1.id, "destination_region_id": r2.id, "active": True, "ltl_capable": True, "ftl_capable": True, "max_pallets": 12, "revenue_target": 500.0, "equipment_profile_id": equip.id})
        slevel = cls.SLevel.create({"code": "BOOK_TEST", "name": "Booking Test", "reefer_food_eligible": True})
        offering = cls.SOffering.create({"lane_id": lane.id, "service_level_id": slevel.id, "temperature_mode": "dry", "shipment_type": "both"})
        cls.Schedule.create({"service_offering_id": offering.id, "cutoff_time": 16.0, "pickup_monday": True, "pickup_tuesday": True, "pickup_wednesday": True, "pickup_thursday": True, "pickup_friday": True, "delivery_offset_type": "next_day", "active": True})
        plan = cls.RatePlan.create({"service_offering_id": offering.id, "revenue_target": 500.0, "planned_pallets": 7, "target_load_quantity": 7})
        base = 500 / 7
        for min_q, max_q, mult, cap in [(1,1,1.75,0),(2,2,1.55,0),(3,3,1.35,0),(4,4,1.20,0),(5,5,1.10,0),(6,6,1.00,0),(7,7,0.92,0),(8,999,0.85,550)]:
            cls.Tier.create({"rate_plan_id": plan.id, "tier_type": "pallet", "min_qty": min_q, "max_qty": max_q, "calc_method": "per_unit", "rate": max(round(base*mult,0), 50.0), "cap_amount": cap})

    def test_01_booking_number_format(self):
        """Booking number follows PF-YYMMDD-XXXXXX format."""
        num = self.env["logistics.booking"]._generate_booking_number()
        self.assertRegex(num, r'^PF-\d{6}-\d{6}$')

    def test_02_booking_number_sequence_advances(self):
        """Sequential calls produce different numbers."""
        num1 = self.env["logistics.booking"]._generate_booking_number()
        num2 = self.env["logistics.booking"]._generate_booking_number()
        self.assertNotEqual(num1, num2)

    def test_03_pricing_session_creates_token(self):
        """Pricing session gets a unique token."""
        session = self.env["logistics.pricing.session"].create({
            "partner_id": self.env.user.partner_id.id,
            "pickup_fsa_id": self.fsa1.id,
            "delivery_fsa_id": self.fsa2.id,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 4,
            "weight_lbs": 3200,
            "expires_at": "2026-12-31 23:59:59",
        })
        self.assertTrue(session.token)
        self.assertEqual(len(session.token), 32)

    def test_04_pricing_result_has_price_lines(self):
        """Pricing result includes breakdown lines."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 4, 3200)
        self.assertTrue(result.available)
        self.assertGreater(len(result.price_lines), 1)
        # Last line should be the final price
        self.assertIn("Final Freight Price", result.price_lines[-1]["label"])

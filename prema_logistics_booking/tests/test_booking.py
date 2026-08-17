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

        # Create operational vehicle for capacity engine. Never reuse a
        # production vehicle from the clone (the real Freightliner runs
        # GTA → QUEBEC in the production data).
        Vehicle = cls.env["fleet.vehicle"]
        VehicleModel = cls.env["fleet.vehicle.model"]
        vehicle = Vehicle.search([("name", "=", "TEST-V3-BOOK-Truck")], limit=1)
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
        equip = Equip.with_context(active_test=False).search([("fleet_vehicle_id", "=", vehicle.id)], limit=1)
        if not equip:
            equip = Equip.create({"name": "TEST-Booking-Equip", "fleet_vehicle_id": vehicle.id, "max_pallets": 14})

        r1 = cls.Region.create({"code": "B1", "name": "Booking R1"})
        r2 = cls.Region.create({"code": "B2", "name": "Booking R2"})
        cls.fsa1 = cls.Fsa.create({"fsa": "B1A", "region_id": r1.id, "display_city": "B City 1", "pickup_supported": True, "delivery_supported": True})
        cls.fsa2 = cls.Fsa.create({"fsa": "B2B", "region_id": r2.id, "display_city": "B City 2", "pickup_supported": True, "delivery_supported": True})

        lane = cls.Lane.create({"origin_region_id": r1.id, "destination_region_id": r2.id, "active": True, "ltl_capable": True, "ftl_capable": True, "max_pallets": 12, "revenue_target": 500.0, "equipment_profile_id": equip.id})
        slevel = cls.SLevel.create({"code": "BOOK_TEST", "name": "Booking Test", "reefer_food_eligible": True})
        offering = cls.SOffering.create({"lane_id": lane.id, "service_level_id": slevel.id, "temperature_mode": "dry", "shipment_type": "ltl"})
        cls.Schedule.create({"service_offering_id": offering.id, "cutoff_time": 16.0, "pickup_monday": True, "pickup_tuesday": True, "pickup_wednesday": True, "pickup_thursday": True, "pickup_friday": True, "delivery_offset_type": "next_day", "active": True})
        plan = cls.RatePlan.create({"service_offering_id": offering.id, "revenue_target": 500.0, "planned_pallets": 7, "target_load_quantity": 7})
        base = 500 / 7
        for min_q, max_q, mult, cap in [(1,1,1.75,0),(2,2,1.55,0),(3,3,1.35,0),(4,4,1.20,0),(5,5,1.10,0),(6,6,1.00,0),(7,7,0.92,0),(8,999,0.85,550)]:
            cls.Tier.create({"rate_plan_id": plan.id, "tier_type": "pallet", "min_qty": min_q, "max_qty": max_q, "calc_method": "per_unit", "rate": max(round(base*mult,0), 50.0), "cap_amount": cap})

        # Corridor-era pricing fixture: 100 km × 16 $/km ÷ 8 planned
        # pallets = $2/pallet-km (the lane/offering/rate-plan block above is
        # legacy data kept for model-level tests; pricing never reads it).
        cls.Corridor = cls.env["logistics.corridor"]
        cls.CStop = cls.env["logistics.corridor.stop"]
        cls.corridor = cls.Corridor.with_context(skip_departure_reconcile=True).create({
            "name": "TEST-BOOK Corridor",
            "direction": "eastbound",
            "rate_per_km": 16.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "departure_horizon_weeks": 8,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 10,
            "region_id": r1.id, "distance_from_origin_km": 0.0, "day_offset": 0,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 20,
            "region_id": r2.id, "distance_from_origin_km": 100.0, "day_offset": 0,
        })

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
        """Pricing result includes breakdown lines (corridor era: the
        freight line itself is the price authority — no separate
        'Final Freight Price' summary line exists)."""
        result = PricingService(self.env).calculate(self.fsa1, self.fsa2, "ltl", "dry", 4, 2000)
        self.assertTrue(result.available, result.reason or "not available")
        self.assertGreaterEqual(len(result.price_lines), 1)
        # 100 km × 4 pallets × $2/pallet-km = 800; weight within included
        # allowance, so the single freight line's amount IS the total.
        self.assertIn("km × 4 pallet(s)", result.price_lines[-1]["label"])
        self.assertAlmostEqual(result.price_lines[-1]["amount"], result.calculated_price, places=2)
        self.assertAlmostEqual(result.calculated_price, 800.00, places=2)

"""Booking → Invoice → Dispatch integration tests."""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestBookingInvoice(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Fsa = cls.env["logistics.fsa"]
        cls.Corridor = cls.env["logistics.corridor"]
        cls.CStop = cls.env["logistics.corridor.stop"]
        cls.Departure = cls.env["logistics.corridor.departure"]
        cls.SurchargeType = cls.env["logistics.surcharge.type"]
        cls.Equip = cls.env["logistics.equipment.profile"]
        cls.Vehicle = cls.env["fleet.vehicle"]

        # Create a dedicated TEST vehicle (required for capacity + dispatch
        # feasibility). Never reuse a production vehicle from the clone:
        # _check_vehicle_day_conflicts rejects a second departure on the
        # same truck+date (the real Freightliner already runs GTA → QUEBEC
        # on 2026-08-18 in the production data).
        cls.vehicle = cls.Vehicle.search([("name", "=", "TEST-V3-INV-Truck")], limit=1)
        if not cls.vehicle:
            VehicleModel = cls.env["fleet.vehicle.model"]
            model = VehicleModel.search([], limit=1)
            if not model:
                brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1)
                if not brand:
                    brand = cls.env["fleet.vehicle.model.brand"].create({"name": "TEST-Brand"})
                model = VehicleModel.create({"name": "TEST-Model", "brand_id": brand.id})
            cls.vehicle = cls.Vehicle.create({
                "name": "TEST-V3-INV-Truck",
                "license_plate": "TESTINV",
                "model_id": model.id,
                "x_operational_logistics": True,
                "x_max_pallets": 14, "x_max_payload_lbs": 20000.0,
                "straight_pallet_capacity": 12,
                "pin_wheel_pallet_capacity": 13,
                "turned_pallet_capacity": 14,
            })
        cls.equipment = cls.Equip.with_context(active_test=False).search([("fleet_vehicle_id", "=", cls.vehicle.id)], limit=1)
        if not cls.equipment:
            cls.equipment = cls.Equip.create({
                "name": "TEST-V3-INV-Equip",
                "fleet_vehicle_id": cls.vehicle.id,
                "max_pallets": 14,
            })

        cls.r1 = cls.Region.create({"code": "T1I", "name": "Test Region Invoice 1"})
        cls.r2 = cls.Region.create({"code": "T2I", "name": "Test Region Invoice 2"})
        cls.fsa1 = cls.Fsa.create({
            "fsa": "T1I", "region_id": cls.r1.id, "display_city": "Test City 1",
            "pickup_supported": True, "delivery_supported": True,
        })
        cls.fsa2 = cls.Fsa.create({
            "fsa": "T2I", "region_id": cls.r2.id, "display_city": "Test City 2",
            "pickup_supported": True, "delivery_supported": True,
        })
        # Corridor-era pricing fixture: the corridor owns distance, $/km,
        # planned pallets and the booking minimum. 40 km × 200 $/km over
        # 8 planned pallets = 25 $/pallet-km (see test_02).
        cls.corridor = cls.Corridor.with_context(skip_departure_reconcile=True).create({
            "name": "TEST-INV Corridor",
            "direction": "eastbound",
            "rate_per_km": 200.0,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 0.0,
            "departure_horizon_weeks": 8,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 10,
            "region_id": cls.r1.id, "distance_from_origin_km": 0.0,
            "day_offset": 0,
        })
        cls.CStop.create({
            "corridor_id": cls.corridor.id, "sequence": 20,
            "region_id": cls.r2.id, "distance_from_origin_km": 40.0,
            "day_offset": 0,
        })

        # Scheduled departure on the operational test vehicle — the live
        # portal always prices with resolve_departures=True, and leg
        # creation at confirm refuses a snapshot without an exact
        # departure.
        from datetime import date, timedelta
        cls.Departure.create({
            "corridor_id": cls.corridor.id,
            "departure_date": date.today() + timedelta(days=1),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": cls.vehicle.id,
            "max_capacity": 12,
        })

        # Ensure TEMP_REEFER exists
        cls.reefer_st = cls.SurchargeType.search([("code", "=", "TEMP_REEFER")], limit=1)
        if not cls.reefer_st:
            cls.reefer_st = cls.SurchargeType.create({
                "code": "TEMP_REEFER", "name": "Reefer", "calc_type": "percent",
                "default_amount": 0.0, "is_global": True,
            })

        # Ensure test user's commercial partner is approved for booking
        cls.env.user.partner_id.commercial_partner_id.sudo().write({"logistics_pricing_status": "approved"})

        # Configure product mapping for Canada Dry LTL
        Product = cls.env["product.product"].sudo()
        product = Product.search([("type", "=", "service"), ("name", "=", "LTL Freight Service")], limit=1)
        if not product:
            product = Product.create({
                "name": "LTL Freight Service", "type": "service",
                "list_price": 200.0,
                "taxes_id": False,
            })
        cls.env["ir.config_parameter"].sudo().set_param("logistics.product_ca_dry_ltl_id", str(product.id))
        cls.env["ir.config_parameter"].sudo().set_param("logistics.product_ca_reefer_ltl_id", str(product.id))

    def _create_session_and_book(self, pallets=1, weight=500, temp="dry"):
        """Create a pricing session and confirm booking."""
        ps = PricingService(self.env)
        # resolve_departures mirrors the live portal quote: legs carry an
        # exact departure, which leg creation at confirm requires.
        result = ps.calculate(
            self.fsa1, self.fsa2, "ltl", temp, pallets, weight,
            resolve_departures=True)
        self.assertTrue(result.available, f"Pricing not available: {result.reason}")

        from datetime import datetime, timedelta
        import uuid
        # Mirror the live portal session creation (request_quote.py):
        # corridor_id + route_snapshot are the corridor-era fields;
        # service_offering/rate_plan are legacy compatibility (False).
        session = self.env["logistics.pricing.session"].sudo().create({
            "token": uuid.uuid4().hex,
            "partner_id": self.env.user.partner_id.id,
            "pickup_fsa_id": self.fsa1.id,
            "delivery_fsa_id": self.fsa2.id,
            "corridor_id": result.corridor.id,
            "service_offering_id": result.service_offering.id if result.service_offering else False,
            "rate_plan_id": result.rate_plan.id if result.rate_plan else False,
            "shipment_type": "ltl",
            "temperature_mode": temp,
            # physical_pallets defaults to 1 on the model — mirror the
            # canonical portal session create (prepare_quote) which always
            # writes both, else confirm-time validation rejects any
            # multi-pallet quote (pallets != physical).
            "pallets": pallets,
            "physical_pallets": pallets,
            "weight_lbs": weight,
            "calculated_price": result.calculated_price,
            "price_snapshot": result.price_lines,
            "route_snapshot": result.route_snapshot,
            "pickup_date": result.pickup_date or datetime.now().date(),
            "delivery_date_estimate": (result.delivery_date_estimate or datetime.now().date() + timedelta(days=1)),
            "expires_at": datetime.now() + timedelta(minutes=20),
        })

        booking = self.env["logistics.booking"].sudo().confirm_from_session(
            session.token,
            {"pickup_postal_code": "T1I", "pickup_address": "123 Test St, City",
             "delivery_postal_code": "T2I", "delivery_address": "456 Test Ave, Town"},
        )
        return booking

    # ── Invoice tests ──────────────────────────────────────────────────

    def test_01_booking_creates_invoice(self):
        booking = self._create_session_and_book(pallets=3, weight=1500)
        self.assertTrue(booking.invoice_id, "Booking should have an invoice")
        self.assertEqual(booking.invoice_id.move_type, "out_invoice")
        self.assertEqual(booking.invoice_id.state, "draft")

    def test_02_invoice_price_matches_booking(self):
        booking = self._create_session_and_book(pallets=5, weight=2500)
        line = booking.invoice_id.invoice_line_ids[:1]
        self.assertTrue(line)
        # Corridor formula: 40 km × 5 pallets × (200 $/km / 8 planned
        # pallets) = 5000; 5 × 500 lb included weight covers the load.
        expected = PricingService.calculate_leg_per_km(
            40.0, 200.0, 8, 5, 500.0, 2500.0)["subtotal"]
        self.assertAlmostEqual(line.price_unit, expected, places=2)
        self.assertAlmostEqual(booking.calculated_price, expected, places=2)

    def test_03_duplicate_confirm_returns_same_booking(self):
        booking1 = self._create_session_and_book(pallets=1, weight=500)
        booking2 = self.env["logistics.booking"].sudo().confirm_from_session(
            booking1.pricing_session_token,
            {"pickup_postal_code": "T1I", "pickup_address": "123 Test St, City",
             "delivery_postal_code": "T2I", "delivery_address": "456 Test Ave, Town"},
        )
        self.assertEqual(booking1.id, booking2.id)

    def test_04_invoice_remains_draft(self):
        booking = self._create_session_and_book(pallets=2, weight=1000)
        self.assertEqual(booking.invoice_id.state, "draft")

    def test_05_dispatch_job_created(self):
        booking = self._create_session_and_book(pallets=4, weight=2000)
        self.assertTrue(booking.dispatch_job_id, "Booking should have a dispatch job")

    def test_06_invoice_booking_link(self):
        booking = self._create_session_and_book(pallets=1, weight=500)
        self.assertEqual(booking.invoice_id.logistics_booking_id.id, booking.id)

    # ── Multi-stop tests ───────────────────────────────────────────────

    def test_10_multi_stop_booking_stops(self):
        booking = self._create_session_and_book(pallets=3, weight=1500)
        # Add stops
        self.env["logistics.booking.stop"].sudo().create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "company_name": "Warehouse A", "street": "100 Pickup St", "city": "CityA",
             "province_state": "ON", "pallet_count": 3},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "company_name": "Store B", "street": "200 Delivery Ave", "city": "CityB",
             "province_state": "ON", "pallet_count": 2, "liftgate_required": True},
            {"booking_id": booking.id, "sequence": 30, "stop_type": "delivery",
             "company_name": "Store C", "street": "300 Delivery Rd", "city": "CityC",
             "province_state": "ON", "pallet_count": 1},
        ])
        booking.invalidate_recordset()
        # Confirm already creates the pickup+delivery stops; the 3 added
        # stops must be preserved on top of them.
        names = {s.company_name for s in booking.stop_ids}
        for expected in ("Warehouse A", "Store B", "Store C"):
            self.assertIn(expected, names)

    def test_11_multi_stop_sequence_preserved(self):
        booking = self._create_session_and_book(pallets=4, weight=2000)
        self.env["logistics.booking.stop"].sudo().create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "company_name": "Warehouse", "street": "1 Pickup Rd", "city": "CityA",
             "province_state": "ON", "pallet_count": 4},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "company_name": "Store 1", "street": "2 Delivery St", "city": "CityB",
             "province_state": "ON", "pallet_count": 2, "liftgate_required": True},
            {"booking_id": booking.id, "sequence": 30, "stop_type": "delivery",
             "company_name": "Store 2", "street": "3 Final St", "city": "CityC",
             "province_state": "ON", "pallet_count": 2},
        ])
        booking.invalidate_recordset()
        # Invoice description should mention all stops
        desc = booking._generate_invoice_description()
        self.assertIn("Warehouse", desc)
        self.assertIn("Store 1", desc)
        self.assertIn("Store 2", desc)
        self.assertIn("Liftgate: Yes", desc)

    def test_12_multi_stop_dispatch_creation(self):
        booking = self._create_session_and_book(pallets=4, weight=2000)
        self.env["logistics.booking.stop"].sudo().create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "company_name": "Warehouse", "street": "1 Pickup Rd", "city": "CityA",
             "province_state": "ON", "pallet_count": 4},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "company_name": "Store 1", "street": "2 Delivery St", "city": "CityB",
             "province_state": "ON", "pallet_count": 2},
            {"booking_id": booking.id, "sequence": 30, "stop_type": "delivery",
             "company_name": "Store 2", "street": "3 Final St", "city": "CityC",
             "province_state": "ON", "pallet_count": 2},
        ])
        booking.invalidate_recordset()
        # Dispatch job created during confirmation (from legacy fields, since stops added after)
        self.assertTrue(booking.dispatch_job_id)
        dispatch_stops = booking.dispatch_job_id.stop_ids
        # At least 2 stops (pickup + delivery) from legacy creation
        self.assertGreaterEqual(len(dispatch_stops), 2)

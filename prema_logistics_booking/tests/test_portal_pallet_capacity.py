"""Portal pallet-input capacity — the Total Physical Pallets limit comes
dynamically from VehicleCapacityService.for_pickup_date (same canonical
source used by the server-side pre-check and the confirm-time lock).
"""
import datetime

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
    VehicleCapacityService,
)


class TestPortalPalletCapacity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.service = VehicleCapacityService(cls.env)
        cls.brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            cls.env["fleet.vehicle.model.brand"].create({"name": "PPC-BRAND"})

        def _vehicle(name, plate, straight, pinwheel):
            model = cls.env["fleet.vehicle.model"].create(
                {"name": name + "-m", "brand_id": cls.brand.id})
            return cls.env["fleet.vehicle"].create({
                "name": name, "license_plate": plate, "model_id": model.id,
                "x_operational_logistics": True, "x_max_payload_lbs": 40000.0,
                "straight_pallet_capacity": straight,
                "pin_wheel_pallet_capacity": pinwheel,
                "turned_pallet_capacity": 0,
            })

        cls.truck13 = _vehicle("PPC-Truck13", "PPC13", 12, 13)
        cls.truck16 = _vehicle("PPC-Truck16", "PPC16", 14, 16)
        cls.trailer28 = _vehicle("PPC-Trailer", "PPC28", 26, 28)

        cls.region = cls.env["logistics.region"].create(
            {"code": "PPC-R", "name": "PPC Region"})
        cls.region_b = cls.env["logistics.region"].create(
            {"code": "PPC-B", "name": "PPC Region B"})
        cls.fsa = cls.env["logistics.fsa"].create(
            {"fsa": "V1A", "region_id": cls.region.id,
             "pickup_supported": True, "delivery_supported": True})
        cls.fsa_b = cls.env["logistics.fsa"].create(
            {"fsa": "V2B", "region_id": cls.region_b.id,
             "pickup_supported": True, "delivery_supported": True})

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "PPC-Corridor", "direction": "eastbound",
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.region.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.region_b.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 100.0},
        ])

        cls.monday = datetime.date.today() + datetime.timedelta(days=3)
        cls.wednesday = cls.monday + datetime.timedelta(days=2)
        cls.dep_mon = cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.corridor.id, "departure_date": cls.monday,
            "departure_time": 7.0, "status": "scheduled", "vehicle_id": cls.truck13.id,
            "max_capacity": 12,
        })
        cls.dep_wed = cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.corridor.id, "departure_date": cls.wednesday,
            "departure_time": 7.0, "status": "scheduled", "vehicle_id": cls.truck13.id,
            "max_capacity": 12,
        })

    def _booking(self, departure, pallets, state="confirmed"):
        partner = self.env["res.partner"].search([], limit=1)
        return self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "PPC-%d" % (len(self.env["logistics.booking"].search([])) + 1),
            "departure_id": departure.id,
            "pickup_fsa_id": self.fsa.id,
            "delivery_fsa_id": self.fsa_b.id,
            "pallets": pallets, "physical_pallets": pallets,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "weight_lbs": pallets * 500.0, "state": state,
            "calculated_price": 100.0,
        })

    def _capacity(self, date, region=None):
        return VehicleCapacityService.for_pickup_date(
            self.env, region or self.region, date)

    # ── Dynamic maximum (Part J) ─────────────────────────────────────

    def test_01_max_13_no_reservations(self):
        data = self._capacity(self.monday)
        self.assertTrue(data["available"])
        self.assertEqual(data["max_pallets"], 13)
        self.assertEqual(data["remaining_pallets"], 13)
        self.assertEqual(data["layout_code"], "standard")

    def test_02_eight_reserved_remaining_5(self):
        self._booking(self.dep_mon, 8)
        data = self._capacity(self.monday)
        self.assertEqual(data["reserved_pallets"], 8)
        self.assertEqual(data["remaining_pallets"], 5)
        self.assertEqual(data["layout_code"], "standard")

    def test_03_truck16_dynamic_max(self):
        self.dep_mon.vehicle_id = self.truck16
        data = self._capacity(self.monday)
        self.assertEqual(data["max_pallets"], 16)
        self.assertEqual(data["remaining_pallets"], 16)

    def test_04_trailer_dynamic_max(self):
        self.dep_mon.vehicle_id = self.trailer28
        data = self._capacity(self.monday)
        self.assertEqual(data["max_pallets"], 28)
        self.assertEqual(data["remaining_pallets"], 28)

    def test_05_date_change_updates_max(self):
        self._booking(self.dep_mon, 8)
        monday = self._capacity(self.monday)
        wednesday = self._capacity(self.wednesday)
        self.assertEqual(monday["remaining_pallets"], 5)
        self.assertEqual(wednesday["remaining_pallets"], 13)

    def test_06_fully_booked_remaining_0(self):
        self._booking(self.dep_mon, 13)
        data = self._capacity(self.monday)
        self.assertTrue(data["available"])
        self.assertEqual(data["remaining_pallets"], 0)

    def test_07_no_departure_on_date(self):
        data = self._capacity(self.monday + datetime.timedelta(days=100))
        self.assertFalse(data["available"])

    # ── Server-side rejection paths ──────────────────────────────────

    def test_08_manipulated_request_over_remaining_rejected(self):
        self._booking(self.dep_mon, 8)
        result = self.service.check_and_reserve(self.dep_mon, 6, 3000.0)
        self.assertFalse(result["capacity_valid"])
        self.assertIn("pallet position", result["reason"])

    def test_09_precheck_decision_matches_remaining(self):
        self._booking(self.dep_mon, 8)
        data = self._capacity(self.monday)
        # The controller rejects when physical_pallets > remaining.
        self.assertTrue(6 > data["remaining_pallets"])
        self.assertFalse(5 > data["remaining_pallets"])

    def test_10_multi_stop_shared_counted_once(self):
        booking = self._booking(self.dep_mon, 4)
        booking.shared_pallet_mode = True
        for _ in range(2):
            self.env["logistics.booking.stop"].create({
                "booking_id": booking.id, "sequence": 10,
                "stop_type": "delivery", "location_name": "Ottawa Stop",
            })
        data = self._capacity(self.monday)
        self.assertEqual(data["reserved_pallets"], 4)
        self.assertEqual(data["remaining_pallets"], 9)

    # ── Per-pallet weight source for the portal weight auto-calc ─────

    def test_11_per_pallet_weight_from_corridor(self):
        self.corridor.included_weight_per_pallet = 500.0
        data = self._capacity(self.monday)
        self.assertAlmostEqual(data["per_pallet_weight"], 500.0, places=2)

    def test_12_different_corridor_weight_dynamic(self):
        self.corridor.included_weight_per_pallet = 750.0
        data = self._capacity(self.monday)
        self.assertAlmostEqual(data["per_pallet_weight"], 750.0, places=2)

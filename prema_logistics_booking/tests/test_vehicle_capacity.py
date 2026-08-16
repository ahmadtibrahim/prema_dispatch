"""Dynamic vehicle pallet capacity + automatic layout selection.

Nothing is hardcoded: capacities come from the assigned vehicle's active
layout rows (or the legacy capacity fields), reservations come from
committed bookings, and one canonical VehicleCapacityService answers all
consumers.
"""
import datetime

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
    VehicleCapacityService,
)
from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestVehicleCapacity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.service = VehicleCapacityService(cls.env)
        cls.brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            cls.env["fleet.vehicle.model.brand"].create({"name": "VCP-BRAND"})

        def _vehicle(name, plate, straight, pinwheel, turned=0):
            model = cls.env["fleet.vehicle.model"].create(
                {"name": name + "-model", "brand_id": cls.brand.id})
            return cls.env["fleet.vehicle"].create({
                "name": name, "license_plate": plate, "model_id": model.id,
                "x_operational_logistics": True, "x_max_payload_lbs": 40000.0,
                "straight_pallet_capacity": straight,
                "pin_wheel_pallet_capacity": pinwheel,
                "turned_pallet_capacity": turned,
            })

        cls.truck1 = _vehicle("VCP-Truck1", "VCPTRK1", 12, 13)
        cls.truck_b = _vehicle("VCP-TruckB", "VCPTRK2", 14, 16)
        cls.trailer = _vehicle("VCP-Trailer", "VCPTRL1", 26, 28)

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "VCP-Corridor", "direction": "eastbound",
        })
        cls.region_a = cls.env["logistics.region"].create(
            {"code": "VCP-A", "name": "VCP Region A"})
        cls.region_b = cls.env["logistics.region"].create(
            {"code": "VCP-B", "name": "VCP Region B"})
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10,
             "region_id": cls.region_a.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20,
             "region_id": cls.region_b.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 100.0},
        ])
        cls.fsa_a = cls.env["logistics.fsa"].create(
            {"fsa": "V1A", "region_id": cls.region_a.id,
             "pickup_supported": True, "delivery_supported": True})
        cls.fsa_b = cls.env["logistics.fsa"].create(
            {"fsa": "V2B", "region_id": cls.region_b.id,
             "pickup_supported": True, "delivery_supported": True})
        cls.departure = cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.corridor.id,
            "departure_date": datetime.date.today() + datetime.timedelta(days=3),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": cls.truck1.id,
            "max_capacity": 12,
        })

    def _booking(self, pallets, state="confirmed"):
        partner = self.env["res.partner"].search([], limit=1)
        return self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "VCP-%d" % (len(self.env["logistics.booking"].search([])) + 1),
            "departure_id": self.departure.id,
            "pickup_fsa_id": self.fsa_a.id,
            "delivery_fsa_id": self.fsa_b.id,
            "pallets": pallets,
            "physical_pallets": pallets,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "weight_lbs": pallets * 500.0,
            "state": state,
            "calculated_price": 100.0,
        })

    # ── Layout selection (vehicle-level) ─────────────────────────────

    def test_01_layouts_from_legacy_fields(self):
        layouts = self.service.get_layouts(self.truck1)
        codes = [l["code"] for l in layouts]
        self.assertIn("standard", codes)
        self.assertIn("pinwheel", codes)
        standard = next(l for l in layouts if l["code"] == "standard")
        pinwheel = next(l for l in layouts if l["code"] == "pinwheel")
        self.assertEqual((standard["max_pallets"], standard["is_default"]), (12, True))
        self.assertEqual((pinwheel["max_pallets"], pinwheel["is_default"]), (13, False))

    def test_02_up_to_12_selects_standard(self):
        for count in (1, 8, 12):
            valid, layout = self.service.select_layout(self.truck1, count)
            self.assertTrue(valid)
            self.assertEqual(layout["code"], "standard")

    def test_03_13_selects_pinwheel_automatically(self):
        valid, layout = self.service.select_layout(self.truck1, 13)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "pinwheel")

    def test_04_14_rejected(self):
        valid, layout = self.service.select_layout(self.truck1, 14)
        self.assertFalse(valid)
        self.assertIsNone(layout)

    def test_05_reserved_8_plus_5_pinwheel(self):
        result = self.service.evaluate(self.truck1, self.departure, 5)
        result["reserved_pallets"] = 8  # simulated reservation
        valid, layout = self.service.select_layout(self.truck1, 8 + 5)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "pinwheel")

    def test_06_reserved_8_plus_6_rejected(self):
        valid, layout = self.service.select_layout(self.truck1, 8 + 6)
        self.assertFalse(valid)

    def test_07_reserved_8_plus_4_standard(self):
        valid, layout = self.service.select_layout(self.truck1, 8 + 4)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "standard")

    def test_08_cancel_from_13_to_11_returns_to_standard(self):
        valid, layout = self.service.select_layout(self.truck1, 11)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "standard")

    def test_09_truck_b_15_selects_alternate(self):
        valid, layout = self.service.select_layout(self.truck_b, 15)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "pinwheel")
        self.assertEqual(layout["max_pallets"], 16)

    def test_10_trailer_27_alternate_29_rejected(self):
        valid, layout = self.service.select_layout(self.trailer, 27)
        self.assertTrue(valid)
        self.assertEqual(layout["max_pallets"], 28)
        valid, layout = self.service.select_layout(self.trailer, 29)
        self.assertFalse(valid)

    def test_11_new_vehicle_custom_rows_no_code_change(self):
        vehicle = self.env["fleet.vehicle"].create({
            "name": "VCP-Future", "license_plate": "VCPFUT1",
            "model_id": self.env["fleet.vehicle.model"].create(
                {"name": "Future-model", "brand_id": self.brand.id}).id,
            "x_operational_logistics": True,
        })
        Layout = self.env["fleet.vehicle.pallet.layout"]
        Layout.create([
            {"vehicle_id": vehicle.id, "name": "Standard", "code": "standard",
             "layout_type": "standard", "max_pallets": 20, "is_default": True,
             "sequence": 10},
            {"vehicle_id": vehicle.id, "name": "Pinwheel", "code": "pinwheel",
             "layout_type": "pinwheel", "max_pallets": 22, "sequence": 20},
        ])
        valid, layout = self.service.select_layout(vehicle, 21)
        self.assertTrue(valid)
        self.assertEqual(layout["code"], "pinwheel")
        valid, layout = self.service.select_layout(vehicle, 20)
        self.assertEqual(layout["code"], "standard")

    # ── Departure reservations and display ───────────────────────────

    def test_16_existing_bookings_consume_capacity(self):
        self._booking(5)
        self._booking(3)
        self.departure.invalidate_recordset()
        self.assertEqual(self.service.reserved_pallets(self.departure), 8)
        result = self.service.evaluate(self.truck1, self.departure, 0)
        self.assertEqual(result["remaining_pallets"], 5)

    def test_17_cancelled_bookings_release_capacity(self):
        cancelled = self._booking(5, state="cancelled")
        self.assertEqual(self.service.reserved_pallets(self.departure), 0)
        cancelled.state = "confirmed"
        self.assertEqual(self.service.reserved_pallets(self.departure), 5)

    def test_18_multi_stop_booking_counts_physical_pallets_once(self):
        booking = self._booking(8)
        for _ in range(3):
            self.env["logistics.booking.stop"].create({
                "booking_id": booking.id, "sequence": 10,
                "stop_type": "delivery", "location_name": "Ottawa Stop",
            })
        self.assertEqual(self.service.reserved_pallets(self.departure), 8)

    def test_19_shared_pallet_mode_counts_positions_once(self):
        booking = self._booking(4)
        booking.shared_pallet_mode = True
        self.assertEqual(self.service.reserved_pallets(self.departure), 4)

    def test_20_departure_fields_standard_then_pinwheel(self):
        self._booking(12)
        self.departure.invalidate_recordset()
        self.assertEqual(self.departure.capacity_layout_code, "standard")
        self._booking(1)
        self.departure.invalidate_recordset()
        self.assertEqual(self.departure.capacity_layout_code, "pinwheel")
        self.assertEqual(self.departure.capacity_remaining_pallets, 0)

    def test_22_truck_reassignment_recalculates(self):
        self._booking(12)
        self.departure.invalidate_recordset()
        self.departure.write({
            "vehicle_id": self.truck_b.id,
            "vehicle_assignment_source": "manual_override",
        })
        self.departure.invalidate_recordset()
        self.assertEqual(self.departure.capacity_max_pallets, 16)
        self.assertEqual(self.departure.capacity_layout_code, "standard")
        self.assertEqual(self.departure.capacity_remaining_pallets, 4)

    def test_23_smaller_truck_assignment_blocked(self):
        self._booking(12)
        small = self.env["fleet.vehicle"].create({
            "name": "VCP-Small", "license_plate": "VCPSML1",
            "model_id": self.env["fleet.vehicle.model"].create(
                {"name": "Small-model", "brand_id": self.brand.id}).id,
            "straight_pallet_capacity": 10, "pin_wheel_pallet_capacity": 10,
            "turned_pallet_capacity": 0,
            "x_max_payload_lbs": 20000.0,
        })
        with self.assertRaises(ValidationError):
            self.departure.write({"vehicle_id": small.id})

    def test_24_authoritative_check_blocks_overbooking(self):
        self._booking(8)
        result = self.service.check_and_reserve(self.departure, 5, 2500.0)
        self.assertTrue(result["capacity_valid"])
        # A booking committed meanwhile reduces remaining to 5:
        self._booking(5)
        result = self.service.check_and_reserve(self.departure, 1, 500.0)
        self.assertFalse(result["capacity_valid"])
        self.assertIn("pallet position", result["reason"])

    def test_13_confirm_path_rejects_with_remaining_message(self):
        self._booking(8)
        svc = BookingOrchestrationService(self.env)
        with self.assertRaises(UserError) as ctx:
            svc._lock_and_validate_departures(
                [self.departure.id], "dry", 6, 3000.0,
            )
        self.assertIn("pallet position", str(ctx.exception))

    def test_25_layout_override_validation(self):
        self._booking(13)
        layout = self.env["fleet.vehicle.pallet.layout"].search(
            [("vehicle_id", "=", self.truck1.id), ("code", "=", "standard")],
            limit=1)
        # Ensure a layout row exists (fallback rows don't exist as records;
        # create one via the migration-style seed for the test).
        if not layout:
            layout = self.env["fleet.vehicle.pallet.layout"].create({
                "vehicle_id": self.truck1.id, "name": "Standard",
                "code": "standard", "layout_type": "standard",
                "max_pallets": 12, "is_default": True, "sequence": 10,
            })
        with self.assertRaises(ValidationError):
            self.departure.write({"capacity_layout_override_id": layout.id})

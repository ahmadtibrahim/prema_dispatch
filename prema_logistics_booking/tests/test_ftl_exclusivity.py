"""Shared LTL capacity + FTL exclusivity (Manual-UAT Part 5).

Rules under test:
  * LTL service bookings reserve exactly their physical POSITIONS — even
    when the corridor's FTL pricing threshold (enable_ftl +
    ftl_threshold_pallets + auto_price, corridor 9: 10) priced them with
    FTL math. Pricing mode is NEVER service type; pricing never
    auto-reserves the truck (the audit).
  * FTL / Dedicated / Exclusive service bookings (load_type/shipment_type
    = 'ftl') reserve the ENTIRE vehicle: they need a free truck, and once
    confirmed nothing else may join — LTL or FTL.
  * Milk-run (movement_v1) capacity is segment-aware: each pallet
    movement occupies only its pickup-region → delivery-region span, so
    a movement picked up at a later corridor stop never inflates the
    earlier segments.
  * Portal availability = vehicle max − reserved LTL − exclusive hold.
"""
import datetime
import json

from odoo.tests import TransactionCase
from odoo.exceptions import UserError

from odoo.addons.prema_logistics_booking.services.capacity_engine import CapacityEngine
from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
    VehicleCapacityService,
)


class TestFtlExclusivity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.service = VehicleCapacityService(cls.env)
        cls.engine = CapacityEngine(cls.env)
        cls.brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            cls.env["fleet.vehicle.model.brand"].create({"name": "FXL-BRAND"})

        def _vehicle(name, plate, straight, pinwheel):
            model = cls.env["fleet.vehicle.model"].create(
                {"name": name + "-model", "brand_id": cls.brand.id})
            return cls.env["fleet.vehicle"].create({
                "name": name, "license_plate": plate, "model_id": model.id,
                "x_operational_logistics": True, "x_max_payload_lbs": 40000.0,
                "straight_pallet_capacity": straight,
                "pin_wheel_pallet_capacity": pinwheel,
            })

        cls.truck = _vehicle("FXL-Truck", "FXLTRK1", 12, 13)

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "FXL-Corridor", "direction": "eastbound",
            # Corridor 9's audit config: FTL PRICING threshold at 10,
            # auto_price — never a capacity-exclusivity trigger.
            "enable_ftl": True, "ftl_threshold_pallets": 10,
            "ftl_behavior": "auto_price",
        })
        cls.region_a = cls.env["logistics.region"].create(
            {"code": "FXL-A", "name": "FXL Region A"})
        cls.region_b = cls.env["logistics.region"].create(
            {"code": "FXL-B", "name": "FXL Region B"})
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
            {"fsa": "X1A", "region_id": cls.region_a.id,
             "pickup_supported": True, "delivery_supported": True})
        cls.fsa_b = cls.env["logistics.fsa"].create(
            {"fsa": "X2B", "region_id": cls.region_b.id,
             "pickup_supported": True, "delivery_supported": True})
        cls.departure = cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.corridor.id,
            "departure_date": datetime.date.today() + datetime.timedelta(days=3),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": cls.truck.id,
            "max_capacity": 12,
        })

        cls.partner = cls.env["res.partner"].search([], limit=1)
        cls._seq = [0]

    def _booking(self, pallets, shipment_type="ltl", load_type=None,
                 pricing_mode=None, state="confirmed"):
        self._seq[0] += 1
        values = {
            "partner_id": self.partner.id,
            "booking_number": "FXL-%d" % self._seq[0],
            "departure_id": self.departure.id,
            "pickup_fsa_id": self.fsa_a.id,
            "delivery_fsa_id": self.fsa_b.id,
            "pallets": pallets,
            "physical_pallets": pallets,
            "shipment_type": shipment_type,
            "temperature_mode": "dry",
            "weight_lbs": pallets * 500.0,
            "state": state,
            "calculated_price": 100.0,
        }
        if load_type is not None:
            values["load_type"] = load_type
        if pricing_mode:
            values["route_snapshot"] = {"pricing_mode": pricing_mode}
        return self.env["logistics.booking"].create(values)

    # ── LTL reserves positions only ─────────────────────────────────

    def test_01_ltl_reserves_positions_only(self):
        """8 LTL pallets on a 13-position truck leave 5 sellable."""
        self._booking(8)
        self.departure.invalidate_recordset()
        result = self.service.evaluate(self.truck, self.departure, 0)
        self.assertFalse(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["reserved_ltl_positions"], 8)
        self.assertEqual(result["reserved_pallets"], 8)
        self.assertEqual(result["remaining_sellable_capacity"], 5)
        self.assertEqual(result["remaining_pallets"], 5)
        # Portal availability exposes the same sellable number.
        portal = self.service.for_pickup_date(
            self.env, self.region_a, self.departure.departure_date)
        self.assertTrue(portal["available"])
        self.assertFalse(portal["exclusive_vehicle_reserved"])
        self.assertEqual(portal["remaining_sellable_capacity"], 5)
        self.assertEqual(portal["remaining_pallets"], 5)

    # ── FTL reserves the whole vehicle ──────────────────────────────

    def test_02_ftl_confirmed_reserves_whole_vehicle(self):
        """An FTL service booking (6 pallets) holds the ENTIRE truck: no
        sellable capacity remains, even though its own positions are few."""
        ftl = self._booking(6, shipment_type="ftl", load_type="ftl")
        self.departure.invalidate_recordset()
        result = self.service.evaluate(self.truck, self.departure, 0)
        self.assertTrue(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["exclusive_booking_ids"], [ftl.id])
        self.assertEqual(result["reserved_ltl_positions"], 0)
        self.assertEqual(result["remaining_sellable_capacity"], 0)
        self.assertEqual(result["remaining_pallets"], 0)
        # Departure audit fields.
        self.departure.invalidate_recordset()
        self.assertTrue(self.departure.exclusive_vehicle_reserved)
        self.assertIn("FXL-", self.departure.exclusive_booking_ref)
        self.assertEqual(self.departure.remaining_sellable_capacity, 0)
        self.assertEqual(self.departure.reserved_ltl_positions, 0)
        # Portal: not sellable.
        portal = self.service.for_pickup_date(
            self.env, self.region_a, self.departure.departure_date)
        self.assertTrue(portal["exclusive_vehicle_reserved"])
        self.assertEqual(portal["remaining_sellable_capacity"], 0)

    def test_03_ltl_cannot_join_exclusive_departure(self):
        self._booking(6, shipment_type="ftl", load_type="ftl")
        check = self.service.check_and_reserve(self.departure, 4, 2000.0)
        self.assertFalse(check["capacity_valid"])
        self.assertIn("exclusively reserved", check["reason"])

    def test_04_ftl_requires_completely_free_truck(self):
        """FTL cannot join a departure that already carries LTL bookings —
        even when the total would physically fit."""
        self._booking(8)
        check = self.service.check_and_reserve(
            self.departure, 4, 2000.0, service_type="ftl")
        self.assertFalse(check["capacity_valid"])
        self.assertIn("entire", check["reason"].lower())

    def test_05_ftl_after_ltl_blocked_at_confirm_gate(self):
        """The confirm-time lock (FOR UPDATE) enforces the same rule and
        raises a user-facing error before anything is written."""
        self._booking(8)
        from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
            BookingOrchestrationService,
        )
        svc = BookingOrchestrationService(self.env)
        with self.assertRaises(UserError) as ctx:
            svc._lock_and_validate_departures(
                [self.departure.id], "dry", 4, 2000.0, service_type="ftl")
        self.assertIn("entire", str(ctx.exception).lower())
        # And an LTL confirmation on an exclusive departure is refused too.
        self._booking(2, shipment_type="ftl", load_type="ftl")
        with self.assertRaises(UserError) as ctx:
            svc._lock_and_validate_departures(
                [self.departure.id], "dry", 2, 1000.0, service_type="ltl")
        self.assertIn("exclusively", str(ctx.exception).lower())

    # ── The audit: FTL pricing threshold never reserves the truck ───

    def test_06_threshold_priced_ltl_never_reserves_truck(self):
        """Corridor 9's audit: 10-pallet LTL load priced FTL by the
        corridor threshold (auto_price) is still an LTL SERVICE booking —
        it reserves 10 positions and another 2-pallet LTL can join the
        remaining 3."""
        threshold_booking = self._booking(
            10, load_type="ltl", pricing_mode="ftl")
        self.assertFalse(
            self.engine._is_exclusive_service(threshold_booking))
        self.departure.invalidate_recordset()
        result = self.service.evaluate(self.truck, self.departure, 0)
        self.assertFalse(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["reserved_ltl_positions"], 10)
        self.assertEqual(result["remaining_sellable_capacity"], 3)
        # A second LTL booking still joins.
        check = self.service.check_and_reserve(self.departure, 2, 1000.0)
        self.assertTrue(check["capacity_valid"])
        self.assertEqual(check["remaining_sellable_capacity"], 3)

    def test_07_requested_ftl_load_reserves_whole_vehicle(self):
        """A genuinely requested FTL load (load_type='ftl') reserves the
        whole vehicle even below the pricing threshold — service type,
        not pallet count, decides exclusivity."""
        self._booking(3, shipment_type="ftl", load_type="ftl")
        self.departure.invalidate_recordset()
        result = self.service.evaluate(self.truck, self.departure, 0)
        self.assertTrue(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["remaining_sellable_capacity"], 0)

    # ── Segment-based milk-run capacity ─────────────────────────────

    def _milk_run_setup(self):
        """A second corridor with three regions (GTA → SEO → OTT) and real
        square polygons so movement stops resolve to regions."""
        Region = self.env["logistics.region"]
        country_ca = self.env.ref("base.ca")
        state_on = self.env["res.country.state"].search(
            [("country_id", "=", country_ca.id), ("code", "=", "ON")], limit=1)

        def _square(lng, lat, dx=0.1, dy=0.05):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - dx, lat - dy], [lng + dx, lat - dy],
                [lng + dx, lat + dy], [lng - dx, lat + dy],
                [lng - dx, lat - dy],
            ]]})

        gta = Region.create({
            "code": "MRX-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": country_ca.id,
            "state_id": state_on.id, "polygon_geojson": _square(-79.69, 43.76),
        })
        seo = Region.create({
            "code": "MRX-SEO", "name": "Southeastern Ontario",
            "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": country_ca.id,
            "state_id": state_on.id, "polygon_geojson": _square(-77.39, 44.18),
        })
        ott = Region.create({
            "code": "MRX-OTT", "name": "Ottawa", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": country_ca.id,
            "state_id": state_on.id, "polygon_geojson": _square(-75.70, 45.42),
        })
        corridor = self.env["logistics.corridor"].create({
            "name": "MRX-Eastbound", "direction": "eastbound",
        })
        self.env["logistics.corridor.stop"].create([
            {"corridor_id": corridor.id, "sequence": 10, "region_id": gta.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 0.0},
            {"corridor_id": corridor.id, "sequence": 20, "region_id": seo.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 312.1},
            {"corridor_id": corridor.id, "sequence": 30, "region_id": ott.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 507.6},
        ])
        departure = self.env["logistics.corridor.departure"].create({
            "corridor_id": corridor.id,
            "departure_date": datetime.date.today() + datetime.timedelta(days=5),
            "departure_time": 7.0, "status": "scheduled",
            "vehicle_id": self.truck.id, "max_capacity": 12,
        })
        return departure, gta, seo, ott

    def test_08_milk_run_movements_occupy_their_own_segments(self):
        """Movement_v1 segment capacity: 2 pallets Brampton→Belleville and
        2 pallets Brampton→Ottawa — peak 4 on GTA→SEO, 2 on SEO→OTT; a
        second booking with an SEO pickup movement never inflates GTA→SEO."""
        departure, gta, seo, ott = self._milk_run_setup()
        partner = self.partner

        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id, "booking_number": "MRX-1",
            "departure_id": departure.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": 2000.0,
            "state": "confirmed", "calculated_price": 100.0,
            "route_model_version": "movement_v1",
            "price_snapshot": [{"_pallet_movements": [
                {"key": "u1", "pickup_stop_key": "PU", "delivery_stop_keys": ["D-BLV"],
                 "weight_lbs": 500.0},
                {"key": "u2", "pickup_stop_key": "PU", "delivery_stop_keys": ["D-BLV"],
                 "weight_lbs": 500.0},
                {"key": "u3", "pickup_stop_key": "PU", "delivery_stop_keys": ["D-OTT"],
                 "weight_lbs": 500.0},
                {"key": "u4", "pickup_stop_key": "PU", "delivery_stop_keys": ["D-OTT"],
                 "weight_lbs": 500.0},
            ]}],
        })
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "stop_key": "PU", "city": "Brampton",
             "latitude": 43.755959, "longitude": -79.692568},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "stop_key": "D-BLV", "city": "Belleville",
             "latitude": 44.183661, "longitude": -77.394851},
            {"booking_id": booking.id, "sequence": 30, "stop_type": "delivery",
             "stop_key": "D-OTT", "city": "Ottawa",
             "latitude": 45.4215, "longitude": -75.6972},
        ])

        peak = self.engine.compute_departure_peak(departure)
        self.assertEqual(peak["peak_pallets"], 4)   # GTA→SEO carries all 4
        details = {d["to_stop"]: d["pallets"] for d in peak["segment_details"]}
        self.assertEqual(details["Southeastern Ontario"], 4)
        self.assertEqual(details["Ottawa"], 2)      # SEO→OTT carries only 2
        self.assertEqual(peak["reserved_ltl_positions"], 4)
        self.assertFalse(peak["exclusive_vehicle_reserved"])

        # A movement picked up at SEO (later stop) must NOT inflate the
        # GTA→SEO segment — the old anchor approach would have added it.
        booking2 = self.env["logistics.booking"].create({
            "partner_id": partner.id, "booking_number": "MRX-2",
            "departure_id": departure.id,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "physical_pallets": 2, "weight_lbs": 1000.0,
            "state": "confirmed", "calculated_price": 100.0,
            "route_model_version": "movement_v1",
            "price_snapshot": [{"_pallet_movements": [
                {"key": "s1", "pickup_stop_key": "PU-SEO",
                 "delivery_stop_keys": ["D-OTT2"], "weight_lbs": 500.0},
                {"key": "s2", "pickup_stop_key": "PU-SEO",
                 "delivery_stop_keys": ["D-OTT2"], "weight_lbs": 500.0},
            ]}],
        })
        self.env["logistics.booking.stop"].create([
            {"booking_id": booking2.id, "sequence": 10, "stop_type": "pickup",
             "stop_key": "PU-SEO", "city": "Belleville",
             "latitude": 44.183661, "longitude": -77.394851},
            {"booking_id": booking2.id, "sequence": 20, "stop_type": "delivery",
             "stop_key": "D-OTT2", "city": "Ottawa",
             "latitude": 45.4215, "longitude": -75.6972},
        ])

        peak2 = self.engine.compute_departure_peak(departure)
        details2 = {d["to_stop"]: d["pallets"] for d in peak2["segment_details"]}
        # GTA→SEO: booking1's 4 only (booking2 picks up at SEO — 6 would be
        # the anchor-based overstatement).
        self.assertEqual(details2["Southeastern Ontario"], 4)
        # SEO→OTT: 2 (booking1) + 2 (booking2) = 4.
        self.assertEqual(details2["Ottawa"], 4)
        self.assertEqual(peak2["peak_pallets"], 4)
        self.assertEqual(peak2["reserved_ltl_positions"], 4)

    # ── Cancellation releases the hold ──────────────────────────────

    def test_09_cancel_releases_exclusivity(self):
        booking = self._booking(6, shipment_type="ftl", load_type="ftl")
        self.departure.invalidate_recordset()
        self.assertTrue(self.departure.exclusive_vehicle_reserved)
        booking.state = "cancelled"
        self.departure.invalidate_recordset()
        result = self.service.evaluate(self.truck, self.departure, 0)
        self.assertFalse(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["remaining_sellable_capacity"],
                         result["maximum_capacity"])

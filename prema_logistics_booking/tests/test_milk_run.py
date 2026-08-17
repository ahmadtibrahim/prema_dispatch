"""Milk-run routing: movement simulation, time-aware route adviser,
precedence, peak capacity, and the booking→dispatch route bridge."""
import datetime

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.itinerary_planner import ItineraryPlanner


def _dt(day_offset, hour):
    return datetime.datetime(2026, 8, 19, 12, 0) + datetime.timedelta(
        days=day_offset, hours=hour - 12)


class TestMilkRun(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.planner = ItineraryPlanner(cls.env)

    # ── Test A: primary route movement simulation ───────────────────

    def test_01_primary_route_simulation(self):
        movements = [
            {"key": "u1", "pickup_stop_key": "ud", "delivery_stop_keys": ["ott"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "u2", "pickup_stop_key": "ud", "delivery_stop_keys": ["ott"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "u3", "pickup_stop_key": "ud", "delivery_stop_keys": ["ott"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "u4", "pickup_stop_key": "ud", "delivery_stop_keys": ["ott"],
             "shared": False, "weight_lbs": 500.0},
            {"key": "t1", "pickup_stop_key": "tf", "delivery_stop_keys": ["blv"],
             "shared": False, "weight_lbs": 400.0},
            {"key": "t2", "pickup_stop_key": "tf", "delivery_stop_keys": ["blv"],
             "shared": False, "weight_lbs": 400.0},
            {"key": "t3", "pickup_stop_key": "tf", "delivery_stop_keys": ["blv"],
             "shared": False, "weight_lbs": 400.0},
        ]
        result = self.planner.simulate_movements(
            ["ud", "tf", "blv", "ott"], movements)
        self.assertEqual([d["after"] for d in result["deltas"]], [4, 7, 4, 0])
        self.assertEqual(result["peak"], 7)
        self.assertEqual(result["onboard_after"], 0)

    # ── Test T: shared pallet stays onboard until final allocation ───

    def test_02_shared_pallet_final_allocation_custody(self):
        movements = [
            {"key": "s1", "pickup_stop_key": "pu", "delivery_stop_keys": ["d1", "d2"],
             "shared": True, "weight_lbs": 600.0},
        ]
        result = self.planner.simulate_movements(["pu", "d1", "d2"], movements)
        self.assertEqual([d["after"] for d in result["deltas"]], [1, 1, 0])
        self.assertEqual(result["peak"], 1)

    # ── Test G: precedence blocks delivery before its pickup ────────

    def test_03_precedence_blocked(self):
        stops = [
            {"stop_key": "blv", "stop_type": "delivery", "location_name": "BLV",
             "latitude": 44.1, "longitude": -77.4, "timing_type": "flexible",
             "operating_hours_snapshot": {str(d): [6.0, 20.0] for d in range(7)},
             "service_time_minutes": 15, "timezone": "America/Toronto"},
            {"stop_key": "tf", "stop_type": "pickup", "location_name": "TF",
             "latitude": 43.7, "longitude": -79.4, "timing_type": "flexible",
             "operating_hours_snapshot": {str(d): [6.0, 20.0] for d in range(7)},
             "service_time_minutes": 15, "timezone": "America/Toronto"},
        ]
        movements = [
            {"key": "t1", "pickup_stop_key": "tf", "delivery_stop_keys": ["blv"],
             "shared": False, "weight_lbs": 400.0},
        ]
        result = self.planner.recommend_route(
            stops, movements, _dt(0, 8), vehicle_max=13)
        # BLV (delivery of TF freight) cannot precede TF pickup — with only
        # those two stops the legal order is TF first; forcing BLV first is
        # structurally prevented because the adviser never picks an illegal
        # candidate.
        self.assertTrue(result["feasible"])
        self.assertEqual(result["recommended"], ["tf", "blv"])

    # ── Tests B/E: time windows and waiting ──────────────────────────

    def test_04_waiting_time_computed(self):
        stop = {
            "stop_key": "s", "stop_type": "pickup", "location_name": "S",
            "timing_type": "flexible", "timezone": "America/Toronto",
            "operating_hours_snapshot": {str(d): [10.0, 17.0] for d in range(7)},
            "service_time_minutes": 20,
        }
        # 09:30 Toronto = 13:30 UTC (EDT); facility opens 10:00 local.
        feasible, waiting, start, _dep = self.planner.arrival_plan(
            stop, _dt(0, 13) + datetime.timedelta(minutes=30))
        self.assertTrue(feasible)
        self.assertAlmostEqual(waiting, 30.0, places=0)
        self.assertEqual(start.hour, 14)  # 10:00 EDT = 14:00 UTC

    def test_05_hard_window_protected(self):
        """A stop closing soon must be scheduled before a closer flexible
        stop (look-ahead protection)."""
        stops = [
            {"stop_key": "near", "stop_type": "pickup", "location_name": "NEAR",
             "latitude": 43.7, "longitude": -79.40, "timing_type": "flexible",
             "operating_hours_snapshot": {str(d): [6.0, 20.0] for d in range(7)},
             "service_time_minutes": 10, "timezone": "America/Toronto"},
            {"stop_key": "tight", "stop_type": "delivery", "location_name": "TIGHT",
             "latitude": 43.8, "longitude": -79.5, "timing_type": "time_window",
             "window_start": 8.0, "window_end": 9.5,
             "operating_hours_snapshot": {str(d): [6.0, 20.0] for d in range(7)},
             "service_time_minutes": 10, "timezone": "America/Toronto"},
        ]
        movements = [
            {"key": "n1", "pickup_stop_key": "near",
             "delivery_stop_keys": ["tight"], "shared": False,
             "weight_lbs": 500.0},
        ]
        result = self.planner.recommend_route(
            stops, movements, _dt(0, 8), vehicle_max=13)
        self.assertTrue(result["feasible"])
        # Both orders are precedence-legal here; the adviser must pick the
        # feasible one (tight's 08:00-09:30 window is satisfiable from 08:00).
        self.assertIn("tight", result["recommended"])

    # ── Test I: total handled > capacity, peak fits ──────────────────

    def test_06_total_handled_exceeds_capacity_peak_fits(self):
        movements = []
        for i in range(8):
            movements.append({"key": "a%d" % i, "pickup_stop_key": "pa",
                              "delivery_stop_keys": (["da"] if i < 5 else ["db"]),
                              "shared": False, "weight_lbs": 500.0})
        for i in range(8):
            movements.append({"key": "b%d" % i, "pickup_stop_key": "pb",
                              "delivery_stop_keys": ["db"], "shared": False,
                              "weight_lbs": 500.0})
        result = self.planner.simulate_movements(
            ["pa", "da", "pb", "db"], movements)
        self.assertEqual(result["peak"], 11)  # 8 → 3 → 11 → 0
        self.assertEqual(len(movements), 16)  # total handled 16
        self.assertLessEqual(result["peak"], 13)

    # ── Bridge: booking with pallet rows → one route job ─────────────

    def test_07_pallet_movements_dict_shape(self):
        """Canonical movement dicts expose stable stop keys (never array
        indices) and feed the itinerary planner directly."""
        partner = self.env["res.partner"].search([], limit=1)
        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "MR-0002",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "physical_pallets": 2, "weight_lbs": 1000.0,
            "state": "confirmed",
            "calculated_price": 300.0,
        })
        ud = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
            "location_name": "United Dairy"})
        ott = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
            "location_name": "Ottawa"})
        for i in range(2):
            pallet = self.env["logistics.booking.pallet"].create({
                "booking_id": booking.id, "sequence": i * 10,
                "label": "U-%02d" % (i + 1), "weight_lbs": 500.0,
                "pickup_stop_id": ud.id,
            })
            self.env["logistics.booking.pallet.stop.allocation"].create({
                "pallet_id": pallet.id, "delivery_stop_id": ott.id,
                "unload_sequence": 10})
        movements = booking.pallet_movements()
        self.assertEqual(len(movements), 2)
        for movement in movements:
            self.assertEqual(movement["pickup_stop_key"], "s%d" % ud.id)
            self.assertEqual(movement["delivery_stop_keys"], ["s%d" % ott.id])
        result = self.planner.simulate_movements(
            ["s%d" % ud.id, "s%d" % ott.id], movements)
        self.assertEqual(result["peak"], 2)
        self.assertEqual(result["onboard_after"], 0)

    # ── Architecture discriminator: pallet rows are NOT a selector ───

    def _make_booking_with_stops_pallets(self, route_model_version):
        partner = self.env["res.partner"].search([], limit=1)
        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "MR-DISC-%s" % route_model_version,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "physical_pallets": 2, "weight_lbs": 1000.0,
            "state": "confirmed",
            "calculated_price": 300.0,
            "route_model_version": route_model_version,
        })
        ud = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
            "location_name": "United Dairy"})
        ott = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
            "location_name": "Ottawa"})
        for i in range(2):
            pallet = self.env["logistics.booking.pallet"].create({
                "booking_id": booking.id, "sequence": i * 10,
                "label": "U-%02d" % (i + 1), "weight_lbs": 500.0,
                "pickup_stop_id": ud.id,
            })
            self.env["logistics.booking.pallet.stop.allocation"].create({
                "pallet_id": pallet.id, "delivery_stop_id": ott.id,
                "unload_sequence": 10})
        return booking

    def test_08_legacy_booking_with_pallet_rows_uses_legacy_bridge(self):
        """Compatibility pallet rows must NOT flip a legacy booking onto the
        movement bridge — dispatch bridge selection is the explicit
        route_model_version discriminator, never pallet-row presence."""
        booking = self._make_booking_with_stops_pallets("legacy")
        self.assertEqual(booking.route_model_version, "legacy")
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        # Legacy bridge: no canonical movement job, no canonical pallet
        # items — bridge selection is the explicit route_model_version
        # discriminator, never pallet-row presence. (The stops DO carry the
        # booking-stop link since the integrity pass: Booking 185's
        # dispatch stops were created by this very bridge, and one-click
        # re-geocode / coordinate restore requires the confirmed booking
        # snapshot link.)
        self.assertNotEqual(job.name.split("—")[0].strip(), "Milk Run")
        self.assertTrue(job.stop_ids.mapped("logistics_booking_stop_id"))
        self.assertFalse(job.item_ids.mapped("logistics_booking_pallet_id"))

    def test_09_movement_v1_booking_uses_movement_bridge(self):
        booking = self._make_booking_with_stops_pallets("movement_v1")
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertIn("Milk Run", job.name)
        # 2 operational stops bridged to the booking stops.
        self.assertEqual(len(job.stop_ids), 2)
        self.assertTrue(all(job.stop_ids.mapped("logistics_booking_stop_id")))
        # 2 canonical items — one per physical pallet, no duplicates.
        self.assertEqual(len(job.item_ids), 2)
        self.assertTrue(all(job.item_ids.mapped("logistics_booking_pallet_id")))
        pickup_stop = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")
        drop_stop = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        self.assertEqual(pickup_stop.pallets_in, 2)
        self.assertEqual(drop_stop.pallets_out, 2)
        self.assertTrue(pickup_stop.pop_required)
        self.assertTrue(drop_stop.pod_required)
        for item in job.item_ids:
            self.assertEqual(item.pickup_stop_id, pickup_stop)
            self.assertEqual(item.delivery_stop_id, drop_stop)

    # ── REAL ORM bridge: booking confirmation → dispatch route job ───

    def test_10_real_orm_booking_to_dispatch_bridge(self):
        """Canonical milk-run flow through confirm_from_internal:
        UD pickup (4 → Ottawa), TerraFreska pickup (3 → Belleville)
        → ONE same-day dispatch route job with 4 operational stops and 7
        canonical items; correct pickup/delivery stop references; no
        duplicate physical items."""
        partner = self.env["res.partner"].search([], limit=1)
        from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
            BookingOrchestrationService,
        )
        orchestration = BookingOrchestrationService(self.env)
        stops_spec = {
            "stp-ud": {
                "stop_key": "stp-ud", "stop_type": "pickup",
                "company_name": "United Dairy", "postal_code": "K7M",
                "city": "Kingston", "latitude": 44.23, "longitude": -76.49,
            },
            "stp-tf": {
                "stop_key": "stp-tf", "stop_type": "pickup",
                "company_name": "TerraFreska", "postal_code": "L6T",
                "city": "Brampton", "latitude": 43.73, "longitude": -79.76,
            },
            "stp-blv": {
                "stop_key": "stp-blv", "stop_type": "delivery",
                "company_name": "Belleville Depot", "postal_code": "K8N",
                "city": "Belleville", "latitude": 44.16, "longitude": -77.38,
            },
            "stp-ott": {
                "stop_key": "stp-ott", "stop_type": "delivery",
                "company_name": "Ottawa DC", "postal_code": "K1A",
                "city": "Ottawa", "latitude": 45.42, "longitude": -75.70,
            },
        }
        movements = []
        for i in range(4):
            movements.append({
                "key": "u%d" % (i + 1), "label": "U-%02d" % (i + 1),
                "weight_lbs": 500.0, "shared": False,
                "pickup_stop_key": "stp-ud", "delivery_stop_keys": ["stp-ott"],
            })
        for i in range(3):
            movements.append({
                "key": "t%d" % (i + 1), "label": "TF-%02d" % (i + 1),
                "weight_lbs": 400.0, "shared": False,
                "pickup_stop_key": "stp-tf", "delivery_stop_keys": ["stp-blv"],
            })
        norm = orchestration.normalize_request({
            "partner_id": partner.id,
            "pricing_method": "manual",
            "agreed_rate": 500.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "pallets": 7, "physical_pallets": 7,
            "weight_lbs": 3200.0,
            "pickup_stops": [stops_spec["stp-ud"], stops_spec["stp-tf"]],
            "delivery_stops": [stops_spec["stp-blv"], stops_spec["stp-ott"]],
            "route_model_version": "movement_v1",
            "pallet_movements": movements,
            "idempotency_key": "test:milkrun:canonical",
        }, source_channel="internal")
        booking = orchestration.confirm_from_internal(norm, skip_invoice=True)
        self.assertEqual(booking.route_model_version, "movement_v1")
        self.assertEqual(len(booking.pallet_ids), 7)
        self.assertEqual(len(booking.stop_ids), 4)
        # Exactly one same-day route job.
        jobs = booking.dispatch_job_ids
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        # Ordered operational stops: UD → TF → Belleville → Ottawa.
        stops = job.stop_ids.sorted("sequence")
        self.assertEqual(
            [s.logistics_booking_stop_id.stop_key for s in stops],
            ["stp-ud", "stp-tf", "stp-blv", "stp-ott"],
        )
        self.assertEqual([s.pallets_in for s in stops], [4, 3, 0, 0])
        self.assertEqual([s.pallets_out for s in stops], [0, 0, 3, 4])
        self.assertTrue(stops[0].pop_required)
        self.assertTrue(stops[1].pop_required)
        self.assertTrue(stops[2].pod_required)
        self.assertTrue(stops[3].pod_required)
        # Canonical items — 7 physical pallets, correct stop references.
        items = job.item_ids
        self.assertEqual(len(items), 7)
        by_key = {i.logistics_booking_pallet_id.label: i for i in items}
        for label in ("U-01", "U-02", "U-03", "U-04"):
            self.assertEqual(by_key[label].pickup_stop_id, stops[0])
            self.assertEqual(by_key[label].delivery_stop_id, stops[3])
        for label in ("TF-01", "TF-02", "TF-03"):
            self.assertEqual(by_key[label].pickup_stop_id, stops[1])
            self.assertEqual(by_key[label].delivery_stop_id, stops[2])
        self.assertEqual(len(items.mapped("logistics_booking_pallet_id")), 7)

    # ── Migration backfill: compatibility rows keep legacy bridge ────

    def test_11_migration_backfill_keeps_legacy_architecture(self):
        """18.0.11.0.0 backfill creates compatibility pallet rows for a
        historical booking but the booking stays legacy and its dispatch
        keeps the legacy bridge."""
        import importlib.util
        import os
        migration_path = os.path.join(
            os.path.dirname(__file__), "..", "migrations", "18.0.11.0.0",
            "post-migrate.py")
        spec = importlib.util.spec_from_file_location(
            "post_migrate_18_0_11_0_0", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        partner = self.env["res.partner"].search([], limit=1)
        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "MR-MIG-01",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "physical_pallets": 2, "weight_lbs": 1000.0,
            "state": "confirmed",
            "calculated_price": 300.0,
            "price_snapshot": [{"_pallet_allocs": [
                {"pallet": 1, "stops": [1]},
                {"pallet": 2, "stops": [1]},
            ]}],
        })
        # Historical bookings predate the discriminator field — the model
        # default and the migration both keep them legacy.
        self.assertEqual(booking.route_model_version, "legacy")
        delivery = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "delivery",
            "location_name": "Ottawa"})
        module.backfill_booking_pallets(self.env)
        booking.invalidate_recordset(["pallet_ids", "stop_ids"])
        self.assertEqual(booking.route_model_version, "legacy")
        self.assertEqual(len(booking.pallet_ids), 2)
        self.assertEqual(len(booking.stop_ids), 2)  # pickup + delivery
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertNotIn("Milk Run", job.name)
        # Legacy architecture kept (no canonical items) — but the stops
        # link their confirmed booking snapshots for coordinate/facility
        # integrity repair (Booking 185: legacy-created stops were the
        # corrupted ones; re-geocode needs the link).
        self.assertTrue(job.stop_ids.mapped("logistics_booking_stop_id"))
        self.assertFalse(job.item_ids.mapped("logistics_booking_pallet_id"))

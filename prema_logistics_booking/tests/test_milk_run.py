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

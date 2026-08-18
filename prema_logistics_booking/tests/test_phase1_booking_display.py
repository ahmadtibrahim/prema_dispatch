"""PHASE 1 regression suite — booking display + multi-stop eligible dates
+ operational timezone.

Covers section 61 regression scenarios that live in the booking backend:
  1. simple 1 pickup / 1 delivery hides Milk-Run (single-pair route → no
     builder payloads; legacy and route engines return identical dates)
  2. multi-stop routes evaluate EVERY delivery stop — never just the first
  3. per-stop results cover every delivery exactly once (no duplicate
     "Pickup 1"-style phantom stops)
  4. eligible dates use canonical VehicleCapacityService capacity (layout
     rows / legacy fields) — never a hardcoded 12/13/14
  5. shared pallets count ONCE: capacity is checked against
     physical_pallets, not deliveries × pallets
  6. equipment (reefer) and payload constraints enforced on the pickup
     departure vehicle
  7. operational timezone: "today" never flips at UTC midnight

The fixture graph is self-contained (own regions/corridor/vehicle) and
sits at ocean coordinates so production region polygons can never match.
"""
import datetime
from unittest.mock import Mock, patch

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.shipment_routing_service import (
    ShipmentRoutingService,
)

# Ocean coordinates — far from every production Ontario/Quebec polygon.
_RA = (43.05, -50.05)
_RB = (43.55, -49.05)
_RC = (44.05, -48.05)


def _poly(lat, lng, size=0.04):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lng - size, lat - size], [lng + size, lat - size],
            [lng + size, lat + size], [lng - size, lat + size],
            [lng - size, lat - size],
        ]],
    }


class TestPhase1BookingDisplay(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.svc = ShipmentRoutingService(cls.env)

        # ── Country / state — REAL Canada/ON (network-enabled) ────────
        cls.country = cls.env.ref("base.ca")
        cls.country.logistics_network_enabled = True
        cls.state = cls.env["res.country.state"].search(
            [("country_id", "=", cls.country.id), ("code", "=", "ON")], limit=1,
        )
        cls.state.logistics_network_enabled = True

        # ── Regions: A origin, B served delivery, C UNSERVED delivery ─
        def _region(code, name, lat, lng):
            return cls.env["logistics.region"].create({
                "code": code, "name": name,
                "is_official_ltl_region": True, "active": True,
                "boundary_status": "approved",
                "boundary_area_km2": 40.0,
                "country_id": cls.country.id, "state_id": cls.state.id,
                "polygon_geojson": str(_poly(lat, lng)).replace("'", '"'),
            })

        cls.r_a = _region("P1A", "P1 Origin", *_RA)
        cls.r_b = _region("P1B", "P1 Served Delivery", *_RB)
        cls.r_c = _region("P1C", "P1 Unserved Delivery", *_RC)

        # ── FSAs ──────────────────────────────────────────────────────
        # Codes verified ABSENT from the prod-copy test DB (P1A/P1B/P1C
        # are REAL Mississauga FSAs — the fsa_uniq constraint rejects
        # them; these Z-series codes are unallocated).
        def _fsa(fsa, region):
            return cls.env["logistics.fsa"].create({
                "fsa": fsa, "province": "ON", "display_city": region.name,
                "region_id": region.id,
                "pickup_supported": True, "delivery_supported": True,
            })

        cls.fsa_a = _fsa("Z9X", cls.r_a)
        cls.fsa_b = _fsa("Z8X", cls.r_b)
        cls.fsa_c = _fsa("Z7X", cls.r_c)

        # ── Vehicle with layout rows (canonical capacity authority) ───
        cls.brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1) or \
            cls.env["fleet.vehicle.model.brand"].create({"name": "P1-BRAND"})
        model = cls.env["fleet.vehicle.model"].create(
            {"name": "P1-MODEL", "brand_id": cls.brand.id})
        cls.truck = cls.env["fleet.vehicle"].create({
            "name": "P1-Truck", "license_plate": "P1TRK",
            "model_id": model.id,
            "x_operational_logistics": True,
            "x_max_payload_lbs": 40000.0,
            "x_reefer": False,
            "straight_pallet_capacity": 0,   # legacy fields EMPTY: layout rows rule
            "pin_wheel_pallet_capacity": 0,
            "turned_pallet_capacity": 0,
        })
        cls.env["fleet.vehicle.pallet.layout"].create({
            "vehicle_id": cls.truck.id, "code": "standard", "name": "Standard",
            "layout_type": "standard", "max_pallets": 14, "is_default": True,
            "sequence": 10,
        })
        cls.env["fleet.vehicle.pallet.layout"].create({
            "vehicle_id": cls.truck.id, "code": "pinwheel", "name": "Pinwheel",
            "layout_type": "pinwheel", "max_pallets": 16, "is_default": False,
            "sequence": 20,
        })

        # ── Corridor A → B (operates EVERY day; direction eastbound) ──
        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "P1-CORRIDOR-A-B", "direction": "eastbound",
            "phase": 1, "truck_slot": 1,
            "operate_monday": True, "operate_tuesday": True,
            "operate_wednesday": True, "operate_thursday": True,
            "operate_friday": True, "operate_saturday": True,
            "operate_sunday": True,
            "full_distance_km": 120.0, "planned_pallets": 8,
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.r_a.id,
             "name": "P1-A", "pickup_allowed": True, "delivery_allowed": False,
             "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.r_b.id,
             "name": "P1-B", "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 120.0},
        ])

        # ── Direct rule A→B (DIRECT_ALLOWED path in the leg probe) ────
        cls.env["logistics.direct.delivery.rule"].create({
            "origin_region_id": cls.r_a.id,
            "destination_region_id": cls.r_b.id,
            "direction": "both",
            "direct_same_day_allowed": True,
            "hub_transfer_required": False,
        })

        # ── Departures: next 8 calendar days (each gets a truck) ──────
        cls.departure_dates = [
            datetime.date.today() + datetime.timedelta(days=offset)
            for offset in range(1, 9)
        ]
        cls.departures = []
        for dep_date in cls.departure_dates:
            dep = cls.env["logistics.corridor.departure"].create({
                "corridor_id": cls.corridor.id,
                "departure_date": dep_date,
                "departure_time": 1.0, "cutoff_time": 16.0,
                "status": "scheduled", "max_capacity": 12,
                "vehicle_id": cls.truck.id,
            })
            cls.departures.append(dep)

    # ── Helpers ──────────────────────────────────────────────────────

    def _date_strs(self, dates):
        return {str(d) for d in dates}

    def _stops(self, deliveries):
        """pickup stop at R-A + the given delivery tuples
        [(lat, lng, stop_key, city), ...]"""
        stops = [{
            "stop_type": "pickup", "latitude": _RA[0], "longitude": _RA[1],
            "stop_key": "pu-1", "city": "P1 Origin",
        }]
        for i, (lat, lng, key, city) in enumerate(deliveries, start=1):
            stops.append({
                "stop_type": "delivery", "latitude": lat, "longitude": lng,
                "stop_key": key, "city": city,
            })
        return stops

    # ── 1. Simple 1+1 — one code path, no builder payloads ──────────

    def test_01_single_pair_legacy_and_route_engine_identical(self):
        """Legacy single-pair signature delegates to the route engine —
        one code path. Both must return exactly the 8 departure dates."""
        legacy = self.svc.get_eligible_pickup_dates(
            _RA[0], _RA[1], _RB[0], _RB[1],
            pallets=1, weight_lbs=500, equipment="dry")
        route = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        expected = self._date_strs(self.departure_dates)
        self.assertEqual(
            self._date_strs([d["date"] for d in legacy]), expected,
            "legacy single-pair must return every departure date")
        self.assertEqual(
            self._date_strs([d["date"] for d in route]), expected,
            "route engine must return every departure date")
        self.assertEqual(len(legacy), len(self.departures))
        self.assertEqual(len(route), len(self.departures))

    def test_02_single_pair_returns_capacity_layout(self):
        """The calendar carries canonical capacity answers — the
        vehicle's layout rows (14 default / 16 max), never a hardcoded
        number."""
        dates = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertTrue(dates)
        first = dates[0]
        self.assertEqual(first["max_capacity"], 16)
        self.assertEqual(first["remaining_sellable_capacity"], 16)
        self.assertEqual(first["layout_code"], "standard")
        self.assertEqual(first["layout_name"], "Standard")

    # ── 2. Multi-stop: EVERY delivery stop considered ────────────────

    def test_03_unserved_second_delivery_kills_all_dates(self):
        """THE core regression: availability must never be computed from
        the first delivery only. R-C resolves to a region but NO corridor
        serves it → the complete route can never move → zero eligible
        dates. The same origin with only the served delivery still gets
        all 8 dates."""
        multi = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([
                (_RB[0], _RB[1], "del-1", "P1 B"),
                (_RC[0], _RC[1], "del-2", "P1 C"),
            ]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertEqual(multi, [], "unserved second delivery must make "
                                   "the whole route ineligible")

        control = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertEqual(
            self._date_strs([d["date"] for d in control]),
            self._date_strs(self.departure_dates))

    def test_04_per_stop_covers_every_delivery_once(self):
        """Two deliveries to the SAME region: per_stop must contain both,
        distinct stop_keys, no loss and no duplication."""
        dates = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([
                (_RB[0], _RB[1], "del-1", "P1 B"),
                (_RB[0] + 0.01, _RB[1] + 0.01, "del-2", "P1 B2"),
            ]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertTrue(dates)
        per_stop = dates[0]["per_stop"]
        keys = [s["stop_key"] for s in per_stop]
        self.assertEqual(keys, ["del-1", "del-2"],
                         "every delivery stop present exactly once, in route order")
        self.assertTrue(all(s["feasible"] for s in per_stop))

    # ── 3. Canonical capacity (never hardcoded 12/13/14) ─────────────

    def test_05_pallets_beyond_legacy_13_fit_layout_rows(self):
        """14 pallets fit the 14-pallet standard layout — the old
        hardcoded capacity 13 would have rejected this. 16 fit via
        pinwheel; 17 fit nothing."""
        for pallets, expected in ((14, True), (16, True), (17, False)):
            dates = self.svc.get_eligible_pickup_dates_for_route(
                self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
                physical_pallets=pallets, weight_lbs=500, equipment="dry")
            self.assertEqual(bool(dates), expected,
                             "pallets=%d eligibility" % pallets)

    def test_06_reserved_positions_reduce_remaining(self):
        """A confirmed 5-pallet booking on departure[0] leaves 11 — the
        date is blocked for 12 pallets, still open for 11."""
        partner = self.env["res.partner"].search([], limit=1)
        self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "P1-RES-%d" % (
                len(self.env["logistics.booking"].search([])) + 1),
            "departure_id": self.departures[0].id,
            "pickup_fsa_id": self.fsa_a.id,
            "delivery_fsa_id": self.fsa_b.id,
            "pallets": 5, "physical_pallets": 5,
            "shipment_type": "ltl", "temperature_mode": "dry",
            "weight_lbs": 2500.0, "state": "confirmed",
            "calculated_price": 100.0,
        })
        self.env.cr.flush()

        blocked = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=12, weight_lbs=500, equipment="dry")
        blocked_dates = self._date_strs([d["date"] for d in blocked])
        self.assertNotIn(str(self.departure_dates[0]), blocked_dates,
                         "12 pallets must not fit a 11-remaining departure")
        self.assertEqual(len(blocked_dates), len(self.departures) - 1)

        open_for = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=11, weight_lbs=500, equipment="dry")
        self.assertIn(str(self.departure_dates[0]),
                      self._date_strs([d["date"] for d in open_for]))

    def test_07_shared_pallets_count_once(self):
        """Two delivery stops, ONE physical pallet: the capacity check
        uses physical_pallets (1), never deliveries × pallets (2)."""
        multi = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([
                (_RB[0], _RB[1], "del-1", "P1 B"),
                (_RB[0] + 0.01, _RB[1] + 0.01, "del-2", "P1 B2"),
            ]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertEqual(
            self._date_strs([d["date"] for d in multi]),
            self._date_strs(self.departure_dates),
            "1 shared pallet across 2 stops fits every departure")
        self.assertEqual(multi[0]["remaining_capacity"], 16)

    # ── 4. Equipment + payload on the departure vehicle ──────────────

    def test_08_reefer_requested_on_dry_truck_rejected(self):
        """temperature_compat: a non-reefer truck can never serve a
        reefer booking — zero eligible dates. Dry still eligible."""
        reefer = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=500, equipment="reefer")
        self.assertEqual(reefer, [], "non-reefer vehicle rejects reefer")

        dry = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=500, equipment="dry")
        self.assertTrue(dry)

    def test_09_payload_over_max_rejected(self):
        """Vehicle payload 40,000 lb: 50,000 lb shipment never fits;
        30,000 lb fits."""
        over = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=50000, equipment="dry")
        self.assertEqual(over, [], "over-payload shipment must be ineligible")

        under = self.svc.get_eligible_pickup_dates_for_route(
            self._stops([(_RB[0], _RB[1], "del-1", "P1 B")]),
            physical_pallets=1, weight_lbs=30000, equipment="dry")
        self.assertTrue(under)

    # ── 5. Operational timezone near midnight ────────────────────────

    def test_10_op_today_never_flips_at_utc_midnight(self):
        """_op_today() is the OPERATIONAL calendar date (Toronto by
        default). 23:30 UTC on Aug 18 = 19:30 Toronto → Aug 18.
        00:30 UTC Aug 19 = 20:30 Toronto Aug 18 → STILL Aug 18. The old
        datetime.utcnow() would have said Aug 19 in the second case."""
        module = "odoo.addons.prema_logistics_booking.services.shipment_routing_service"
        # 2026-08-18 is a Tuesday; Toronto is EDT (UTC-4) in August.
        for utc_naive, expected in (
                (datetime.datetime(2026, 8, 18, 23, 30), datetime.date(2026, 8, 18)),
                (datetime.datetime(2026, 8, 19, 0, 30), datetime.date(2026, 8, 18)),
                (datetime.datetime(2026, 8, 19, 4, 30), datetime.date(2026, 8, 19)),
        ):
            with patch(module + ".datetime",
                       Mock(utcnow=lambda: utc_naive)):
                self.assertEqual(
                    self.svc._op_today(), expected,
                    "operational date for UTC %s" % utc_naive)

    def test_11_op_tz_configurable(self):
        """The operational timezone reads the ir.config_parameter; a
        bad value falls back to Toronto."""
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("prema_logistics_booking.operational_tz", "America/Vancouver")
        try:
            self.assertEqual(str(self.svc._op_tz()), "America/Vancouver")
        finally:
            Param.set_param("prema_logistics_booking.operational_tz", "America/Toronto")
        self.assertEqual(str(self.svc._op_tz()), "America/Toronto")
        Param.set_param("prema_logistics_booking.operational_tz", "Not/AZone")
        try:
            self.assertEqual(str(self.svc._op_tz()), "America/Toronto")
        finally:
            Param.set_param("prema_logistics_booking.operational_tz", "America/Toronto")

"""LTL Additional Stop Charge + multi-stop portal quote regression.

The additional-stop charge is corridor-configured: delivery stops are
grouped by saved-location city, and each city with N stops adds (N - 1)
charges. 6 stops with 3 in one city → 2 charges; 4 stops in one city →
3 charges.
"""
import datetime
import json

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestLtlAdditionalStops(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        cls.country_ca = cls.env.ref("base.ca")
        cls.country_ca.logistics_network_enabled = True
        cls.state_on = cls.env["res.country.state"].search(
            [("country_id", "=", cls.country_ca.id), ("code", "=", "ON")], limit=1,
        )
        cls.state_on.logistics_network_enabled = True

        def _square(lng, lat):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - 0.05, lat - 0.04], [lng + 0.05, lat - 0.04],
                [lng + 0.05, lat + 0.04], [lng - 0.05, lat + 0.04],
                [lng - 0.05, lat - 0.04],
            ]]})

        cls.gta = Region.create({
            "code": "ADS-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-87.5, 49.0),
        })
        cls.ott = Region.create({
            "code": "ADS-OTT", "name": "Ottawa", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-86.5, 49.8),
        })
        cls.hub = cls.env["logistics.hub"].create({
            "name": "ADS Hub", "public_name": "ADS Hub", "code": "ADS-HUB",
            "canonical_region_id": cls.gta.id, "is_default": True,
            "latitude": 49.0, "longitude": -87.5,
        })

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "ADS-Eastbound",
            "direction": "eastbound",
            "rate_per_km": 3.5,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
            "ltl_additional_stop_charge": 75.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
            "enable_ftl": True,
            "ftl_threshold_pallets": 10,
            "ftl_rate_per_km": 3.0,
            "ftl_behavior": "auto_price",
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 507.6},
        ])
        cls.env["logistics.pallet.volume.tier"].create([
            {"corridor_id": cls.corridor.id, "min_pallets": 2, "max_pallets": 3,
             "discount_pct": 5.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 4, "max_pallets": 6,
             "discount_pct": 10.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 7, "max_pallets": 9,
             "discount_pct": 15.0, "pricing_type": "ltl"},
        ])

        cls.partner = cls.env["res.partner"].create({"name": "ADS Customer"})
        cls.wednesday = cls._next("wednesday")

    @classmethod
    def _next(cls, weekday):
        day = datetime.date.today()
        while day.strftime("%A").lower() != weekday:
            day += datetime.timedelta(days=1)
        return day

    def _stop(self, city, lat=None, lng=None):
        stop = {"city": city, "postal_code": "X0X"}
        if lat and lng:
            stop.update({"latitude": lat, "longitude": lng})
        return stop

    def _quote(self, delivery_stops, pallets, weight, load_type="ltl",
               pallet_allocations=None, shared=False):
        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [{"latitude": 49.0, "longitude": -87.5, "postal_code": "X0Z"}],
            "delivery_stops": delivery_stops,
            "pallets": pallets,
            "weight_lbs": weight,
            "load_type": load_type,
            "equipment_type": "dry",
            "requested_pickup_date": self.wednesday,
            "pallet_allocations": pallet_allocations or [],
            "shared_pallet_mode": shared,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1,
        )
        return result, session

    def _helper_charge(self, cities, corridor, load_type="ltl"):
        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [{"postal_code": "X0Z"}],
            "delivery_stops": [self._stop(city) for city in cities],
            "pallets": 4,
            "weight_lbs": 2000.0,
            "load_type": load_type,
            "equipment_type": "dry",
        }, source_channel="internal")
        return svc._ltl_additional_stop_charge(norm, corridor)

    # ── Helper-level: same-city grouping rules ───────────────────────

    def test_01_one_stop_no_charge(self):
        count, rate, total = self._helper_charge(["Ottawa"], self.corridor)
        self.assertEqual((count, total), (0, 0.0))

    def test_02_two_same_city_stops_one_charge(self):
        count, rate, total = self._helper_charge(["Ottawa", "Ottawa"], self.corridor)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(rate, 75.0, places=2)
        self.assertAlmostEqual(total, 75.0, places=2)

    def test_03_three_same_city_stops_two_charges(self):
        count, rate, total = self._helper_charge(
            ["Ottawa", "Ottawa", "Ottawa"], self.corridor)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(total, 150.0, places=2)

    def test_03b_four_same_city_stops_three_charges(self):
        count, rate, total = self._helper_charge(
            ["Ottawa"] * 4, self.corridor)
        self.assertEqual(count, 3)
        self.assertAlmostEqual(total, 225.0, places=2)

    def test_03c_six_stops_three_matching_two_charges(self):
        count, rate, total = self._helper_charge(
            ["Belleville", "Ottawa", "Ottawa", "Ottawa", "Montreal", "Toronto"],
            self.corridor)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(total, 150.0, places=2)

    def test_04_zero_rate_no_charge(self):
        zero = self.env["logistics.corridor"].create({
            "name": "ADS-Zero", "direction": "eastbound",
        })
        count, rate, total = self._helper_charge(["Ottawa", "Ottawa"], zero)
        # 2 same-city stops = 1 additional stop, but a zero rate adds nothing.
        self.assertEqual((count, rate, total), (1, 0.0, 0.0))

    def test_05_dynamic_per_corridor_rates(self):
        corridor_b = self.env["logistics.corridor"].create({
            "name": "ADS-B", "direction": "eastbound",
            "ltl_additional_stop_charge": 125.0,
        })
        _, _, total_a = self._helper_charge(["Ottawa", "Ottawa"], self.corridor)
        _, _, total_b = self._helper_charge(["Ottawa", "Ottawa"], corridor_b)
        self.assertAlmostEqual(total_a, 75.0, places=2)
        self.assertAlmostEqual(total_b, 125.0, places=2)

    def test_06_different_cities_no_charge(self):
        count, rate, total = self._helper_charge(["Belleville", "Ottawa"], self.corridor)
        self.assertEqual(count, 0)
        self.assertAlmostEqual(total, 0.0, places=2)

    def test_08_ftl_never_charged(self):
        count, rate, total = self._helper_charge(
            ["Ottawa", "Ottawa"], self.corridor, load_type="ftl")
        self.assertEqual((count, rate, total), (0, 0.0, 0.0))

    # ── Portal-level multi-stop quotes ───────────────────────────────

    def test_13_two_ottawa_stops_quote_succeeds_with_charge(self):
        stops = [
            self._stop("Ottawa", 49.8, -86.52),
            self._stop("Ottawa", 49.8, -86.48),
        ]
        result, session = self._quote(stops, pallets=4, weight=2000.0)
        pricing = (session.route_snapshot or {}).get("pricing") or {}
        # 799.47 (discounted transportation) + 75.00 (one extra Ottawa stop).
        self.assertAlmostEqual(session.calculated_price, 874.47, places=2)
        self.assertAlmostEqual(result["calculated_price"], 874.47, places=2)
        self.assertEqual(pricing["additional_stop_count"], 1)
        self.assertAlmostEqual(pricing["additional_stop_rate"], 75.0, places=2)
        self.assertAlmostEqual(pricing["additional_stop_total"], 75.0, places=2)
        self.assertTrue(any(
            line.get("label", "").startswith("Additional Stop")
            for line in session.price_snapshot
        ))
        # 500-fix regression: allocations accessor exists and works.
        self.assertEqual(session._get_pallet_allocations(), [])

    def test_14_belleville_ottawa_quote_succeeds_no_charge(self):
        stops = [
            self._stop("Belleville", 49.6, -87.2),
            self._stop("Ottawa", 49.8, -86.5),
        ]
        result, session = self._quote(stops, pallets=4, weight=2000.0)
        self.assertAlmostEqual(session.calculated_price, 799.47, places=2)
        pricing = (session.route_snapshot or {}).get("pricing") or {}
        self.assertEqual(pricing["additional_stop_count"], 0)

    def test_15_split_allocation_8_pallets_3_5(self):
        allocs = [
            {"pallet": 1, "stops": [1]},
            {"pallet": 2, "stops": [1]},
            {"pallet": 3, "stops": [1]},
            {"pallet": 4, "stops": [2]},
            {"pallet": 5, "stops": [2]},
            {"pallet": 6, "stops": [2]},
            {"pallet": 7, "stops": [2]},
            {"pallet": 8, "stops": [2]},
        ]
        stops = [
            self._stop("Ottawa", 49.8, -86.52),
            self._stop("Ottawa", 49.8, -86.48),
        ]
        result, session = self._quote(stops, pallets=8, weight=4000.0,
                                      pallet_allocations=allocs)
        self.assertEqual(session.physical_pallets, 8)
        self.assertEqual(session.pallets, 8)
        # 8 pallets: 1,776.60 → 15% = 1,510.11 → +75.00 = 1,585.11.
        self.assertAlmostEqual(session.calculated_price, 1585.11, places=2)
        # Allocations survive the snapshot round-trip (the 500 fix).
        extracted = session._get_pallet_allocations()
        self.assertEqual(len(extracted), 8)
        self.assertEqual(len(session.delivery_stop_ids), 2)

    def test_16_shared_pallet_mode_quote_succeeds(self):
        stops = [
            self._stop("Ottawa", 49.8, -86.52),
            self._stop("Ottawa", 49.8, -86.48),
        ]
        result, session = self._quote(stops, pallets=4, weight=2000.0, shared=True)
        self.assertTrue(session.shared_pallet_mode)
        self.assertAlmostEqual(session.calculated_price, 874.47, places=2)

    def test_17_empty_allocation_friendly_no_500(self):
        stops = [self._stop("Ottawa", 49.8, -86.5)]
        result, session = self._quote(stops, pallets=4, weight=2000.0,
                                      pallet_allocations=[])
        self.assertTrue(session)
        self.assertAlmostEqual(session.calculated_price, 799.47, places=2)

    def test_18_direction_mismatch_friendly_no_service(self):
        from odoo.exceptions import UserError
        tuesday = self.wednesday - datetime.timedelta(days=1)
        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [{"latitude": 49.0, "longitude": -87.5, "postal_code": "X0Z"}],
            "delivery_stops": [self._stop("Ottawa", 49.8, -86.5)],
            "pallets": 4,
            "weight_lbs": 2000.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": tuesday,
        }, source_channel="portal")
        with self.assertRaises(UserError):
            svc.prepare_quote(norm)

    def test_19_volume_discount_still_applies_with_stops(self):
        stops = [
            self._stop("Ottawa", 49.8, -86.52),
            self._stop("Ottawa", 49.8, -86.48),
        ]
        result, session = self._quote(stops, pallets=4, weight=2000.0)
        pricing = (session.route_snapshot or {}).get("pricing") or {}
        self.assertAlmostEqual(pricing["volume_discount_pct"], 10.0, places=2)
        # Transportation discounted first, then the stop charge added once.
        self.assertAlmostEqual(pricing["leg_total"], 888.30, places=2)
        self.assertAlmostEqual(session.calculated_price, 874.47, places=2)

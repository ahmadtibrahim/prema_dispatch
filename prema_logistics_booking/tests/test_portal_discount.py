"""Portal quote path — LTL pallet-volume discount must reach the pricing
session and the rendered total.

Reproduces the live Brampton → Ottawa defect where prepare_quote rebuilt
the total from raw leg prices and dropped the discount applied inside
plan_route.
"""
import datetime
import json
from decimal import Decimal, ROUND_HALF_UP

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestPortalDiscount(TransactionCase):

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
                [lng - 0.1, lat - 0.05], [lng + 0.1, lat - 0.05],
                [lng + 0.1, lat + 0.05], [lng - 0.1, lat + 0.05],
                [lng - 0.1, lat - 0.05],
            ]]})

        cls.gta = Region.create({
            "code": "PDC-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-87.5, 49.0),
        })
        cls.ott = Region.create({
            "code": "PDC-OTT", "name": "Ottawa", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-86.5, 49.8),
        })
        cls.hub = cls.env["logistics.hub"].create({
            "name": "PDC Hub", "public_name": "PDC Hub", "code": "PDC-HUB",
            "canonical_region_id": cls.gta.id, "is_default": True,
            "latitude": 49.0, "longitude": -87.5,
        })

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "PDC-Eastbound",
            "direction": "eastbound",
            "rate_per_km": 3.5,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
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
        Tier = cls.env["logistics.pallet.volume.tier"]
        Tier.create([
            {"corridor_id": cls.corridor.id, "min_pallets": 2, "max_pallets": 3,
             "discount_pct": 5.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 4, "max_pallets": 6,
             "discount_pct": 10.0, "pricing_type": "ltl"},
            {"corridor_id": cls.corridor.id, "min_pallets": 7, "max_pallets": 9,
             "discount_pct": 15.0, "pricing_type": "ltl"},
        ])

        cls.partner = cls.env["res.partner"].create({"name": "PDC Customer"})
        cls.wednesday = cls._next("wednesday")

    @classmethod
    def _next(cls, weekday):
        day = datetime.date.today()
        while day.strftime("%A").lower() != weekday:
            day += datetime.timedelta(days=1)
        return day

    def _quote(self, pallets, weight_lbs, load_type="ltl"):
        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [{"latitude": 49.0, "longitude": -87.5, "postal_code": "X0X"}],
            "delivery_stops": [{"latitude": 49.8, "longitude": -86.5, "postal_code": "X0Y"}],
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "load_type": load_type,
            "equipment_type": "dry",
            "requested_pickup_date": self.wednesday,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1,
        )
        return result, session

    def _pricing(self, session):
        return (session.route_snapshot or {}).get("pricing") or {}

    @staticmethod
    def _half_up(value):
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _assert_discounted(self, pallets, expected_pct, expected_final):
        result, session = self._quote(pallets, pallets * 500.0)
        pricing = self._pricing(session)
        self.assertAlmostEqual(pricing["volume_discount_pct"], expected_pct, places=2)
        self.assertAlmostEqual(session.calculated_price, expected_final, places=2)
        self.assertAlmostEqual(result["calculated_price"], expected_final, places=2)
        return session, pricing

    def test_01_two_pallets_5_percent(self):
        _, pricing = self._assert_discounted(2, 5.0, 421.94)
        self.assertAlmostEqual(pricing["leg_total"], 444.15, places=2)

    def test_02_four_pallets_10_percent(self):
        session, pricing = self._assert_discounted(4, 10.0, 799.47)
        self.assertAlmostEqual(pricing["leg_total"], 888.30, places=2)
        self.assertAlmostEqual(pricing["volume_discount_amount"], -88.83, places=2)
        # The session's price snapshot carries the discount line.
        self.assertTrue(any(
            line.get("label", "").startswith("Volume discount")
            for line in session.price_snapshot
        ))

    def test_03_six_pallets_10_percent(self):
        # 1,332.45 × 0.90 = 1,199.205 → Odoo HALF_UP = 1,199.21.
        _, pricing = self._assert_discounted(6, 10.0, 1199.21)
        self.assertAlmostEqual(pricing["leg_total"], 1332.45, places=2)

    def test_04_seven_pallets_15_percent(self):
        session, pricing = self._assert_discounted(7, 15.0, 1321.35)
        expected = self._half_up(Decimal(str(pricing["leg_total"])) * Decimal("0.85"))
        self.assertAlmostEqual(pricing["final_transportation"], expected, places=2)
        self.assertAlmostEqual(session.calculated_price, expected, places=2)

    def test_05_eight_pallets_15_percent(self):
        _, pricing = self._assert_discounted(8, 15.0, 1510.11)
        self.assertAlmostEqual(pricing["leg_total"], 1776.60, places=2)

    def test_06_discounts_disabled_no_discount(self):
        self.corridor.enable_volume_discounts = False
        _, session = self._quote(4, 2000.0)
        pricing = self._pricing(session)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 0.0, places=2)
        self.assertAlmostEqual(session.calculated_price, 888.30, places=2)

    def test_07_no_matching_tier_no_discount(self):
        _, session = self._quote(1, 500.0)
        pricing = self._pricing(session)
        self.assertAlmostEqual(pricing["volume_discount_pct"], 0.0, places=2)
        self.assertAlmostEqual(session.calculated_price, pricing["leg_total"], places=2)

    def test_08_ftl_no_ltl_discount(self):
        _, session = self._quote(10, 5000.0, load_type="ftl")
        snapshot = session.route_snapshot or {}
        self.assertEqual(snapshot.get("pricing_mode"), "ftl")
        pricing = snapshot.get("pricing") or {}
        self.assertAlmostEqual(pricing.get("volume_discount_pct", 0.0), 0.0, places=2)
        # FTL: 507.6 km × corridor $3.00/km = 1,522.80 (no regional rule).
        self.assertAlmostEqual(session.calculated_price, 1522.80, places=2)

    def test_09_booking_minimum_stays_floor_only(self):
        # Floor semantics are covered end-to-end in test_ltl_routing_fixes
        # (test_K); here the discounted session value for 2 pallets must
        # simply remain the discounted amount (well above the $150 floor).
        _, session = self._quote(2, 1000.0)
        self.assertAlmostEqual(session.calculated_price, 421.94, places=2)

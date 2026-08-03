"""Pricing integration — offering resolution, schedule, route snapshot, booking legs."""
from datetime import date, timedelta
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestOfferingResolution(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r1 = cls.env['logistics.region'].create({'code': 'PIA', 'name': 'PI Region A'})
        cls.r2 = cls.env['logistics.region'].create({'code': 'PIB', 'name': 'PI Region B'})
        cls.fsa1 = cls.env['logistics.fsa'].create({
            'fsa': 'P1A', 'region_id': cls.r1.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa2 = cls.env['logistics.fsa'].create({
            'fsa': 'P2B', 'region_id': cls.r2.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.lane = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r1.id, 'destination_region_id': cls.r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        cls.slevel = cls.env['logistics.service.level'].create({
            'code': 'PI_LVL', 'name': 'PI Level', 'reefer_food_eligible': True,
        })
        cls._make_offering_rp('ltl', 1600.0, 8)
        cls.svc = PricingService(cls.env)

    @classmethod
    def _make_offering_rp(cls, shipment_type, revenue, tlq):
        off = cls.env['logistics.service.offering'].create({
            'lane_id': cls.lane.id, 'service_level_id': cls.slevel.id,
            'temperature_mode': 'dry', 'shipment_type': shipment_type, 'active': True,
        })
        cls.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True,
            'delivery_offset_type': 'next_day', 'active': True,
        })
        rp = cls.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': revenue,
            'target_load_quantity': tlq, 'active': True,
            'effective_from': date.today() - timedelta(days=30),
        })
        return rp

    def test_01_ltl_selects_ltl_offering(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, r.reason)
        self.assertEqual(r.route_snapshot["shipment_type"], "ltl")

    def test_02_ftl_rejected_when_no_ftl_offering(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ftl", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertEqual(r.reason, "request_quote")

    def test_03_invalid_shipment_type_rejected(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "invalid", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertIn("invalid", r.reason)

    def test_04_invalid_equipment_rejected(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "invalid_eq", 1, 500)
        self.assertFalse(r.available)
        self.assertIn("invalid", r.reason)


class TestChilledFrozenMapping(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r1 = cls.env['logistics.region'].create({'code': 'CFA', 'name': 'CF A'})
        cls.r2 = cls.env['logistics.region'].create({'code': 'CFB', 'name': 'CF B'})
        cls.fsa1 = cls.env['logistics.fsa'].create({
            'fsa': 'C1A', 'region_id': cls.r1.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa2 = cls.env['logistics.fsa'].create({
            'fsa': 'C2B', 'region_id': cls.r2.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.lane = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r1.id, 'destination_region_id': cls.r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = cls.env['logistics.service.level'].create({
            'code': 'CF_LVL', 'name': 'CF Level', 'reefer_food_eligible': True,
        })
        off = cls.env['logistics.service.offering'].create({
            'lane_id': cls.lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        cls.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True,
            'delivery_offset_type': 'next_day', 'active': True,
        })
        cls.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0,
            'target_load_quantity': 8, 'active': True,
            'effective_from': date.today() - timedelta(days=30),
        })
        cls.svc = PricingService(cls.env)

    def test_01_chilled_maps_to_reefer(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "chilled", 1, 500)
        self.assertTrue(r.available, f"Chilled should map to reefer: {r.reason}")

    def test_02_frozen_maps_to_reefer(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "frozen", 1, 500)
        self.assertTrue(r.available, f"Frozen should map to reefer: {r.reason}")

    def test_03_reefer_direct(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "reefer", 1, 500)
        self.assertTrue(r.available)

    def test_04_dry_baseline(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)


class TestAmbiguousOfferings(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r1 = cls.env['logistics.region'].create({'code': 'AMA', 'name': 'AM A'})
        cls.r2 = cls.env['logistics.region'].create({'code': 'AMB', 'name': 'AM B'})
        cls.fsa1 = cls.env['logistics.fsa'].create({
            'fsa': 'M1A', 'region_id': cls.r1.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa2 = cls.env['logistics.fsa'].create({
            'fsa': 'M2B', 'region_id': cls.r2.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.lane = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r1.id, 'destination_region_id': cls.r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = cls.env['logistics.service.level'].create({
            'code': 'AM_LVL', 'name': 'AM Level', 'reefer_food_eligible': True,
        })
        # Create TWO dry LTL offerings — ambiguous
        for i in range(2):
            off = cls.env['logistics.service.offering'].create({
                'lane_id': cls.lane.id, 'service_level_id': slevel.id,
                'temperature_mode': 'dry', 'shipment_type': 'ltl', 'active': True,
            })
            cls.env['logistics.lane.schedule'].create({
                'service_offering_id': off.id, 'cutoff_time': 16.0,
                'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
                'pickup_thursday': True, 'pickup_friday': True,
                'delivery_offset_type': 'next_day', 'active': True,
            })
            cls.env['logistics.rate.plan'].create({
                'service_offering_id': off.id, 'revenue_target': 1600.0,
                'target_load_quantity': 8, 'active': True,
                'effective_from': date.today() - timedelta(days=30),
            })
        cls.svc = PricingService(cls.env)

    def test_01_ambiguous_offerings_request_quote(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertFalse(r.available, "Ambiguous offerings must return request_quote")
        self.assertEqual(r.reason, "request_quote")


class TestRouteSnapshotPersistence(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r1 = cls.env['logistics.region'].create({'code': 'RSA', 'name': 'RS A'})
        cls.r2 = cls.env['logistics.region'].create({'code': 'RSB', 'name': 'RS B'})
        cls.fsa1 = cls.env['logistics.fsa'].create({
            'fsa': 'S1A', 'region_id': cls.r1.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.fsa2 = cls.env['logistics.fsa'].create({
            'fsa': 'S2B', 'region_id': cls.r2.id, 'pickup_supported': True, 'delivery_supported': True,
        })
        cls.lane = cls.env['logistics.lane'].create({
            'origin_region_id': cls.r1.id, 'destination_region_id': cls.r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = cls.env['logistics.service.level'].create({
            'code': 'RS_LVL', 'name': 'RS Level', 'reefer_food_eligible': True,
        })
        off = cls.env['logistics.service.offering'].create({
            'lane_id': cls.lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        cls.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True,
            'delivery_offset_type': 'next_day', 'active': True,
        })
        cls.rp = cls.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0,
            'target_load_quantity': 8, 'active': True,
            'effective_from': date.today() - timedelta(days=30),
        })
        cls.svc = PricingService(cls.env)

    def test_01_route_snapshot_in_result(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        snap = r.route_snapshot
        self.assertTrue(snap)
        self.assertEqual(snap["leg_count"], 1)
        self.assertEqual(snap["legs"][0]["rate_plan_id"], self.rp.id)

    def test_02_schedule_dates_present(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available)
        self.assertIsNotNone(r.pickup_date)
        self.assertIsNotNone(r.delivery_date_estimate)
        self.assertTrue(r.pickup_date <= r.delivery_date_estimate)

    def test_03_capacity_gate_13_pallets(self):
        r = self.svc.calculate(self.fsa1, self.fsa2, "ltl", "dry", 13, 6500)
        self.assertFalse(r.available)
        self.assertIn("pallets", r.reason)

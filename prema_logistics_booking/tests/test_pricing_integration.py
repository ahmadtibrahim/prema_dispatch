"""Pricing integration — offering resolution, schedule, route snapshot, booking legs."""
from datetime import date, timedelta
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService


class TestCanonicalSelections(TransactionCase):
    """LTL/FTL offering selection, Dry/Reefer persistence, Chilled/Frozen compat."""

    def test_01_ltl_offering_selected(self):
        r1 = self.env['logistics.region'].create({'code': 'CS1', 'name': 'CS 1'})
        r2 = self.env['logistics.region'].create({'code': 'CS2', 'name': 'CS 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'S1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'S2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'CSLVL', 'name': 'CS Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'ltl', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ltl", "dry", 1, 500)
        self.assertTrue(r.available, r.reason or "not available")
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2)

    def test_02_ftl_rejected_when_no_ftl_offering(self):
        r1 = self.env['logistics.region'].create({'code': 'FT1', 'name': 'FT 1'})
        r2 = self.env['logistics.region'].create({'code': 'FT2', 'name': 'FT 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'F1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'F2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'FTLVL', 'name': 'FT Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'ltl', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ftl", "dry", 1, 500)
        self.assertFalse(r.available, "FTL with only LTL must return request_quote")

    def test_03_invalid_shipment_type(self):
        r1 = self.env['logistics.region'].create({'code': 'IV1', 'name': 'IV 1'})
        r2 = self.env['logistics.region'].create({'code': 'IV2', 'name': 'IV 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'V1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'V2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "invalid", "dry", 1, 500)
        self.assertFalse(r.available)
        self.assertIn("invalid", r.reason)

    def test_04_reefer_persistence(self):
        r1 = self.env['logistics.region'].create({'code': 'RF1', 'name': 'RF 1'})
        r2 = self.env['logistics.region'].create({'code': 'RF2', 'name': 'RF 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'R1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'R2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'RFLVL', 'name': 'RF Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ltl", "reefer", 1, 500)
        self.assertTrue(r.available, "Reefer must resolve with reefer_supported lane")
        self.assertEqual(r.route_snapshot["temperature_mode"], "reefer")

    def test_05_chilled_maps_to_reefer(self):
        r1 = self.env['logistics.region'].create({'code': 'CH1', 'name': 'CH 1'})
        r2 = self.env['logistics.region'].create({'code': 'CH2', 'name': 'CH 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'H1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'H2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'CHLVL', 'name': 'CH Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ltl", "chilled", 1, 500)
        self.assertTrue(r.available, "Chilled must map to reefer capability")


class TestSnapshotPersistence(TransactionCase):
    """Route snapshot stored in session, copied to booking, never recalculated."""

    def test_01_session_stores_snapshot(self):
        r1 = self.env['logistics.region'].create({'code': 'SN1', 'name': 'SN 1'})
        r2 = self.env['logistics.region'].create({'code': 'SN2', 'name': 'SN 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'N1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'N2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'SNLVL', 'name': 'SN Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        rp = self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        result = svc.calculate(fsa1, fsa2, "ltl", "reefer", 2, 1000)
        self.assertTrue(result.available, result.reason or "not available")
        snap = result.route_snapshot
        self.assertTrue(snap, "route_snapshot must not be empty")
        self.assertEqual(snap["leg_count"], 1)
        self.assertEqual(snap["calculated_price"], result.calculated_price)
        self.assertEqual(snap["pallets"], 2)
        self.assertEqual(snap["weight_lbs"], 1000)
        leg = snap["legs"][0]
        self.assertEqual(leg["rate_plan_id"], rp.id)
        self.assertEqual(leg["origin_region"], 'SN1')
        self.assertEqual(leg["dest_region"], 'SN2')
        self.assertIsNotNone(result.pickup_date)
        self.assertIsNotNone(result.delivery_date_estimate)

    def test_02_capacity_gate_13_pallets(self):
        r1 = self.env['logistics.region'].create({'code': 'CG1', 'name': 'CG 1'})
        r2 = self.env['logistics.region'].create({'code': 'CG2', 'name': 'CG 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'G1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'G2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ltl", "dry", 13, 6500)
        self.assertFalse(r.available)
        self.assertIn("pallets", r.reason)

    def test_03_nearest_5_rounding(self):
        """$228.57 from old formula → nearest $5 = $230.00."""
        from odoo.addons.prema_logistics_booking.services.pricing_service import PricingService
        # Use _compute_v4_formula directly for exact rounding test
        r1 = self.env['logistics.region'].create({'code': 'NR1', 'name': 'NR 1'})
        r2 = self.env['logistics.region'].create({'code': 'NR2', 'name': 'NR 2'})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'NRLVL', 'name': 'NR Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        rp = self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 7,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        v = svc._compute_v4_formula(rp, 1, 700)
        # 1600/7=228.5714, + excess 200*0.1454=29.09, subtotal=257.66, nearest 5 = 260
        self.assertEqual(v["final"], 260.00)
        # Verify nearest-$5: subtotal/5 rounded * 5
        self.assertEqual(v["final"], round(v["subtotal"] / 5.0) * 5.0)

    def test_04_no_per_km_leakage(self):
        r1 = self.env['logistics.region'].create({'code': 'PK1', 'name': 'PK 1'})
        r2 = self.env['logistics.region'].create({'code': 'PK2', 'name': 'PK 2'})
        fsa1 = self.env['logistics.fsa'].create({'fsa': 'K1A', 'region_id': r1.id, 'pickup_supported': True, 'delivery_supported': True})
        fsa2 = self.env['logistics.fsa'].create({'fsa': 'K2B', 'region_id': r2.id, 'pickup_supported': True, 'delivery_supported': True})
        lane = self.env['logistics.lane'].create({
            'origin_region_id': r1.id, 'destination_region_id': r2.id,
            'active': True, 'ltl_capable': True, 'ftl_capable': True, 'reefer_supported': True,
        })
        slevel = self.env['logistics.service.level'].create({'code': 'PKLVL', 'name': 'PK Level', 'reefer_food_eligible': True})
        off = self.env['logistics.service.offering'].create({
            'lane_id': lane.id, 'service_level_id': slevel.id,
            'temperature_mode': 'dry', 'shipment_type': 'both', 'active': True,
        })
        self.env['logistics.lane.schedule'].create({
            'service_offering_id': off.id, 'cutoff_time': 16.0,
            'pickup_monday': True, 'pickup_tuesday': True, 'pickup_wednesday': True,
            'pickup_thursday': True, 'pickup_friday': True, 'delivery_offset_type': 'next_day', 'active': True,
        })
        self.env['logistics.rate.plan'].create({
            'service_offering_id': off.id, 'revenue_target': 1600.0, 'target_load_quantity': 8,
            'active': True, 'effective_from': date.today() - timedelta(days=30),
        })
        svc = PricingService(self.env)
        r = svc.calculate(fsa1, fsa2, "ltl", "dry", 1, 500, liftgate_pickup=True, liftgate_delivery=True, residential=True)
        self.assertTrue(r.available)
        self.assertAlmostEqual(r.calculated_price, 200.00, places=2,
            msg="Accessorials must not add charges")

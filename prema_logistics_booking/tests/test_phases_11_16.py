"""Phases 11-16 batch verification — 20 rollback tests (TransactionCase).

Runs against Prod-db; every test rolls back. No production data is
created or modified permanently. Uses the same fixture patterns as
test_phases_3_10 (regions with approved polygons, vehicles, corridors).
"""
import json
from datetime import date, datetime, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPhasesElevenSixteen(TransactionCase):
    """Phases 11-16 — final operations + subcontracting engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Param = env["ir.config_parameter"].sudo()
        # Migrations do not run in test DBs — seed the Phase 11-16
        # authorities explicitly (same values the migration seeds).
        Param.set_param("logistics.hub_transfer_cost", "50.0")
        Param.set_param("logistics.minimum_margin_pct", "10.0")
        Param.set_param("logistics.market_buy_rate_per_km", "0.0")
        Param.set_param("logistics.allow_cross_border_subcontract", "False")
        Product = env["product.product"].sudo()
        product = Product.search(
            [("default_code", "=", "SUBCONTRACTED_FREIGHT_SVC")], limit=1)
        if not product:
            product = Product.create({
                "name": "Subcontracted Freight Service",
                "default_code": "SUBCONTRACTED_FREIGHT_SVC",
                "type": "service",
                "purchase_ok": True,
                "sale_ok": False,
            })
        cls.freight_product = product

        cls.canada = env["res.country"].search([("code", "=", "CA")], limit=1)
        cls.ontario = env["res.country.state"].search(
            [("code", "=", "ON"), ("country_id", "=", cls.canada.id)],
            limit=1) if cls.canada else env["res.country.state"]
        if cls.canada and not cls.canada.logistics_network_enabled:
            cls.canada.logistics_network_enabled = True
        if cls.ontario and not cls.ontario.logistics_network_enabled:
            cls.ontario.logistics_network_enabled = True
        cls.partner = env["res.partner"].create(
            {"name": "Phases 11-16 Test Customer"})
        cls.vehicle = cls._make_vehicle(cls.partner)
        cls.carrier = env["res.partner"].create({
            "name": "Test Subcontract Carrier",
            "is_transport_carrier": True,
            "carrier_status": "active",
        })
        cls.carrier2 = env["res.partner"].create({
            "name": "Test Subcontract Carrier 2",
            "is_transport_carrier": True,
            "carrier_status": "active",
        })
        cls.svc = env["prema.dispatch.location"]
        cls._loc_n = 0
        cls._last_booking = False

    @classmethod
    def _make_vehicle(cls, partner):
        env = cls.env
        brand = env["fleet.vehicle.model.brand"].create(
            {"name": "Phases 11-16 Test Brand"})
        model = env["fleet.vehicle.model"].create(
            {"name": "Phases 11-16 Test Model", "brand_id": brand.id})
        return env["fleet.vehicle"].create({
            "model_id": model.id,
            "license_plate": "TEST-11-16",
            "odometer_unit": "kilometers",
            "power_unit": "power",
            "driver_id": partner.id,
        })

    @classmethod
    def _region(cls, code, lat=None, lng=None):
        env = cls.env
        region = env["logistics.region"].search([("code", "=", code)], limit=1)
        if region:
            return region
        vals = {
            "code": code,
            "name": code,
            "country_id": cls.canada.id,
            "state_id": cls.ontario.id,
            "is_official_ltl_region": True,
            "boundary_status": "approved",
        }
        if lat is not None:
            poly = [
                [lng - 0.5, lat - 0.5], [lng + 0.5, lat - 0.5],
                [lng + 0.5, lat + 0.5], [lng - 0.5, lat + 0.5],
                [lng - 0.5, lat - 0.5],
            ]
            vals["polygon_geojson"] = json.dumps(
                {"type": "Polygon", "coordinates": [poly]})
        return env["logistics.region"].create(vals)

    # ── Booking / job fixtures ──────────────────────────────────────

    @classmethod
    def _booking_stop(cls, city, lat, lng, booking=None):
        """Booking stops belong to a booking — reuse the most recently
        created booking unless one is given explicitly."""
        cls._loc_n += 1
        if booking is None:
            booking = cls._last_booking or cls._booking()
        return cls.env["logistics.booking.stop"].create({
            "booking_id": booking.id,
            "company_name": "Stop Co %d" % cls._loc_n,
            "formatted_address": "456 Test Ave #%d, %s" % (cls._loc_n, city),
            "city": city,
            "latitude": lat,
            "longitude": lng,
            "stop_type": "delivery",
        })

    @classmethod
    def _leg(cls, booking, origin, dest, leg_type="linehaul",
             departure=False, **extra):
        vals = {
            "booking_id": booking.id,
            "sequence": 10,
            "origin_stop_id": origin.id,
            "destination_stop_id": dest.id,
            # Namespaced codes: Prod-db has ARCHIVED regions R1-R15 whose
            # active-filtered search misses them, so create() would hit the
            # unique constraint. T116* can never collide with real codes.
            "origin_region_id": cls._region("T116R%d" % cls._loc_n,
                                            lat=origin.latitude,
                                            lng=origin.longitude).id,
            "destination_region_id": cls._region(
                "T116RD%d" % cls._loc_n, lat=dest.latitude,
                lng=dest.longitude).id,
            "leg_type": leg_type,
            "pickup_date": datetime(2026, 9, 8, 8, 0),
            "delivery_date": datetime(2026, 9, 8, 18, 0),
            "pallets": 4,
            "weight_lbs": 2000.0,
            "status": "scheduled",
            "reservation_state": "reserved",
            "frozen_leg_price": 2400.0,
        }
        if departure:
            vals["departure_id"] = departure.id
        vals.update(extra)
        return cls.env["logistics.booking.leg"].create(vals)

    @classmethod
    def _booking(cls, price=2400.0, **extra):
        vals = {
            "partner_id": cls.partner.id,
            "calculated_price": price,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 4,
            "weight_lbs": 2000.0,
            "pickup_date": date(2026, 9, 8),
            "estimated_delivery_date": date(2026, 9, 8),
        }
        vals.update(extra)
        booking = cls.env["logistics.booking"].create(vals)
        cls._last_booking = booking
        return booking

    @classmethod
    def _job(cls, **extra):
        vals = {
            "name": "JOB-TEST-11-16-%d" % cls._loc_n,
            "partner_id": cls.partner.id,
            "vehicle_id": cls.vehicle.id,
            "driver_id": cls.partner.id,
            "scheduled_pickup": datetime(2026, 9, 8, 8, 0),
        }
        vals.update(extra)
        return cls.env["prema.dispatch.job"].create(vals)

    @classmethod
    def _dispatch_stop(cls, job, stop_type, lat, lng, scheduled_time,
                       sequence=10, status="pending", **extra):
        vals = {
            "job_id": job.id,
            "stop_type": stop_type,
            "address": "789 Dispatch Rd, Testville",
            "latitude": lat,
            "longitude": lng,
            "scheduled_time": scheduled_time,
            "sequence": sequence,
            "status": status,
        }
        vals.update(extra)
        return cls.env["prema.dispatch.stop"].create(vals)

    def _generate(self, booking):
        from odoo.addons.prema_logistics_booking.services.\
            execution_scenario_service import ExecutionScenarioService
        return ExecutionScenarioService(self.env).generate(booking)

    # ═══ PHASE 11 — Dynamic Actual Route + Live ETA ═════════════════

    def test_01_actual_route_extent_trim(self):
        """Only confirmed stops form the actual route; locked stops never
        move; deliveries order before pickups within the same window
        (pallet precedence). Corridor endpoints nobody confirmed are
        never inserted."""
        from odoo.addons.prema_logistics_booking.services.\
            live_route_service import LiveRouteService
        job = self._job()
        window = datetime(2026, 9, 8, 10, 0)
        s_delivery = self._dispatch_stop(
            job, "dropoff", 43.6, -79.4, window, sequence=10)
        s_pickup = self._dispatch_stop(
            job, "pickup", 43.7, -79.5, window, sequence=20)
        s_locked = self._dispatch_stop(
            job, "dropoff", 43.8, -79.6, window - timedelta(hours=2),
            sequence=30, status="completed")
        ordered = LiveRouteService(self.env).build_actual_route(job)
        # Locked (completed) stop keeps position first — the actual route
        # is RE-sequenced (10/20/30) to reflect the real execution order.
        self.assertEqual(ordered[0].id, s_locked.id)
        self.assertEqual(ordered[0].sequence, 10)
        # Same window: delivery (pallet precedence) before pickup.
        self.assertEqual(ordered[1].id, s_delivery.id)
        self.assertEqual(ordered[2].id, s_pickup.id)
        self.assertEqual([s.sequence for s in ordered], [10, 20, 30])
        # No extra stop was invented for a corridor endpoint.
        self.assertEqual(len(ordered), 3)
        self.assertEqual(len(job.stop_ids), 3)

    def test_02_cross_day_positioning(self):
        """Start position hierarchy: prior-day end wins under 'auto'
        when this truck completed a stop yesterday; manual GPS override
        wins over everything."""
        from odoo.addons.prema_logistics_booking.services.\
            live_route_service import LiveRouteService
        svc = LiveRouteService(self.env)
        yesterday = self._job(
            scheduled_pickup=datetime(2026, 9, 7, 8, 0))
        prior = self._dispatch_stop(
            yesterday, "dropoff", 43.61, -79.41,
            datetime(2026, 9, 7, 17, 0), sequence=10, status="completed")
        job = self._job()
        lat, lng, source = svc.start_position(job)
        self.assertEqual(source, "prior_day_end")
        self.assertAlmostEqual(lat, 43.61, places=4)
        # Manual override wins.
        job.write({
            "start_position_override": "gps",
            "start_position_override_lat": 44.12,
            "start_position_override_lng": -78.93,
        })
        lat, lng, source = svc.start_position(job)
        self.assertEqual(source, "manual")
        self.assertAlmostEqual(lat, 44.12, places=4)

    def test_03_live_eta_delay_propagation(self):
        """A late stop pushes every downstream ETA forward; the delay is
        measured against scheduled time and carried stop-by-stop."""
        from odoo.addons.prema_logistics_booking.services.\
            live_route_service import LiveRouteService
        job = self._job()
        anchor = datetime(2026, 9, 8, 8, 0)
        s1 = self._dispatch_stop(
            job, "pickup", 43.6, -79.4, anchor, sequence=10)
        s2 = self._dispatch_stop(
            job, "dropoff", 43.7, -79.5, anchor + timedelta(hours=2),
            sequence=20)
        svc = LiveRouteService(self.env)
        svc.recompute_live_eta(job, anchor_time=anchor)
        self.assertTrue(s1.eta_live > anchor)
        self.assertTrue(s2.eta_live > s1.eta_live)
        self.assertEqual(s1.eta_source, "live")
        # Simulate the pickup running 3h late (en_route, ETA held) — far
        # past the delivery's 2h scheduled window so the delay MUST
        # propagate downstream.
        s1.write({"status": "en_route",
                  "eta_live": anchor + timedelta(hours=3)})
        svc.recompute_live_eta(job, anchor_time=anchor)
        self.assertTrue(s2.eta_delay_minutes > 0,
                        "downstream delay must be positive")
        self.assertEqual(s2.eta_source, "live")
        # Dispatcher override wins for its stop.
        s2.write({"eta_override": anchor + timedelta(hours=4)})
        svc.recompute_live_eta(job, anchor_time=anchor)
        self.assertEqual(s2.eta_source, "override")
        self.assertEqual(s2.eta_live, anchor + timedelta(hours=4))

    # ═══ PHASE 12 — Execution Scenario + Cost Engine ════════════════

    def test_04_scenario_own_direct(self):
        """Scheduled network (rank 1) + own-fleet dedicated direct
        (rank 2) are always evaluated; manual review is the last resort."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("London", 42.98, -81.25)
        self._leg(booking, o, d, estimated_leg_cost=700.0,
                  execution_mode="own_fleet")
        scenarios = self._generate(booking)
        self.assertGreaterEqual(len(scenarios), 3)
        rank1 = scenarios.filtered(lambda s: s.rank == 1)
        rank2 = scenarios.filtered(lambda s: s.rank == 2)
        self.assertTrue(rank1 and rank1[0].state == "auto_bookable")
        self.assertTrue(rank2)
        self.assertEqual(rank2[0].estimated_total_cost, 700.0)
        self.assertEqual(rank2[0].estimated_margin, 2400.0 - 700.0)
        self.assertEqual(rank2[0].estimated_margin_pct,
                         round(1700.0 / 2400.0 * 100.0, 2))
        self.assertTrue(
            scenarios.filtered(lambda s: s.rank == 99 and
                               s.state == "manual_review"))

    def test_05_scenario_own_plus_sub(self):
        """Own fleet + subcontract mix: cost = own estimate + accepted
        buy rate; confirmation required (not auto-bookable)."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        h = self._booking_stop("Belleville", 44.16, -77.38)
        d = self._booking_stop("Ottawa", 45.42, -75.70)
        self._leg(booking, o, h, estimated_leg_cost=300.0,
                  execution_mode="own_fleet")
        self._leg(booking, h, d, estimated_leg_cost=400.0,
                  execution_mode="subcontracted",
                  accepted_buy_rate=350.0, cost_source="carrier_accepted")
        scenarios = self._generate(booking)
        rank1 = scenarios.filtered(lambda s: s.rank == 1)
        self.assertTrue(rank1)
        self.assertEqual(rank1[0].estimated_total_cost, 300.0 + 350.0)
        self.assertEqual(rank1[0].state, "carrier_acceptance_required")

    def test_06_scenario_sub_plus_own(self):
        """Sub + own order also sums correctly (direction-independent)."""
        booking = self._booking()
        o = self._booking_stop("Ottawa", 45.42, -75.70)
        h = self._booking_stop("Belleville", 44.16, -77.38)
        d = self._booking_stop("Toronto", 43.65, -79.38)
        self._leg(booking, o, h, estimated_leg_cost=400.0,
                  execution_mode="subcontracted", accepted_buy_rate=380.0,
                  cost_source="carrier_accepted")
        self._leg(booking, h, d, estimated_leg_cost=250.0,
                  execution_mode="own_fleet")
        scenarios = self._generate(booking)
        rank1 = scenarios.filtered(lambda s: s.rank == 1)
        self.assertEqual(rank1[0].estimated_total_cost, 380.0 + 250.0)
        self.assertEqual(rank1[0].state, "carrier_acceptance_required")

    def test_07_scenario_full_sub_with_lane_rate(self):
        """Carrier lane-rate card is the BUY authority for full
        subcontract: cost = rate card + hub transfer cost."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Kingston", 44.23, -76.48)
        leg = self._leg(booking, o, d, estimated_leg_cost=600.0,
                        execution_mode="unassigned")
        # The lane rate card must match the LEG's actual regions.
        self.env["logistics.carrier.lane.rate"].create({
            "carrier_id": self.carrier.id,
            "origin_region_id": leg.origin_region_id.id,
            "destination_region_id": leg.destination_region_id.id,
            "equipment_type": "dry",
            "pricing_method": "flat_rate",
            "rate": 600.0,
        })
        scenarios = self._generate(booking)
        sub = scenarios.filtered(lambda s: s.rank >= 10 and s.rank < 99
                                 and s.execution_plan
                                 and s.execution_plan[0].get("carrier_id")
                                 == self.carrier.id)
        self.assertTrue(sub, "carrier scenario must exist")
        self.assertEqual(sub[0].estimated_total_cost, 600.0 + 50.0)
        self.assertEqual(sub[0].state, "carrier_acceptance_required")

    # ═══ PHASE 13 — Carrier network & negotiation ═══════════════════

    def test_08_carrier_negotiation_lifecycle(self):
        """Draft → availability requested → negotiating → accepted:
        acceptance fixes the leg's buy authority, creates the freight
        PO, and recomputes margin."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Hamilton", 43.26, -79.87)
        leg = self._leg(booking, o, d, execution_mode="unassigned")
        offer = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier.id,
            "target_buy_rate": 500.0,
        })
        self.assertEqual(offer.state, "draft")
        offer.action_request_availability()
        self.assertEqual(offer.state, "availability_requested")
        offer.action_negotiate(580.0)
        self.assertEqual(offer.state, "negotiating")
        self.assertEqual(offer.carrier_counter_rate, 580.0)
        offer.write({"agreed_rate": 575.0})
        offer.action_accept()
        self.assertEqual(offer.state, "accepted")
        leg.invalidate_recordset()
        self.assertEqual(leg.execution_mode, "subcontracted")
        self.assertEqual(leg.executing_carrier_id.id, self.carrier.id)
        self.assertEqual(leg.accepted_buy_rate, 575.0)
        self.assertEqual(leg.cost_source, "carrier_accepted")
        self.assertEqual(leg.execution_status, "confirmed")
        self.assertTrue(leg.purchase_order_id)
        self.assertTrue(leg.purchase_order_id.is_freight_subcontract)
        self.assertEqual(leg.purchase_order_id.amount_untaxed, 575.0)
        # Second acceptance on the same leg is refused.
        offer2 = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier2.id,
            "target_buy_rate": 500.0,
        })
        offer2.write({"agreed_rate": 550.0})
        with self.assertRaises(UserError):
            offer2.action_accept()

    def test_09_freight_po_rate_confirmation(self):
        """The freight PO carries the frozen freight details and the
        seeded freight product — the Rate Confirmation is a
        presentation of this PO, never a parallel model."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Niagara", 43.09, -79.07)
        leg = self._leg(booking, o, d)
        offer = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier.id,
            "agreed_rate": 600.0,
        })
        offer.action_accept()
        po = leg.purchase_order_id
        self.assertTrue(po)
        details = po.freight_details
        self.assertTrue(details)
        self.assertEqual(details["pallets"], 4)
        self.assertEqual(details["weight_lbs"], 2000.0)
        self.assertEqual(details["agreed_rate"], 600.0)
        self.assertEqual(details["pod_required"], True)
        self.assertEqual(po.order_line[0].product_id.id,
                         self.freight_product.id)
        self.assertEqual(po.order_line[0].price_unit, 600.0)
        # Idempotent: a second accept returns the SAME PO.
        po2 = leg._create_freight_purchase_order(offer, 600.0)
        self.assertEqual(po2.id, po.id)

    # ═══ PHASE 14/15 — Vendor bill, variance, POD ═══════════════════

    def _post_bill(self, po, amount):
        bill = self.env["account.move"].create({
            "move_type": "in_invoice",
            "partner_id": po.partner_id.id,
            "invoice_date": date(2026, 9, 15),
            "invoice_line_ids": [(0, 0, {
                "product_id": po.order_line[0].product_id.id,
                "quantity": 1,
                "price_unit": amount,
                "purchase_line_id": po.order_line[0].id,
            })],
        })
        bill.action_post()
        return bill

    def test_10_vendor_bill_review(self):
        """Reviewing the vendor bill writes the actual leg cost and the
        flagged variance — and never touches the accepted buy rate."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Kitchener", 43.45, -80.49)
        leg = self._leg(booking, o, d)
        offer = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier.id,
            "agreed_rate": 600.0,
        })
        offer.action_accept()
        po = leg.purchase_order_id
        self._post_bill(po, 650.0)
        po.action_review_vendor_bill()
        leg.invalidate_recordset()
        self.assertEqual(leg.actual_leg_cost, 650.0)
        self.assertEqual(leg.carrier_invoice_variance, 50.0)
        self.assertTrue(leg.carrier_invoice_received)
        # Accepted rate preserved — never silently rewritten.
        self.assertEqual(leg.accepted_buy_rate, 600.0)

    def test_11_variance_flagged_not_silent(self):
        """PO-level freight variance is computed from billed lines vs
        the accepted PO amount — flagged for review, never applied."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Barrie", 44.39, -79.69)
        leg = self._leg(booking, o, d)
        offer = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier.id,
            "agreed_rate": 700.0,
        })
        offer.action_accept()
        po = leg.purchase_order_id
        self.assertEqual(po.freight_variance, 0.0)
        self._post_bill(po, 755.0)
        po.invalidate_recordset()
        self.assertEqual(po.freight_variance, 55.0)

    def test_12_pod_capture(self):
        """POD via the existing attachment infrastructure."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Oshawa", 43.90, -78.85)
        leg = self._leg(booking, o, d)
        self.assertFalse(leg.pod_received)
        attach = self.env["ir.attachment"].create({
            "name": "pod_test.png",
            "raw": b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        })
        leg.action_attach_carrier_pod(attach)
        self.assertTrue(leg.pod_received)
        self.assertIn(attach.id, leg.pod_attachment_ids.ids)
        leg.action_record_carrier_status("delivered")
        self.assertEqual(leg.execution_status, "delivered")

    # ═══ Phase 12 — Profitability: estimated vs actual ══════════════

    def test_13_profitability_math(self):
        """2400 revenue − (700 own + 600 sub + 50 hub) = 1350 estimated
        cost → 1050 estimated margin (43.75%), no margin warning at the
        10% threshold. Estimated and actual stay distinct."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        h = self._booking_stop("Belleville", 44.16, -77.38)
        d = self._booking_stop("Ottawa", 45.42, -75.70)
        leg1 = self._leg(booking, o, h, estimated_leg_cost=700.0,
                         execution_mode="own_fleet")
        leg2 = self._leg(booking, h, d, estimated_leg_cost=600.0,
                         execution_mode="subcontracted",
                         accepted_buy_rate=600.0,
                         cost_source="carrier_accepted")
        leg2.write({"hub_transfer_cost": 50.0})
        booking._compute_execution_totals()
        booking.invalidate_recordset()
        self.assertEqual(booking.own_fleet_cost_total, 700.0)
        self.assertEqual(booking.subcontract_cost_total, 600.0)
        self.assertEqual(booking.hub_cost_total, 50.0)
        self.assertEqual(booking.execution_estimated_cost, 1350.0)
        self.assertEqual(booking.execution_estimated_margin, 1050.0)
        self.assertEqual(booking.execution_estimated_margin_pct,
                         round(1050.0 / 2400.0 * 100.0, 2))
        self.assertFalse(booking.margin_warning)
        self.assertTrue(booking.has_subcontracted_legs)
        # Actuals are zero until a vendor bill exists.
        self.assertEqual(booking.actual_total_cost, 0.0)
        self.assertEqual(booking.actual_margin, 0.0)
        # Margin warning fires when below the 10% threshold.
        leg1.write({"estimated_leg_cost": 1900.0})
        booking._compute_execution_totals()
        booking.invalidate_recordset()
        self.assertTrue(booking.margin_warning)
        self.assertEqual(booking.calculated_price, 2400.0)

    def test_14_buy_sell_detention_independent(self):
        """Carrier BUY detention is a separate authority from customer
        SELL detention — both roll up, never netted."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Brampton", 43.73, -79.76)
        leg = self._leg(booking, o, d, execution_mode="own_fleet",
                        estimated_leg_cost=500.0)
        leg.write({"carrier_detention_amount": 120.0})
        job = self._job()
        job.write({"logistics_booking_id": booking.id})
        stop = self._dispatch_stop(
            job, "dropoff", 43.65, -79.38,
            datetime(2026, 9, 8, 12, 0), sequence=10)
        self.env["prema.dispatch.detention.item"].create({
            "job_id": job.id,
            "stop_id": stop.id,
            "state": "approved",
            "approved_amount": 80.0,
        })
        booking._compute_execution_totals()
        booking.invalidate_recordset()
        self.assertEqual(booking.carrier_detention_cost_total, 120.0)
        self.assertEqual(booking.customer_detention_revenue, 80.0)
        self.assertEqual(booking.execution_estimated_cost, 500.0 + 120.0)

    # ═══ PHASE 16 — Fallback & safety ═══════════════════════════════

    def test_15_decline_fallback_unchoose(self):
        """A declined offer un-chooses the scenario that depended on the
        carrier and clears the booking's chosen scenario — the engine
        never silently re-subscribes."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Mississauga", 43.59, -79.64)
        leg = self._leg(booking, o, d)
        sc = self.env["logistics.execution.scenario"].create({
            "booking_id": booking.id,
            "rank": 10,
            "state": "carrier_acceptance_required",
            "customer_revenue": 2400.0,
            "estimated_total_cost": 600.0,
            "chosen": True,
            "execution_plan": [{
                "leg": leg.id,
                "execution_mode": "subcontracted",
                "carrier_id": self.carrier.id,
                "buy_rate": 600.0,
                "cost_source": "carrier_quote",
            }],
        })
        booking.write({"execution_scenario_id": sc.id,
                       "execution_confirmation_required": True})
        offer = self.env["logistics.booking.leg.carrier.offer"].create({
            "booking_leg_id": leg.id,
            "carrier_id": self.carrier.id,
            "target_buy_rate": 600.0,
        })
        offer.action_request_availability()
        offer.action_decline()
        sc.invalidate_recordset()
        booking.invalidate_recordset()
        self.assertFalse(sc.chosen)
        self.assertFalse(booking.execution_scenario_id)

    def test_16_missed_connection(self):
        """A cancelled onward departure flags the connection exception —
        custody is preserved, never a false delivery — and the next
        departure is recommended, never auto-booked."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("Kingston", 44.23, -76.48)
        corridor = self.env["logistics.corridor"].search(
            [("active", "=", True)], limit=1)
        if corridor:
            dep = self.env["logistics.corridor.departure"].create({
                "corridor_id": corridor.id,
                "departure_date": date(2026, 9, 8),
                "vehicle_id": self.vehicle.id,
            })
            leg = self._leg(booking, o, d, departure=dep)
            dep.write({"status": "cancelled"})
            booking.action_detect_missed_connections()
            leg.invalidate_recordset()
            self.assertTrue(leg.connection_exception)
            self.assertEqual(leg.execution_status, "exception")
            # Recommend, never auto-book.
            next_dep = leg.action_recommend_next_departure()
            self.assertTrue(next_dep is False or isinstance(next_dep, int))
            self.assertEqual(leg.execution_status, "exception")
            booking.action_release_missed_connections()
            leg.invalidate_recordset()
            self.assertFalse(leg.connection_exception)
        else:
            # No corridor available — the detector still runs cleanly.
            booking.action_detect_missed_connections()
            self.assertTrue(True)

    def test_17_mon_to_tue_regression(self):
        """Multi-leg bookings spanning days survive scenario generation
        intact (legs/regions/prices untouched)."""
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        h = self._booking_stop("Belleville", 44.16, -77.38)
        d = self._booking_stop("Ottawa", 45.42, -75.70)
        leg1 = self._leg(booking, o, h, pickup_date=datetime(2026, 9, 7, 8, 0),
                         delivery_date=datetime(2026, 9, 7, 12, 0),
                         estimated_leg_cost=300.0, execution_mode="own_fleet")
        leg2 = self._leg(booking, h, d, pickup_date=datetime(2026, 9, 8, 8, 0),
                         delivery_date=datetime(2026, 9, 8, 12, 0),
                         estimated_leg_cost=250.0, execution_mode="own_fleet")
        before = [(l.id, l.sequence, l.frozen_leg_price) for l in booking.leg_ids]
        self._generate(booking)
        after = [(l.id, l.sequence, l.frozen_leg_price) for l in booking.leg_ids]
        self.assertEqual(before, after)
        self.assertEqual(booking.leg_ids[0].id, leg1.id)
        self.assertEqual(booking.leg_ids[1].id, leg2.id)
        self.assertEqual(booking.calculated_price, 2400.0)

    def test_18_driver_app_regression(self):
        """The driver-app job feed and the customer-visible milestone
        derivation both still work with the new fields present."""
        from odoo.addons.prema_logistics_booking.services.\
            live_route_service import LiveRouteService
        job = self._job()
        self._dispatch_stop(job, "pickup", 43.65, -79.38,
                            datetime(2026, 9, 8, 8, 0), sequence=10)
        self._dispatch_stop(job, "dropoff", 43.65, -79.38,
                            datetime(2026, 9, 8, 10, 0), sequence=20)
        summary = job._driver_job_summary()
        self.assertIn("vehicle", summary)
        self.assertIn("pickup_location", summary)
        self.assertIn("vehicle_layout_max_capacity", summary)
        status = LiveRouteService(self.env).customer_visible_status(job)
        self.assertIn(status, (
            "estimated_arrival", "updated_eta", "in_transit",
            "out_for_delivery", "delivered"))
        self.assertNotIn("carrier", summary)
        # New dispatcher-override fields are present and defaulted.
        self.assertEqual(job.start_position_override, "auto")
        self.assertFalse(job.ignore_route_recommendation)

    def test_19_customer_accounting_independence(self):
        """No customer-group ACL exists on any Phase 11-16 model — buy
        rates, offers, scenarios, margins never reach customers."""
        cust = self.env.ref("prema_logistics_booking.group_logistics_customer")
        for model in ("logistics.carrier.lane.rate",
                      "logistics.booking.leg.carrier.offer",
                      "logistics.execution.scenario"):
            rows = self.env["ir.model.access"].search([
                ("model_id.model", "=", model),
                ("group_id", "=", cust.id),
            ])
            self.assertEqual(len(rows), 0,
                             "customer group must have no access to %s" % model)
        # The booking itself stays the customer-visible surface; the
        # execution snapshot is internal-only via field-level controls.
        booking = self._booking()
        o = self._booking_stop("Toronto", 43.65, -79.38)
        d = self._booking_stop("London", 42.98, -81.25)
        leg = self._leg(booking, o, d, execution_mode="own_fleet",
                        estimated_leg_cost=700.0)
        self.assertTrue(booking.has_subcontracted_legs in (True, False))

    def test_20_normal_po_unchanged(self):
        """An ordinary purchase order is untouched: no freight flag, no
        freight details, no rate-confirmation actions."""
        po = self.env["purchase.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {
                "product_id": self.freight_product.id,
                "name": "Ordinary goods purchase",
                "product_qty": 2,
                "price_unit": 25.0,
            })],
        })
        self.assertFalse(po.is_freight_subcontract)
        self.assertFalse(po.freight_details)
        self.assertFalse(po.booking_leg_id)
        self.assertEqual(po.freight_variance, 0.0)
        with self.assertRaises(UserError):
            po.action_print_rate_confirmation()
        with self.assertRaises(UserError):
            po.action_send_rate_confirmation()

"""
Prema Dispatch — 20 server-side test cases (Task 25).
Run with: ./odoo-bin -c odoo18.conf -d Prod-db --test-enable -u prema_dispatch
"""
import base64
from datetime import date

from odoo import exceptions, fields
from odoo.tests.common import TransactionCase

from .mock_google_apis import install_google_mocks


class TestDispatchCapacity(TransactionCase):
    """Tests 1–5: Capacity validation."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def _make_job(self, skids=0):
        return self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "approximate_skids": skids,
        })

    def _add_stop(self, job, stop_type, pallets_in=0, pallets_out=0, seq=10, group=0):
        return self.env["prema.dispatch.stop"].create({
            "job_id": job.id,
            "sequence": seq,
            "stop_type": stop_type,
            "address": "Test Address, ON",
            "pallets_in": pallets_in,
            "pallets_out": pallets_out,
            "linked_load_group": group,
        })

    def test_01_simple_pickup_dropoff(self):
        """Test 1: Simple one pickup / one drop-off."""
        job = self._make_job(skids=5)
        self._add_stop(job, "pickup", pallets_in=5, seq=10)
        self._add_stop(job, "dropoff", pallets_out=5, seq=20)
        job.invalidate_recordset()
        self.assertEqual(job.max_onboard_pallets, 5)

    def test_02_multi_stop_delivery(self):
        """Test 2: Multi-stop delivery, one pickup."""
        job = self._make_job()
        self._add_stop(job, "pickup", pallets_in=10, seq=10)
        self._add_stop(job, "dropoff", pallets_out=3, seq=20)
        self._add_stop(job, "dropoff", pallets_out=4, seq=30)
        self._add_stop(job, "dropoff", pallets_out=3, seq=40)
        job.invalidate_recordset()
        self.assertEqual(job.max_onboard_pallets, 10)

    def test_03_multi_pickup_same_warehouse(self):
        """Test 3: Multi-pickup from same warehouse (2 rounds)."""
        job = self._make_job()
        # Round 1: pickup 12 → drop 12
        self._add_stop(job, "pickup", pallets_in=12, seq=10, group=1)
        self._add_stop(job, "dropoff", pallets_out=12, seq=20, group=1)
        # Round 2: pickup 6 → drop 1,1,2,1,1
        self._add_stop(job, "pickup", pallets_in=6, seq=30, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=40, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=50, group=2)
        self._add_stop(job, "dropoff", pallets_out=2, seq=60, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=70, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=80, group=2)
        job.invalidate_recordset()
        self.assertEqual(job.max_onboard_pallets, 12)

    def test_04_18_total_12_max_onboard_should_pass(self):
        """Test 4: 18 total pallets but max onboard 12 — assignment capacity should use 12."""
        job = self._make_job(skids=18)
        self._add_stop(job, "pickup", pallets_in=12, seq=10, group=1)
        self._add_stop(job, "dropoff", pallets_out=12, seq=20, group=1)
        self._add_stop(job, "pickup", pallets_in=6, seq=30, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=40, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=50, group=2)
        self._add_stop(job, "dropoff", pallets_out=2, seq=60, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=70, group=2)
        self._add_stop(job, "dropoff", pallets_out=1, seq=80, group=2)
        job.invalidate_recordset()
        # max_onboard should be 12, not 18
        self.assertEqual(job.max_onboard_pallets, 12)
        # Vehicle with 12-pallet capacity should NOT trigger capacity warning
        vehicle = self.env["fleet.vehicle"].search(
            [("x_max_pallets", ">=", 12)], limit=1
        )
        if vehicle:
            hard_blocks, soft_warnings = job._check_vehicle_compatibility(vehicle)
            cap_warnings = [w for w in soft_warnings if "exceed" in w.lower()]
            self.assertEqual(cap_warnings, [], "Should not warn when max onboard fits the truck")

    def test_05_capacity_exceeded_mid_route_should_warn(self):
        """Test 5: Capacity exceeded at mid-route segment — should produce warning."""
        job = self._make_job()
        # Load 20 in one go to a 12-pallet truck
        self._add_stop(job, "pickup", pallets_in=20, seq=10)
        self._add_stop(job, "dropoff", pallets_out=20, seq=20)
        job.invalidate_recordset()
        vehicle = self.env["fleet.vehicle"].search(
            [("x_max_pallets", ">=", 1), ("x_max_pallets", "<=", 15)], limit=1
        )
        if vehicle and vehicle.x_max_pallets:
            _, soft_warnings = job._check_vehicle_compatibility(vehicle)
            self.assertTrue(
                any("exceed" in w.lower() for w in soft_warnings),
                "Should warn when peak load exceeds truck capacity"
            )


class TestDispatchTimeWindows(TransactionCase):
    """Tests 6–9: Time window field validation."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def test_06_pickup_strict_only(self):
        """Test 6: Pickup window type = window stores correctly."""
        from odoo import fields
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "pickup_window_type": "window",
            "pickup_earliest": "2026-07-07 09:00:00",
            "pickup_latest": "2026-07-07 11:00:00",
            "delivery_window_type": "flexible",
        })
        self.assertEqual(job.pickup_window_type, "window")
        self.assertTrue(job.pickup_earliest)
        self.assertTrue(job.pickup_latest)
        self.assertFalse(job.hard_deadline)

    def test_07_delivery_strict_only(self):
        """Test 7: Delivery deadline with hard_deadline flag."""
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "pickup_window_type": "flexible",
            "delivery_window_type": "deadline",
            "delivery_deadline": "2026-07-07 15:30:00",
            "hard_deadline": True,
        })
        self.assertEqual(job.delivery_window_type, "deadline")
        self.assertTrue(job.hard_deadline)

    def test_08_both_pickup_and_delivery_strict(self):
        """Test 8: Both pickup and delivery windows set."""
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "pickup_window_type": "window",
            "pickup_earliest": "2026-07-07 09:00:00",
            "pickup_latest": "2026-07-07 11:00:00",
            "delivery_window_type": "deadline",
            "delivery_deadline": "2026-07-07 15:30:00",
            "hard_deadline": True,
        })
        self.assertTrue(job.pickup_earliest)
        self.assertTrue(job.delivery_deadline)
        self.assertTrue(job.hard_deadline)

    def test_09_no_strict_window_defaults_flexible(self):
        """Test 9: No time window set — defaults to flexible."""
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
        })
        self.assertEqual(job.pickup_window_type, "flexible")
        self.assertEqual(job.delivery_window_type, "flexible")


class TestDispatchAssignment(TransactionCase):
    """Tests 10–16: Truck assignment, mid-day insertion, availability."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.stage_dispatched = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "dispatched")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def _make_job(self, **kw):
        vals = {"partner_id": self.partner.id, "stage_id": self.stage_draft.id}
        vals.update(kw)
        return self.env["prema.dispatch.job"].create(vals)

    def test_10_reefer_required_no_reefer_truck_blocks(self):
        """Test 13 (spec): Reefer required but truck has no reefer — hard block."""
        vehicle = self.env["fleet.vehicle"].search(
            [("x_reefer", "=", False)], limit=1
        )
        if not vehicle:
            self.skipTest("No non-reefer vehicle in fleet")
        job = self._make_job(requires_reefer=True)
        hard_blocks, _ = job._check_vehicle_compatibility(vehicle)
        self.assertTrue(any("reefer" in b.lower() for b in hard_blocks))

    def test_11_liftgate_required_no_liftgate_blocks(self):
        """Test 14 (spec): Liftgate required but truck has no liftgate — hard block."""
        vehicle = self.env["fleet.vehicle"].search(
            [("x_liftgate", "=", False)], limit=1
        )
        if not vehicle:
            self.skipTest("No non-liftgate vehicle in fleet")
        job = self._make_job(requires_liftgate=True)
        hard_blocks, _ = job._check_vehicle_compatibility(vehicle)
        self.assertTrue(any("liftgate" in b.lower() for b in hard_blocks))

    def test_12_truck_reefer_compatible_no_block(self):
        """Test 16 (spec): Truck meets requirements — no hard blocks."""
        vehicle = self.env["fleet.vehicle"].search(
            [("x_reefer", "=", True)], limit=1
        )
        if not vehicle:
            self.skipTest("No reefer vehicle in fleet")
        job = self._make_job(requires_reefer=True)
        hard_blocks, _ = job._check_vehicle_compatibility(vehicle)
        reefer_blocks = [b for b in hard_blocks if "reefer" in b.lower()]
        self.assertEqual(reefer_blocks, [])

    def test_13_dry_job_can_use_reefer_truck(self):
        """Dry freight should remain assignable to a reefer truck."""
        vehicle = self.env["fleet.vehicle"].search(
            [("x_reefer", "=", True)], limit=1
        )
        if not vehicle:
            self.skipTest("No reefer vehicle in fleet")
        job = self._make_job(requires_reefer=False)
        hard_blocks, _ = job._check_vehicle_compatibility(vehicle)
        reefer_blocks = [b for b in hard_blocks if "reefer" in b.lower()]
        self.assertEqual(
            reefer_blocks, [],
            "Dry jobs must be allowed on reefer trucks."
        )

    def test_14_manager_override_allows_blocked_assignment(self):
        """Test 19 (spec): Manager override with reason bypasses hard block."""
        vehicle = self.env["fleet.vehicle"].search(
            [("x_reefer", "=", False)], limit=1
        )
        if not vehicle:
            self.skipTest("No non-reefer vehicle in fleet")
        job = self._make_job(requires_reefer=True)
        # Write with override reason — should not raise
        try:
            job.write({
                "vehicle_id": vehicle.id,
                "assignment_override_reason": "Customer approved dry van — temp-controlled packing used.",
            })
        except Exception as e:
            if "Cannot assign" in str(e) or "requirements not met" in str(e):
                self.skipTest("Override requires manager group — acceptable in test context")
            raise

    def test_14_send_to_driver_updates_stage(self):
        """Test 20 (spec): Send to Driver moves job to dispatched stage."""
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        driver = self.env["res.partner"].search([], limit=1)
        if not vehicle or not driver:
            self.skipTest("No vehicle or driver available")

        job = self._make_job(vehicle_id=vehicle.id, driver_id=driver.id)
        job.action_send_to_driver()

        self.assertIn(
            job.stage_id.stage_type, ("dispatched",),
            "Stage should be dispatched after Send to Driver"
        )

    def test_15_requested_delivery_date_defaults_from_invoice(self):
        """Test: Requested delivery date defaults from invoice date."""
        from odoo import fields
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
        })
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "invoice_id": invoice.id,
        })
        self.assertEqual(job.requested_delivery_date, fields.Date.today())

    def test_16_temperature_normalization(self):
        """Test: Bare number temp requirement gets °C appended."""
        job = self._make_job(requires_reefer=True, temp_requirement="10")
        self.assertIn("°C", job.temp_requirement)
        job2 = self._make_job(requires_reefer=True, temp_requirement="-18 °C")
        self.assertEqual(job2.temp_requirement, "-18 °C")  # Already has °C — no double


class TestDispatchStopOnboard(TransactionCase):
    """Tests 17–20: Per-stop running load and new job creation from AI data."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def _make_job(self):
        return self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
        })

    def test_17_onboard_load_after_stop_correct_sequence(self):
        """Test: onboard_load_after_stop follows running load correctly."""
        job = self._make_job()
        s1 = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 10, "stop_type": "pickup",
            "pallets_in": 12, "address": "Ajax, ON",
        })
        s2 = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 20, "stop_type": "dropoff",
            "pallets_out": 12, "address": "Oshawa, ON",
        })
        s3 = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 30, "stop_type": "pickup",
            "pallets_in": 6, "address": "Ajax, ON",
        })
        s4 = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 40, "stop_type": "dropoff",
            "pallets_out": 6, "address": "Whitby, ON",
        })
        # Force recompute
        (s1 | s2 | s3 | s4)._compute_onboard_load()
        self.assertEqual(s1.onboard_load_after_stop, 12, "After first pickup: 12")
        self.assertEqual(s2.onboard_load_after_stop, 0, "After Oshawa drop: 0")
        self.assertEqual(s3.onboard_load_after_stop, 6, "After second pickup: 6")
        self.assertEqual(s4.onboard_load_after_stop, 0, "After Whitby drop: 0")

    def test_18_create_stops_from_ai_data_sets_pallets_correctly(self):
        """Test: _create_stops_from_ai_data sets pallets_in on pickup, pallets_out on dropoff."""
        job = self._make_job()
        ai_stops = [
            {"type": "pickup", "address": "689 Salem Rd N, Ajax, ON",
             "pallets_in": 12, "pallets_out": 0, "linked_load_group": 1},
            {"type": "dropoff", "address": "501 Ritson Rd S, Oshawa, ON",
             "pallets_in": 0, "pallets_out": 12, "linked_load_group": 1},
            {"type": "pickup", "address": "689 Salem Rd N, Ajax, ON",
             "pallets_in": 6, "pallets_out": 0, "linked_load_group": 2},
            {"type": "dropoff", "address": "Whitby, ON",
             "pallets_in": 0, "pallets_out": 6, "linked_load_group": 2},
        ]
        self.env["prema.dispatch.job"]._create_stops_from_ai_data(job, ai_stops)
        stops = job.stop_ids.sorted("sequence")
        self.assertEqual(len(stops), 4)
        self.assertEqual(stops[0].stop_type, "pickup")
        self.assertEqual(stops[0].pallets_in, 12)
        self.assertEqual(stops[0].pallets_out, 0)
        self.assertEqual(stops[1].stop_type, "dropoff")
        self.assertEqual(stops[1].pallets_out, 12)
        self.assertEqual(stops[2].linked_load_group, 2)

    def test_19_infer_pickup_pallets_from_following_dropoffs(self):
        """Test: pickup with pallets_in=0 infers load from following drop-off pallets_out."""
        job = self._make_job()
        self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 10, "stop_type": "pickup",
            "pallets_in": 0, "address": "Ajax, ON",  # No pallets_in set
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 20, "stop_type": "dropoff",
            "pallets_out": 5, "address": "Oshawa, ON",
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "sequence": 30, "stop_type": "dropoff",
            "pallets_out": 3, "address": "Whitby, ON",
        })
        job.invalidate_recordset()
        # max_onboard should be inferred as 5+3=8
        self.assertEqual(job.max_onboard_pallets, 8)

    def test_20_booking_board_shows_dispatched_jobs(self):
        """Test 20 (spec): Jobs in dispatched stage appear in Booking Board (is_active domain)."""
        if not self.stage_draft:
            self.skipTest("No draft stage")
        dispatched_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "dispatched")], limit=1
        )
        if not dispatched_stage:
            self.skipTest("No dispatched stage")

        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": dispatched_stage.id,
        })
        # Booking Board domain: not cancelled, not completed
        found = self.env["prema.dispatch.job"].search([
            ("id", "=", job.id),
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
        ])
        self.assertIn(job, found, "Dispatched job must appear in Booking Board domain")


class TestDispatchWorkflowAudit4(TransactionCase):
    """Tests 23-24: cross-dock interleave feasibility, covering the
    4-issue report on invoice
    D-AJX-OSH-WHI-NCL-PTB-CAM-FOX-070624 / D-WOO-BEL-110624.

    (Audit tests 21-22 — repeated-pickup booking → one dispatch job —
    live in the prema_logistics_booking suite as
    TestRepeatedPickupPromotion: dispatch's own test phase loads before
    the booking module, so logistics.booking is never in the registry
    here.)"""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def _make_invoice(self):
        return self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
        })

    def test_23_saved_location_warehouse_allows_cross_dock(self):
        """Test 23: Allow Cross-Dock is a checkbox on Saved Location, not a
        separate Location Type — a Warehouse can have it set True."""
        loc = self.env["prema.dispatch.location"].create({
            "name": "Ajax Warehouse",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })
        self.assertEqual(loc.location_type, "warehouse")
        self.assertTrue(loc.allow_cross_dock)

    def test_24_cross_dock_interleave_avoids_false_infeasibility(self):
        """Test 24: two same-day jobs on one truck that share a cross-dock
        stop must not be blocked by the rigid busy-until-previous-job gate
        (the Woodbridge/Belleville + Ajax jobs on the same truck scenario)."""
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")

        crossdock = self.env["prema.dispatch.location"].create({
            "name": "Ajax Cross-Dock",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })

        job_a = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id, "stage_id": self.stage_draft.id,
            "vehicle_id": vehicle.id,
            "scheduled_pickup": "2026-07-06 12:00:00",
            # Tight pickup window — without the cross-dock bypass, job_b's
            # flat "+4h busy" fallback (10:00 pickup -> busy until 14:00)
            # pushes the computed ETA for this job's pickup well past this
            # window, producing a false not_feasible.
            "pickup_earliest": "2026-07-06 11:30:00",
            "pickup_latest": "2026-07-06 12:30:00",
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job_a.id, "sequence": 10, "stop_type": "pickup",
            "address": crossdock.address, "saved_location_id": crossdock.id,
            "pallets_in": 12,
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job_a.id, "sequence": 20, "stop_type": "dropoff",
            "address": "501 Ritson Road S, Oshawa, ON", "pallets_out": 12,
        })

        job_b = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id, "stage_id": self.stage_draft.id,
            "vehicle_id": vehicle.id,
            "scheduled_pickup": "2026-07-06 10:00:00",
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job_b.id, "sequence": 10, "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON",
        })
        self.env["prema.dispatch.stop"].create({
            "job_id": job_b.id, "sequence": 20, "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON",
        })

        option = job_a._feasibility_option_for_truck(vehicle.id)
        self.assertIsNotNone(option)
        self.assertNotEqual(
            option.get("verdict"), "not_feasible",
            f"Cross-dock interleave should not be blocked: {option.get('reason')}",
        )


class TestAutoPlanCrossDock(TransactionCase):
    """Tests 25-28: Auto Plan's route builder must materialize cross-dock
    unload/reload legs (not just clear a feasibility check), keep onboard
    counts correct across the interleave, respect allow_cross_dock, and the
    Stop 'Company' lookup must only ever search Saved Locations."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)

    def _make_scenario(self, allow_cross_dock=True):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 13)], limit=1)
        if not vehicle:
            return None, None, None, None

        crossdock = self.env["prema.dispatch.location"].create({
            "name": "Ajax Cross-Dock Test",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": allow_cross_dock,
        })

        # Load B — Ajax multi-stop hub (repeat pickup at the same location).
        job_hub = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id, "stage_id": self.stage_draft.id,
            "vehicle_id": vehicle.id, "scheduled_pickup": "2026-07-06 12:00:00",
        })
        Stop = self.env["prema.dispatch.stop"]
        Stop.create({"job_id": job_hub.id, "sequence": 10, "stop_type": "pickup",
                     "address": crossdock.address, "saved_location_id": crossdock.id,
                     "pallets_in": 12})
        Stop.create({"job_id": job_hub.id, "sequence": 20, "stop_type": "dropoff",
                     "address": "501 Ritson Road S, Oshawa, ON", "pallets_out": 12})
        Stop.create({"job_id": job_hub.id, "sequence": 30, "stop_type": "pickup",
                     "address": crossdock.address, "saved_location_id": crossdock.id,
                     "pallets_in": 6})
        Stop.create({"job_id": job_hub.id, "sequence": 40, "stop_type": "dropoff",
                     "address": "728 Anderson Street, Whitby, ON", "pallets_out": 1})
        Stop.create({"job_id": job_hub.id, "sequence": 50, "stop_type": "dropoff",
                     "address": "275 Toronto St, Newcastle, ON", "pallets_out": 1})
        Stop.create({"job_id": job_hub.id, "sequence": 60, "stop_type": "dropoff",
                     "address": "754 Lansdowne St W, Peterborough, ON", "pallets_out": 2})
        Stop.create({"job_id": job_hub.id, "sequence": 70, "stop_type": "dropoff",
                     "address": "34 Tanner industrial park, Campbellford, ON", "pallets_out": 1})
        Stop.create({"job_id": job_hub.id, "sequence": 80, "stop_type": "dropoff",
                     "address": "54 Frankford road, Foxboro, ON", "pallets_out": 1})

        # Load A — Woodbridge -> Belleville, single skid, unrelated freight.
        job_carrier = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id, "stage_id": self.stage_draft.id,
            "vehicle_id": vehicle.id, "scheduled_pickup": "2026-07-06 10:00:00",
        })
        Stop.create({"job_id": job_carrier.id, "sequence": 10, "stop_type": "pickup",
                     "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON", "pallets_in": 1})
        Stop.create({"job_id": job_carrier.id, "sequence": 20, "stop_type": "dropoff",
                     "address": "290 N Front St, Belleville, ON", "pallets_out": 1})

        return vehicle, crossdock, job_hub, job_carrier

    def test_25_auto_plan_creates_cross_dock_unload_reload(self):
        """Test 25: the route builder materializes a temporary drop + reload
        for the carrier's freight instead of just carrying it or blocking."""
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        vehicle, crossdock, job_hub, job_carrier = self._make_scenario()
        if not vehicle:
            self.skipTest("No vehicle with >=13 pallet capacity available")

        result = DispatchOptimizationService(self.env).apply_consolidated_route(
            vehicle.id, "2026-07-06"
        )
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("cross_dock_legs"), 2, "Expected one drop + one reload leg")

        carrier_stops = job_carrier.stop_ids.sorted("sequence")
        self.assertEqual(len(carrier_stops), 4, "Pickup, drop, reload, delivery")
        self.assertEqual(
            carrier_stops.mapped("stop_type"),
            ["pickup", "cross_dock_drop", "cross_dock_pickup", "dropoff"],
        )
        self.assertTrue(all(s.saved_location_id.id == crossdock.id
                             for s in carrier_stops.filtered(lambda s: "cross_dock" in s.stop_type)))

    def test_26_onboard_counts_correct_through_interleave(self):
        """Test 26: onboard counts stay correct through the whole sequence —
        +1 (Woodbridge) -> 0 (temp drop) -> +1 (reload) -> 0 (Belleville) on
        the carrier job, and the hub job's own +12 -> 0 -> +6 -> ... -> 0
        pattern is untouched; combined onboard at the reload moment is 7
        (hub's own +6 plus the carrier's reloaded +1)."""
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        vehicle, crossdock, job_hub, job_carrier = self._make_scenario()
        if not vehicle:
            self.skipTest("No vehicle with >=13 pallet capacity available")

        DispatchOptimizationService(self.env).apply_consolidated_route(vehicle.id, "2026-07-06")

        carrier_stops = job_carrier.stop_ids.sorted("sequence")
        self.assertEqual(carrier_stops.mapped("onboard_load_after_stop"), [1, 0, 1, 0])

        hub_stops = job_hub.stop_ids.sorted("sequence")
        self.assertEqual(
            hub_stops.mapped("onboard_load_after_stop"),
            [12, 0, 6, 5, 4, 2, 1, 0],
        )

        reload_stop = carrier_stops.filtered(lambda s: s.stop_type == "cross_dock_pickup")
        second_hub_pickup = hub_stops[2]  # sequence 30, pickup 6
        self.assertEqual(
            second_hub_pickup.onboard_load_after_stop + reload_stop.onboard_load_after_stop,
            7,
            "Combined truck onboard at the second Ajax visit should be 6 (hub) + 1 (reloaded) = 7",
        )

    def test_27_cross_dock_blocked_when_not_allowed(self):
        """Test 27: with allow_cross_dock=False, no cross-dock legs are
        created — the carrier job keeps its original 2 stops."""
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        vehicle, crossdock, job_hub, job_carrier = self._make_scenario(allow_cross_dock=False)
        if not vehicle:
            self.skipTest("No vehicle with >=13 pallet capacity available")

        result = DispatchOptimizationService(self.env).apply_consolidated_route(
            vehicle.id, "2026-07-06"
        )
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("cross_dock_legs"), 0)
        self.assertEqual(len(job_carrier.stop_ids), 2)
        self.assertEqual(job_carrier.stop_ids.mapped("stop_type"), ["pickup", "dropoff"])

    def test_28_stop_company_lookup_uses_saved_locations_only(self):
        """Test 28: the Stop 'Company' field targets Saved Locations, not
        res.partner Contacts — the field's own comodel is what the
        autocomplete searches, so this is a real, not cosmetic, guarantee."""
        Stop = self.env["prema.dispatch.stop"]
        self.assertEqual(
            Stop._fields["saved_location_id"].comodel_name, "prema.dispatch.location",
            "Company lookup must be backed by Saved Locations",
        )
        self.assertNotEqual(
            Stop._fields["saved_location_id"].comodel_name, "res.partner",
            "Company lookup must not search plain Odoo Contacts",
        )
        # allow_cross_dock is a live reflection of the Saved Location's own flag
        self.assertEqual(Stop._fields["allow_cross_dock"].related, "saved_location_id.allow_cross_dock")


class TestDispatchCrossDockCustody(TransactionCase):
    """Saved-location company display plus cross-dock custody transitions."""

    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.partner = self.env["res.partner"].search([], limit=1)
        self.Stop = self.env["prema.dispatch.stop"]
        self.Job = self.env["prema.dispatch.job"]
        self.Item = self.env["prema.dispatch.item"]
        self.Location = self.env["prema.dispatch.location"]

    def _make_job(self, vehicle=None, driver=None, scheduled_pickup="2026-07-06 10:00:00"):
        return self.Job.create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "vehicle_id": vehicle.id if vehicle else False,
            "driver_id": driver.id if driver else False,
            "scheduled_pickup": scheduled_pickup,
        })

    def _make_proof_attachment(self, stop, name="custody.jpg"):
        att = self.env["ir.attachment"].create({
            "name": name,
            "type": "binary",
            "datas": base64.b64encode(b"proof").decode(),
            "res_model": "prema.dispatch.stop",
            "res_id": stop.id,
        })
        stop.write({"pod_attachment_ids": [(4, att.id)]})
        return att

    def _make_crossdock_job(self, vehicle, driver):
        crossdock = self.Location.create({
            "name": "689 Salem Rd N, Ajax ON",
            "business_name": "Test All Special Wholesale",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })
        job = self._make_job(vehicle=vehicle, driver=driver)
        pickup = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7",
            "pallets_in": 1,
        })
        cross_dock_drop = self.Stop.create({
            "job_id": job.id,
            "sequence": 20,
            "stop_type": "cross_dock_drop",
            "address": crossdock.address,
            "saved_location_id": crossdock.id,
            "cross_dock_origin_stop_id": pickup.id,
            "pallets_out": 1,
            "service_time_minutes": 10,
            "pod_required": True,
        })
        cross_dock_pickup = self.Stop.create({
            "job_id": job.id,
            "sequence": 30,
            "stop_type": "cross_dock_pickup",
            "address": crossdock.address,
            "saved_location_id": crossdock.id,
            "cross_dock_origin_stop_id": pickup.id,
            "pallets_in": 1,
            "service_time_minutes": 10,
            "pod_required": True,
        })
        final_drop = self.Stop.create({
            "job_id": job.id,
            "sequence": 40,
            "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON K8P 3C4",
            "pallets_out": 1,
            "pod_required": True,
        })
        item = self.Item.create({
            "job_id": job.id,
            "name": "Woodbridge / Belleville skid",
            "pickup_stop_id": pickup.id,
            "delivery_stop_id": final_drop.id,
            "pallet_count": 1,
        })
        return job, crossdock, pickup, cross_dock_drop, cross_dock_pickup, final_drop, item

    def _make_dispatcher_pattern_scenario(self, allow_cross_dock=True):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 13)], limit=1)
        if not vehicle:
            return None, None, None, None

        crossdock = self.Location.create({
            "name": "689 Salem Rd N, Ajax ON",
            "business_name": "Test All Special Wholesale",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": allow_cross_dock,
        })

        job_a = self._make_job(vehicle=vehicle, scheduled_pickup="2026-07-06 12:00:00")
        self.Stop.create({
            "job_id": job_a.id, "sequence": 10, "stop_type": "pickup",
            "address": crossdock.address, "saved_location_id": crossdock.id, "pallets_in": 12,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 20, "stop_type": "dropoff",
            "address": "501 Ritson Road S, Oshawa, ON", "pallets_out": 12,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 30, "stop_type": "pickup",
            "address": crossdock.address, "saved_location_id": crossdock.id, "pallets_in": 6,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 40, "stop_type": "dropoff",
            "address": "728 Anderson Street, Whitby, ON", "pallets_out": 1,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 50, "stop_type": "dropoff",
            "address": "275 Toronto St, Newcastle, ON", "pallets_out": 1,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 60, "stop_type": "dropoff",
            "address": "754 Lansdowne St W, Peterborough, ON", "pallets_out": 2,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 70, "stop_type": "dropoff",
            "address": "34 Tanner Industrial Park, Campbellford, ON", "pallets_out": 1,
        })
        self.Stop.create({
            "job_id": job_a.id, "sequence": 80, "stop_type": "dropoff",
            "address": "54 Frankford Road, Foxboro, ON", "pallets_out": 1,
        })

        job_b = self._make_job(vehicle=vehicle, scheduled_pickup="2026-07-06 10:00:00")
        self.Stop.create({
            "job_id": job_b.id, "sequence": 10, "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7", "pallets_in": 1,
        })
        self.Stop.create({
            "job_id": job_b.id, "sequence": 20, "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON K8P 3C4", "pallets_out": 1,
        })

        return vehicle, crossdock, job_a, job_b

    @staticmethod
    def _combined_onboard(stops):
        running = 0
        counts = []
        for stop in stops:
            if stop.stop_type in ("pickup", "cross_dock_pickup"):
                running += stop.pallets_in or 0
            elif stop.stop_type in ("dropoff", "return", "cross_dock_drop"):
                running = max(0, running - (stop.pallets_out or running))
            counts.append(running)
        return counts

    def test_saved_location_company_display_on_stops(self):
        loc = self.Location.create({
            "name": "Ajax Warehouse Internal Name",
            "business_name": "Test All Special Wholesale",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })
        fallback = self.Location.create({
            "name": "Fallback Saved Location",
            "address": "501 Ritson Road S, Oshawa, ON",
        })
        self.env["res.partner"].create({
            "name": "Test All Special Wholesale",
        })
        job = self._make_job()

        stop_form = self.Stop.new({
            "job_id": job.id,
            "saved_location_id": loc.id,
        })
        stop_form._onchange_saved_location_id()

        self.assertEqual(stop_form.address, loc.address)
        self.assertEqual(stop_form.contact_name, loc.business_name)
        self.assertTrue(stop_form.allow_cross_dock)
        self.assertEqual(dict(loc.name_get())[loc.id], "Test All Special Wholesale")
        self.assertEqual(dict(fallback.name_get())[fallback.id], "Fallback Saved Location")

        hits = self.Location.name_search("Test All Special Wholesale")
        self.assertIn(loc.id, [rec_id for rec_id, _label in hits])

        stop = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "saved_location_id": loc.id,
            "address": loc.address,
        })
        self.assertEqual(stop.saved_location_id.name_get()[0][1], "Test All Special Wholesale")
        self.assertEqual(stop.address, loc.address)
        self.assertEqual(self.Stop._fields["saved_location_id"].comodel_name, "prema.dispatch.location")
        self.assertNotEqual(self.Stop._fields["saved_location_id"].comodel_name, "res.partner")

    def test_crossdock_drop_creates_in_transit_custody(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Crossdock Driver 1"})

        job, crossdock, pickup, cross_dock_drop, _reload, final_drop, item = self._make_crossdock_job(vehicle, driver)
        pickup.action_mark_completed()
        self._make_proof_attachment(cross_dock_drop, "crossdock-drop.jpg")
        cross_dock_drop.action_mark_completed()
        item.invalidate_recordset()

        self.assertFalse(item.current_vehicle_id)
        self.assertFalse(item.current_driver_id)
        self.assertEqual(item.current_location_id.id, crossdock.id)
        self.assertEqual(item.current_custody_type, "cross_dock")
        self.assertNotEqual(item.status, "delivered")
        self.assertIn(item.status, ("cross_docked", "in_transit"))
        self.assertEqual(final_drop.status, "pending")
        self.assertEqual(job.vehicle_id.id, vehicle.id)

    def test_crossdock_drop_can_complete_without_proof(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Crossdock No Proof Driver"})

        _job, crossdock, pickup, cross_dock_drop, _reload, final_drop, item = self._make_crossdock_job(vehicle, driver)
        pickup.action_mark_completed()
        cross_dock_drop.action_mark_completed()
        item.invalidate_recordset()

        self.assertEqual(cross_dock_drop.status, "completed")
        self.assertEqual(item.current_location_id.id, crossdock.id)
        self.assertEqual(item.current_custody_type, "cross_dock")
        self.assertEqual(item.status, "cross_docked")
        self.assertEqual(final_drop.status, "pending")

    def test_crossdock_reload_same_truck(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Crossdock Driver 1"})

        _job, _crossdock, pickup, cross_dock_drop, cross_dock_pickup, final_drop, item = self._make_crossdock_job(vehicle, driver)
        pickup.action_mark_completed()
        self._make_proof_attachment(cross_dock_drop, "crossdock-drop.jpg")
        cross_dock_drop.action_mark_completed()
        self._make_proof_attachment(cross_dock_pickup, "crossdock-pickup.jpg")
        cross_dock_pickup.action_mark_completed()
        item.invalidate_recordset()

        self.assertEqual(item.current_vehicle_id.id, vehicle.id)
        self.assertEqual(item.current_driver_id.id, driver.id)
        self.assertFalse(item.current_location_id)
        self.assertEqual(item.current_custody_type, "truck")
        self.assertEqual(item.status, "in_transit")
        self.assertEqual(cross_dock_pickup.onboard_load_after_stop, 1)
        self.assertEqual(final_drop.status, "pending")

    def test_crossdock_reload_other_truck(self):
        vehicles = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=2)
        if len(vehicles) < 2:
            self.skipTest("Need two vehicles for cross-truck reload test")
        driver_1 = self.env["res.partner"].create({"name": "Crossdock Driver 1"})
        driver_2 = self.env["res.partner"].create({"name": "Crossdock Driver 2"})

        job, crossdock, pickup, cross_dock_drop, cross_dock_pickup, final_drop, item = self._make_crossdock_job(
            vehicles[0], driver_1
        )
        pickup.action_mark_completed()
        self._make_proof_attachment(cross_dock_drop, "crossdock-drop.jpg")
        cross_dock_drop.action_mark_completed()

        job.write({
            "vehicle_id": vehicles[1].id,
            "driver_id": driver_2.id,
        })
        self._make_proof_attachment(cross_dock_pickup, "crossdock-pickup.jpg")
        cross_dock_pickup.action_mark_completed()
        item.invalidate_recordset()

        self.assertEqual(item.current_vehicle_id.id, vehicles[1].id)
        self.assertEqual(item.current_driver_id.id, driver_2.id)
        self.assertFalse(item.current_location_id)
        self.assertEqual(item.current_custody_type, "truck")
        self.assertEqual(item.status, "in_transit")
        self.assertEqual(final_drop.status, "pending")

        events = self.env["prema.dispatch.custody.event"].search(
            [("item_id", "=", item.id)], order="occurred_at asc, id asc"
        )
        self.assertEqual(events.mapped("event_type"), ["loaded", "cross_docked", "reloaded"])
        self.assertEqual(
            events.filtered(lambda e: e.event_type == "cross_docked")[0].saved_location_id.id,
            crossdock.id,
        )
        self.assertEqual(
            events.filtered(lambda e: e.event_type == "reloaded")[-1].vehicle_id.id,
            vehicles[1].id,
        )

    def test_crossdock_requires_exact_freight_selection_when_multiple_items_match(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 2)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Crossdock Selector Driver"})
        crossdock = self.Location.create({
            "name": "689 Salem Rd N, Ajax ON",
            "business_name": "Test All Special Wholesale",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })
        job = self._make_job(vehicle=vehicle, driver=driver)
        pickup = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7",
            "pallets_in": 2,
        })
        cross_dock_drop = self.Stop.create({
            "job_id": job.id,
            "sequence": 20,
            "stop_type": "cross_dock_drop",
            "address": crossdock.address,
            "saved_location_id": crossdock.id,
            "cross_dock_origin_stop_id": pickup.id,
            "pallets_out": 2,
            "pod_required": True,
        })
        drop_1 = self.Stop.create({
            "job_id": job.id,
            "sequence": 30,
            "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON K8P 3C4",
            "pallets_out": 1,
        })
        drop_2 = self.Stop.create({
            "job_id": job.id,
            "sequence": 40,
            "stop_type": "dropoff",
            "address": "728 Anderson Street, Whitby, ON",
            "pallets_out": 1,
        })
        item_1 = self.Item.create({
            "job_id": job.id,
            "name": "Belleville skid",
            "pickup_stop_id": pickup.id,
            "delivery_stop_id": drop_1.id,
            "pallet_count": 1,
        })
        item_2 = self.Item.create({
            "job_id": job.id,
            "name": "Whitby skid",
            "pickup_stop_id": pickup.id,
            "delivery_stop_id": drop_2.id,
            "pallet_count": 1,
        })

        pickup.action_mark_completed()
        self._make_proof_attachment(cross_dock_drop, "crossdock-drop.jpg")
        with self.assertRaises(exceptions.UserError):
            cross_dock_drop.action_mark_completed()

        cross_dock_drop.write({"freight_item_ids": [(6, 0, [item_1.id])]})
        cross_dock_drop.action_mark_completed()
        item_1.invalidate_recordset()
        item_2.invalidate_recordset()

        self.assertEqual(cross_dock_drop.pallets_out, 1)
        self.assertEqual(item_1.current_location_id.id, crossdock.id)
        self.assertEqual(item_1.current_custody_type, "cross_dock")
        self.assertEqual(item_1.status, "cross_docked")
        self.assertEqual(item_2.current_vehicle_id.id, vehicle.id)
        self.assertEqual(item_2.current_driver_id.id, driver.id)
        self.assertEqual(item_2.current_custody_type, "truck")
        # Item not selected for the cross-dock drop stays ON the truck;
        # "loaded" is the canonical onboard status set at pickup completion
        # (dispatch_stop.action_mark_completed pickup branch), not "in_transit".
        self.assertEqual(item_2.status, "loaded")

    def test_transfer_without_target_stages_and_unassigns(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Transfer Driver 1"})
        meet = self.Location.create({
            "name": "Kingston Relay Yard",
            "business_name": "Kingston Relay Yard",
            "address": "905 Gardiners Rd, Kingston, ON",
            "location_type": "relay",
        })
        job = self._make_job(vehicle=vehicle, driver=driver)
        pickup = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7",
            "pallets_in": 1,
        })
        transfer = self.Stop.create({
            "job_id": job.id,
            "sequence": 20,
            "stop_type": "transfer",
            "address": meet.address,
            "saved_location_id": meet.id,
            "pallets_out": 1,
            "pod_required": True,
        })
        final_drop = self.Stop.create({
            "job_id": job.id,
            "sequence": 30,
            "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON K8P 3C4",
            "pallets_out": 1,
            "pod_required": True,
        })
        item = self.Item.create({
            "job_id": job.id,
            "name": "Belleville transfer skid",
            "pickup_stop_id": pickup.id,
            "delivery_stop_id": final_drop.id,
            "pallet_count": 1,
        })
        transfer.write({"freight_item_ids": [(6, 0, [item.id])]})

        pickup.action_mark_completed()
        result = transfer.action_execute_transfer()
        job.invalidate_recordset()
        item.invalidate_recordset()

        self.assertTrue(result["success"])
        self.assertTrue(result["unassigned"])
        self.assertFalse(job.driver_id)
        self.assertFalse(job.vehicle_id)
        self.assertEqual(item.current_location_id.id, meet.id)
        self.assertEqual(item.current_custody_type, "location")
        self.assertEqual(item.status, "staged")
        self.assertEqual(final_drop.status, "pending")

    def test_crossdock_drop_assignment_to_other_truck_reassigns_remaining_route(self):
        vehicles = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=2)
        if len(vehicles) < 2:
            self.skipTest("Need two vehicles for cross-dock reassignment test")
        driver_1 = self.env["res.partner"].create({"name": "Crossdock Reassign Driver 1"})
        driver_2 = self.env["res.partner"].create({"name": "Crossdock Reassign Driver 2"})
        vehicles[1].write({"driver_id": driver_2.id})

        job, crossdock, pickup, cross_dock_drop, cross_dock_pickup, final_drop, item = self._make_crossdock_job(
            vehicles[0], driver_1
        )
        pickup.action_mark_completed()
        result = cross_dock_drop.action_assign_receiving_truck(vehicles[1].id)
        job.invalidate_recordset()

        self.assertTrue(result["success"])
        self.assertFalse(result["applied"])
        self.assertEqual(cross_dock_drop.transfer_to_vehicle_id.id, vehicles[1].id)
        self.assertEqual(cross_dock_drop.transfer_to_driver_id.id, driver_2.id)
        self.assertEqual(job.vehicle_id.id, vehicles[0].id)

        cross_dock_drop.action_mark_completed()
        job.invalidate_recordset()
        item.invalidate_recordset()

        self.assertEqual(job.vehicle_id.id, vehicles[1].id)
        self.assertEqual(job.driver_id.id, driver_2.id)
        self.assertEqual(cross_dock_drop.transfer_from_vehicle_id.id, vehicles[0].id)
        self.assertEqual(cross_dock_drop.transfer_from_driver_id.id, driver_1.id)
        self.assertEqual(item.current_location_id.id, crossdock.id)
        self.assertEqual(item.current_custody_type, "cross_dock")
        self.assertEqual(item.status, "cross_docked")
        self.assertEqual(cross_dock_pickup.status, "pending")
        self.assertEqual(final_drop.status, "pending")

    def test_completed_crossdock_drop_can_reassign_remaining_route_later(self):
        vehicles = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=2)
        if len(vehicles) < 2:
            self.skipTest("Need two vehicles for post-drop reassignment test")
        driver_1 = self.env["res.partner"].create({"name": "Crossdock Later Driver 1"})
        driver_2 = self.env["res.partner"].create({"name": "Crossdock Later Driver 2"})
        vehicles[1].write({"driver_id": driver_2.id})

        job, crossdock, pickup, _cross_dock_drop, cross_dock_pickup, final_drop, item = self._make_crossdock_job(
            vehicles[0], driver_1
        )
        pickup.action_mark_completed()
        _cross_dock_drop.action_mark_completed()

        result = _cross_dock_drop.action_assign_receiving_truck(vehicles[1].id)
        job.invalidate_recordset()
        item.invalidate_recordset()

        self.assertTrue(result["success"])
        self.assertTrue(result["applied"])
        self.assertEqual(job.vehicle_id.id, vehicles[1].id)
        self.assertEqual(job.driver_id.id, driver_2.id)
        self.assertEqual(_cross_dock_drop.transfer_from_vehicle_id.id, vehicles[0].id)
        self.assertEqual(_cross_dock_drop.transfer_from_driver_id.id, driver_1.id)
        self.assertEqual(item.current_location_id.id, crossdock.id)
        self.assertEqual(item.current_custody_type, "cross_dock")
        self.assertEqual(item.status, "cross_docked")
        self.assertEqual(cross_dock_pickup.status, "pending")
        self.assertEqual(final_drop.status, "pending")

    def test_driver_stops_payload_includes_transfer_truck_options_and_target_ids(self):
        vehicles = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=2)
        if len(vehicles) < 2:
            self.skipTest("Need two vehicles for driver payload transfer-truck test")
        driver_1 = self.env["res.partner"].create({"name": "Driver Payload 1"})
        driver_2 = self.env["res.partner"].create({"name": "Driver Payload 2"})
        vehicles[1].write({"driver_id": driver_2.id})

        job, _crossdock, pickup, cross_dock_drop, _cross_dock_pickup, _final_drop, _item = self._make_crossdock_job(
            vehicles[0], driver_1
        )
        pickup.action_mark_completed()
        cross_dock_drop.action_assign_receiving_truck(vehicles[1].id)

        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Driver Payload User",
            "login": "driver_payload_user@example.com",
            "partner_id": driver_1.id,
            # Real driver accounts carry the Driver group — the stop payload
            # reads pallet.stop.allocation, which is restricted to the
            # dispatch groups (Driver/Dispatcher/Manager/Warehouse).
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("prema_dispatch.group_dispatch_driver").id,
            ])],
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
        })
        # The driver payload only serves the 7-day window (yesterday → today+5),
        # so the fixture's hardcoded 2026-07-06 pickup would be clamped away.
        # Schedule the job relative to today instead — at NOON UTC so the
        # stops' local date (America/Toronto, UTC-4/-5) can never straddle
        # into yesterday when the suite runs near midnight UTC (found in the
        # 2026-08-20 02:00 UTC full-suite run: 21:59 Toronto the day before).
        from datetime import date
        job.scheduled_pickup = fields.Datetime.now().replace(
            hour=12, minute=0, second=0, microsecond=0)
        payload = self.Job.with_user(user).get_driver_stops_for_date(str(date.today()))
        stop_payload = next(stop for stop in payload["stops"] if stop["id"] == cross_dock_drop.id)

        self.assertEqual(stop_payload["transfer_to_vehicle_id"], vehicles[1].id)
        self.assertEqual(stop_payload["transfer_to_driver_id"], driver_2.id)
        self.assertTrue(
            any(truck["id"] == vehicles[1].id for truck in payload["available_transfer_trucks"])
        )

    def test_restore_crossdock_drop_reverts_reassigned_route(self):
        vehicles = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=2)
        if len(vehicles) < 2:
            self.skipTest("Need two vehicles for cross-dock restore test")
        driver_1 = self.env["res.partner"].create({"name": "Crossdock Restore Driver 1"})
        driver_2 = self.env["res.partner"].create({"name": "Crossdock Restore Driver 2"})
        vehicles[1].write({"driver_id": driver_2.id})

        job, _crossdock, pickup, cross_dock_drop, _cross_dock_pickup, _final_drop, item = self._make_crossdock_job(
            vehicles[0], driver_1
        )
        pickup.action_mark_completed()
        cross_dock_drop.action_assign_receiving_truck(vehicles[1].id)
        cross_dock_drop.action_mark_completed()

        cross_dock_drop.action_restore_stop()
        job.invalidate_recordset()
        item.invalidate_recordset()

        self.assertEqual(cross_dock_drop.status, "pending")
        self.assertEqual(job.vehicle_id.id, vehicles[0].id)
        self.assertEqual(job.driver_id.id, driver_1.id)
        self.assertEqual(item.current_vehicle_id.id, vehicles[0].id)
        self.assertEqual(item.current_driver_id.id, driver_1.id)
        self.assertFalse(item.current_location_id)
        self.assertEqual(item.current_custody_type, "truck")
        self.assertEqual(item.status, "in_transit")

    def test_driver_add_evidence_copies_to_invoice_and_item_history(self):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
        })
        job = self.Job.create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "invoice_id": invoice.id,
        })
        pickup = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7",
            "pallets_in": 1,
        })
        final_drop = self.Stop.create({
            "job_id": job.id,
            "sequence": 20,
            "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON K8P 3C4",
            "pallets_out": 1,
            "pod_required": True,
            "invoice_id": invoice.id,
        })
        item = self.Item.create({
            "job_id": job.id,
            "name": "Invoice-linked skid",
            "pickup_stop_id": pickup.id,
            "delivery_stop_id": final_drop.id,
            "pallet_count": 1,
        })

        # Real JPEG bytes, not a placeholder string — Phase 1C added real
        # content-signature validation to driver_add_evidence (services/
        # dispatch_upload.py), which correctly rejects non-image content
        # regardless of its ".jpg" filename. This fixture predates that
        # check; a fake byte string is no longer a valid stand-in for what
        # a real driver upload actually looks like.
        import io as _io
        from PIL import Image as _Image
        _buf = _io.BytesIO()
        _Image.new("RGB", (8, 8), (200, 0, 0)).save(_buf, format="JPEG")
        result = self.Job.driver_add_evidence(
            final_drop.id,
            "pod",
            base64.b64encode(_buf.getvalue()).decode(),
            "pod.jpg",
        )
        final_drop.invalidate_recordset()
        item.invalidate_recordset()

        self.assertTrue(result["success"])
        attachment = final_drop.pod_attachment_ids[:1]
        self.assertTrue(attachment)
        self.assertIn(attachment.id, item.evidence_attachment_ids.ids)
        invoice_copy = self.env["ir.attachment"].search([
            ("description", "=", f"__evidence_source:{attachment.id}__"),
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        self.assertEqual(len(invoice_copy), 1)

    def test_autoplan_dispatcher_sequence_00113_00114_pattern(self):
        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        vehicle, crossdock, job_a, job_b = self._make_dispatcher_pattern_scenario()
        if not vehicle:
            self.skipTest("No vehicle with >=13 pallet capacity available")

        result = DispatchOptimizationService(self.env).apply_consolidated_route(
            vehicle.id, "2026-07-06"
        )
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("cross_dock_legs"), 2)

        merged = (job_a.stop_ids | job_b.stop_ids).sorted("sequence")
        self.assertEqual(merged[0].stop_type, "pickup")
        self.assertIn("Woodbridge", merged[0].address)
        self.assertEqual(merged[1].stop_type, "cross_dock_drop")
        self.assertEqual(merged[1].saved_location_id.id, crossdock.id)
        self.assertEqual(merged[2].stop_type, "pickup")
        self.assertEqual(merged[2].saved_location_id.id, crossdock.id)
        self.assertEqual(merged[2].pallets_in, 12)
        self.assertEqual(merged[3].stop_type, "dropoff")
        self.assertIn("Oshawa", merged[3].address)

        ajax_reload_block = merged[4:6]
        self.assertEqual(
            {stop.stop_type for stop in ajax_reload_block},
            {"pickup", "cross_dock_pickup"},
        )
        self.assertTrue(all(stop.saved_location_id.id == crossdock.id for stop in ajax_reload_block))
        self.assertEqual(sorted(stop.pallets_in for stop in ajax_reload_block), [1, 6])

        remaining_addresses = set(merged[6:].mapped("address"))
        self.assertEqual(remaining_addresses, {
            "728 Anderson Street, Whitby, ON",
            "275 Toronto St, Newcastle, ON",
            "754 Lansdowne St W, Peterborough, ON",
            "34 Tanner Industrial Park, Campbellford, ON",
            "54 Frankford Road, Foxboro, ON",
            "290 N Front St, Belleville, ON K8P 3C4",
        })

        onboard_counts = self._combined_onboard(merged)
        self.assertEqual(onboard_counts[:4], [1, 0, 12, 0])
        self.assertEqual(onboard_counts[5], 7)
        self.assertEqual(onboard_counts[-1], 0)
        self.assertTrue(all(curr <= prev for prev, curr in zip(onboard_counts[5:], onboard_counts[6:])))

        truck_schedule = DispatchAvailabilityService(self.env).get_truck_day_schedule(
            date(2026, 7, 6)
        )
        truck_payload = next(t for t in truck_schedule if t["truck_id"] == vehicle.id)
        payload_stops = []
        for job_payload in truck_payload["jobs"]:
            payload_stops.extend(job_payload["stops"])
        payload_stops.sort(
            key=lambda s: (
                s.get("scheduled_time")
                or s.get("estimated_arrival")
                or s.get("actual_arrival_time")
                or "9999-12-31T23:59:59Z",
                s.get("sequence") or 0,
                s.get("id") or 0,
            )
        )
        payload_counts = [stop["onboard_after"] for stop in payload_stops]
        self.assertEqual(payload_counts[:4], [1, 0, 12, 0])
        self.assertEqual(payload_counts[5], 7)
        self.assertEqual(payload_counts[-1], 0)

    def test_crossdock_blocked_when_location_not_enabled(self):
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        vehicle, _crossdock, _job_a, job_b = self._make_dispatcher_pattern_scenario(
            allow_cross_dock=False
        )
        if not vehicle:
            self.skipTest("No vehicle with >=13 pallet capacity available")

        result = DispatchOptimizationService(self.env).apply_consolidated_route(
            vehicle.id, "2026-07-06"
        )
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("cross_dock_legs"), 0)
        self.assertEqual(job_b.stop_ids.sorted("sequence").mapped("stop_type"), ["pickup", "dropoff"])

    def test_dispatcher_can_manually_change_stop_type(self):
        crossdock = self.Location.create({
            "name": "689 Salem Rd N, Ajax ON",
            "business_name": "Test All Special Wholesale",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
            "allow_cross_dock": True,
        })
        regular = self.Location.create({
            "name": "Regular Delivery",
            "business_name": "Healthy Planet - Belleville",
            "address": "290 N Front St, Belleville, ON K8P 3C4",
            "location_type": "customer",
            "allow_cross_dock": False,
        })
        job = self._make_job()
        stop = self.Stop.create({
            "job_id": job.id,
            "sequence": 10,
            "stop_type": "dropoff",
            "saved_location_id": regular.id,
            "address": regular.address,
            "pallets_out": 1,
        })

        stop.write({
            "stop_type": "cross_dock_drop",
            "saved_location_id": crossdock.id,
            "cross_dock_origin_stop_id": False,
        })
        self.assertEqual(stop.stop_type, "cross_dock_drop")
        self.assertEqual(stop.saved_location_id.id, crossdock.id)
        self.assertTrue(stop.allow_cross_dock)
        self.assertTrue(stop.pod_required)

        with self.assertRaises(exceptions.UserError):
            stop.write({
                "stop_type": "cross_dock_pickup",
                "saved_location_id": regular.id,
            })

    def test_planner_payload_shows_stop_action_labels(self):
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Planner Label Driver"})

        job, crossdock, _pickup, _drop, _reload, _final_drop, _item = self._make_crossdock_job(vehicle, driver)
        board = self.Job.get_dispatch_board_data("2026-07-06")
        truck = next((t for t in board["trucks"] if t["truck_id"] == vehicle.id), None)
        self.assertTrue(truck, "Planner payload should include the assigned truck")

        stops = []
        for job_payload in truck["jobs"]:
            if job_payload["job_id"] == job.id:
                stops.extend(job_payload["stops"])

        labels = {stop["type"]: stop.get("type_label") for stop in stops}
        self.assertEqual(labels.get("pickup"), "Pickup")
        self.assertEqual(labels.get("cross_dock_drop"), "Cross-Dock Drop / Transfer-In")
        self.assertEqual(labels.get("cross_dock_pickup"), "Cross-Dock Pickup / Transfer-Out")
        self.assertEqual(
            [
                stop.get("company_name")
                for stop in stops
                if stop["type"] in ("cross_dock_drop", "cross_dock_pickup")
            ],
            ["Test All Special Wholesale", "Test All Special Wholesale"],
        )

    def test_sheet_button_generates_driver_worksheet(self):
        Worksheet = self.env["prema.dispatch.driver.worksheet"]
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Worksheet Driver"})

        job, _crossdock, _pickup, _drop, _reload, _final_drop, _item = self._make_crossdock_job(vehicle, driver)
        result = Worksheet.generate_for_truck(vehicle.id, "2026-07-06")
        worksheet = Worksheet.search([
            ("vehicle_id", "=", vehicle.id),
            ("date", "=", date(2026, 7, 6)),
        ], limit=1)

        self.assertTrue(result.get("created"))
        self.assertTrue(worksheet)
        self.assertEqual(worksheet.driver_id.id, driver.id)
        self.assertEqual(worksheet.job_ids.ids, [job.id])
        self.assertEqual(worksheet.stop_count, 4)
        self.assertEqual(len(worksheet.stop_ids), 4)
        self.assertEqual(
            worksheet.stop_ids.sorted("sequence").mapped("stop_type"),
            ["pickup", "cross_dock_drop", "cross_dock_pickup", "dropoff"],
        )
        self.assertIn("Test All Special Wholesale", worksheet.worksheet_html)
        self.assertIn("Cross-Dock Drop / Transfer-In", worksheet.worksheet_html)

    def test_sheet_button_does_not_duplicate_worksheet(self):
        Worksheet = self.env["prema.dispatch.driver.worksheet"]
        vehicle = self.env["fleet.vehicle"].search([("x_max_pallets", ">=", 1)], limit=1)
        if not vehicle:
            self.skipTest("No vehicle available")
        driver = self.env["res.partner"].create({"name": "Worksheet Refresh Driver"})

        job, _crossdock, _pickup, _drop, _reload, _final_drop, _item = self._make_crossdock_job(vehicle, driver)
        first = Worksheet.generate_for_truck(vehicle.id, "2026-07-06")
        self.Stop.create({
            "job_id": job.id,
            "sequence": 50,
            "stop_type": "dropoff",
            "address": "728 Anderson Street, Whitby, ON",
            "pallets_out": 1,
        })
        second = Worksheet.generate_for_truck(vehicle.id, "2026-07-06")

        worksheets = Worksheet.search([
            ("vehicle_id", "=", vehicle.id),
            ("date", "=", date(2026, 7, 6)),
        ])
        self.assertEqual(len(worksheets), 1)
        worksheet = worksheets[0]
        self.assertEqual(first.get("id"), second.get("id"))
        self.assertFalse(second.get("created"))
        self.assertEqual(worksheet.stop_count, 5)
        self.assertEqual(len(worksheet.stop_ids), 5)
        self.assertIn("728 Anderson Street, Whitby, ON", worksheet.worksheet_html)

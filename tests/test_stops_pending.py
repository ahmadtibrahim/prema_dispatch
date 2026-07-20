"""
Stops Pending / driver-location workflow regression tests — covers the
Codex branch (Book Load wizard idempotency, capacity reservation,
Saved Location chain+store-number search/duplicate-detection, driver
stop-create authorization) plus the Claude follow-on work (physical
route-visit combining, future-pickup reservation + exact rehandle
plan, and the Load Plan loading-photo upload regression guard).
"""
import os

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestStopsPendingBase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.Item = self.env["prema.dispatch.item"]
        self.LP = self.env["prema.dispatch.load.plan"]
        self.Location = self.env["prema.dispatch.location"]
        self.Wizard = self.env["prema.dispatch.book.load.wizard"]
        self.stage_draft = self.env["prema.dispatch.stage"].search([("stage_type", "=", "draft")], limit=1)
        self.vehicle = self.env["fleet.vehicle"].search([], limit=1)
        self.customer = self.env["res.partner"].create({"name": "Stops Pending Test Customer"})
        self.driver_partner = self.env["res.partner"].create({"name": "SP Driver"})
        self.other_driver_partner = self.env["res.partner"].create({"name": "SP Other Driver"})
        self.move = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.customer.id,
        })
        self.pickup_location = self.Location.create({
            "name": "United Dairy and Grocers Inc.", "business_name": "United Dairy and Grocers Inc.",
            "chain_name": "United Dairy", "address": "145 Sun Pac Blvd, Brampton, ON L6S 5Z6",
            "street": "145 Sun Pac Blvd", "city": "Brampton", "province_code": "ON",
        })


class TestBookLoadWizardStopsPending(TestStopsPendingBase):
    def test_stops_pending_requires_planned_route_and_pickup_location(self):
        wizard = self.Wizard.create({
            "move_id": self.move.id, "partner_id": self.customer.id,
            "route_definition_mode": "stops_pending", "expected_skids": 10,
            "vehicle_id": self.vehicle.id,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_stops_pending_creates_one_pickup_stop_no_fake_pallets(self):
        wizard = self.Wizard.create({
            "move_id": self.move.id, "partner_id": self.customer.id,
            "route_definition_mode": "stops_pending", "expected_skids": 10,
            "scheduled_pickup": "2026-07-20 08:00:00", "planned_route_name": "Ottawa Route",
            "planned_route_corridor": "EAST", "pickup_saved_location_id": self.pickup_location.id,
            "vehicle_id": self.vehicle.id, "driver_id": self.driver_partner.id,
            "reserve_capacity": True,
        })
        wizard.action_confirm()
        job = self.move.dispatch_job_ids
        self.assertEqual(len(job), 1)
        self.assertEqual(job.stops_confirmation_state, "pending")
        self.assertEqual(job.planned_route_corridor, "EAST")
        self.assertEqual(len(job.stop_ids), 1, "Only the pickup stop should exist before route-sheet entry")
        self.assertEqual(job.stop_ids.stop_type, "pickup")
        self.assertFalse(job.item_ids, "No fake pallet items should be created merely to reserve capacity")
        link = self.env["prema.dispatch.load.plan.job"].search([("job_id", "=", job.id)])
        self.assertEqual(link.reserved_floor_positions, 10)

    def test_repeated_book_load_click_does_not_duplicate_job(self):
        wizard1 = self.Wizard.create({
            "move_id": self.move.id, "partner_id": self.customer.id,
            "route_definition_mode": "exact_stops", "vehicle_id": self.vehicle.id,
        })
        wizard1.action_confirm()
        self.assertEqual(len(self.move.dispatch_job_ids), 1)
        wizard2 = self.Wizard.create({
            "move_id": self.move.id, "partner_id": self.customer.id,
            "route_definition_mode": "exact_stops", "vehicle_id": self.vehicle.id,
        })
        wizard2.action_confirm()
        self.assertEqual(len(self.move.dispatch_job_ids), 1, "A second Book Load click must reuse the existing job, not duplicate it")


class TestCapacityReservation(TestStopsPendingBase):
    def _book_stops_pending(self, skids, route_name="Ottawa Route", corridor="EAST", pickup_loc=None):
        move = self.env["account.move"].create({"move_type": "out_invoice", "partner_id": self.customer.id})
        wizard = self.Wizard.create({
            "move_id": move.id, "partner_id": self.customer.id,
            "route_definition_mode": "stops_pending", "expected_skids": skids,
            "scheduled_pickup": "2026-07-20 04:00:00", "planned_route_name": route_name,
            "planned_route_corridor": corridor, "pickup_saved_location_id": pickup_loc.id if pickup_loc else self.pickup_location.id,
            "vehicle_id": self.vehicle.id, "driver_id": self.driver_partner.id, "reserve_capacity": True,
        })
        wizard.action_confirm()
        return move.dispatch_job_ids

    def test_two_jobs_reserve_without_double_counting(self):
        job_a = self._book_stops_pending(10)
        job_b = self._book_stops_pending(2)
        plan = self.LP.search([("vehicle_id", "=", self.vehicle.id), ("operating_date", "=", "2026-07-20")], limit=1)
        self.assertTrue(plan)
        self.assertEqual(len(plan.load_plan_job_ids.filtered("active")), 2)
        self.assertEqual(plan.reserved_pallet_count, 12)
        self.assertEqual(plan.committed_pallet_count, 12, "Reservation + confirmed items must not be double-counted (must be 12, not 24)")
        self.assertNotEqual(plan.layout_template_id.layout_type, "pin_wheel", "12 reserved skids on a 26ft template must not auto-propose Pin-Wheel")
        self.assertNotEqual(job_a.partner_id.id and job_b.partner_id.id, None)


class TestSavedLocationSearch(TestStopsPendingBase):
    def setUp(self):
        super().setUp()
        self.foodland = self.Location.create({
            "name": "Foodland #3290 – Picton", "business_name": "Foodland #3290 – Picton",
            "chain_name": "Foodland", "location_number": "3290",
            "address": "23 George Wright Blvd, Picton, ON", "city": "Picton",
        })
        self.metro = self.Location.create({
            "name": "Metro #153 – Picton", "business_name": "Metro #153 – Picton",
            "chain_name": "Metro", "location_number": "153",
            "address": "73 Main St, Picton, ON", "city": "Picton",
        })
        self.no_frills = self.Location.create({
            "name": "Joe's No Frills – Belleville", "business_name": "Joe's No Frills – Belleville",
            "chain_name": "No Frills", "address": "211 Bell Blvd, Belleville, ON", "city": "Belleville",
        })

    def test_search_by_chain_and_number_with_and_without_hash(self):
        for query in ("Foodland 3290", "Foodland #3290"):
            result = self.Location.driver_search_locations(query)
            ids = [r["id"] for r in result["results"]]
            self.assertIn(self.foodland.id, ids, f"query {query!r} should find Foodland #3290")

        result = self.Location.driver_search_locations("Metro 153")
        ids = [r["id"] for r in result["results"]]
        self.assertIn(self.metro.id, ids)

    def test_no_frills_search_without_invented_number(self):
        self.assertFalse(self.no_frills.location_number)
        result = self.Location.driver_search_locations("No Frills Belleville")
        ids = [r["id"] for r in result["results"]]
        self.assertIn(self.no_frills.id, ids)

    def test_duplicate_chain_and_number_rejected(self):
        with self.assertRaises(ValidationError):
            self.Location.create({
                "name": "Foodland 3290 dup", "business_name": "Foodland 3290 dup",
                "chain_name": "Foodland", "location_number": "3290", "address": "Somewhere else, ON",
            })


class TestDriverStopAndLocationAuthorization(TestStopsPendingBase):
    def setUp(self):
        super().setUp()
        self.job = self.Job.create({
            "partner_id": self.customer.id, "stage_id": self.stage_draft.id,
            "driver_id": self.driver_partner.id, "vehicle_id": self.vehicle.id,
            "route_definition_mode": "stops_pending", "stops_confirmation_state": "pending",
        })

    def test_wrong_driver_cannot_add_stop(self):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_add_stop
        driver_user = self.env["res.users"].create({
            "name": "Other Driver User", "login": "sp_other_driver@example.com",
            "partner_id": self.other_driver_partner.id,
            "groups_id": [(6, 0, [self.env.ref("prema_dispatch.group_dispatch_driver").id])],
        })
        with self.assertRaises(AccessError):
            check_driver_can_add_stop(self.env(user=driver_user), self.job)

    def test_assigned_driver_can_add_stop_and_partial_state(self):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_add_stop
        driver_user = self.env["res.users"].create({
            "name": "Assigned Driver User", "login": "sp_assigned_driver@example.com",
            "partner_id": self.driver_partner.id,
            "groups_id": [(6, 0, [self.env.ref("prema_dispatch.group_dispatch_driver").id])],
        })
        self.assertTrue(check_driver_can_add_stop(self.env(user=driver_user), self.job))
        stop = self.Stop.create({
            "job_id": self.job.id, "sequence": 10, "stop_type": "dropoff",
            "saved_location_id": self.pickup_location.id,
        })
        self.job.write({"stops_confirmation_state": "partial"})
        self.assertEqual(stop.address, self.pickup_location.address, "Creating a stop with saved_location_id must apply the location's address immediately")

    def test_assigned_driver_can_edit_confirmed_route_while_pickup_active(self):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_add_stop
        driver_user = self.env["res.users"].create({
            "name": "Confirmed Route Driver", "login": "sp_confirmed_driver@example.com",
            "partner_id": self.driver_partner.id,
            "groups_id": [(6, 0, [self.env.ref("prema_dispatch.group_dispatch_driver").id])],
        })
        pickup = self.Stop.create({
            "job_id": self.job.id, "sequence": 10, "stop_type": "pickup",
            "saved_location_id": self.pickup_location.id, "status": "arrived",
        })
        self.job.write({"stops_confirmation_state": "confirmed"})
        self.assertTrue(check_driver_can_add_stop(self.env(user=driver_user), self.job))
        pickup.write({"status": "completed", "actual_departure_time": fields.Datetime.now()})
        with self.assertRaises(AccessError):
            check_driver_can_add_stop(self.env(user=driver_user), self.job)

    def test_deleting_stop_reopens_confirmed_stops_pending_route_to_partial(self):
        pickup = self.Stop.create({
            "job_id": self.job.id, "sequence": 10, "stop_type": "pickup",
            "saved_location_id": self.pickup_location.id, "status": "arrived",
        })
        drop = self.Stop.create({
            "job_id": self.job.id, "sequence": 20, "stop_type": "dropoff",
            "saved_location_id": self.pickup_location.id, "status": "pending",
        })
        self.job.write({"stops_confirmation_state": "confirmed"})
        result = self.Job.driver_delete_stop(drop.id)
        self.assertTrue(result["success"])
        self.job.invalidate_recordset()
        self.assertEqual(self.job.stops_confirmation_state, "partial")
        self.assertTrue(pickup.exists())


class TestDriverDateAndPickupWorkflow(TestStopsPendingBase):
    def setUp(self):
        super().setUp()
        self.driver_user = self.env["res.users"].create({
            "name": "SP Driver User", "login": "sp_driver_user@example.com",
            "partner_id": self.driver_partner.id,
            "groups_id": [(6, 0, [self.env.ref("prema_dispatch.group_dispatch_driver").id])],
            "tz": "America/Toronto",
        })
        self.job = self.Job.create({
            "partner_id": self.customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": self.driver_partner.id,
            "vehicle_id": self.vehicle.id,
            "scheduled_pickup": "2026-07-20 08:00:00",
            "route_definition_mode": "stops_pending",
            "stops_confirmation_state": "pending",
            "approximate_skids": 10,
            "pickup_saved_location_id": self.pickup_location.id,
            "planned_route_name": "Ottawa Route",
            "planned_route_corridor": "EAST",
        })
        self.pickup_stop = self.Stop.create({
            "job_id": self.job.id,
            "sequence": 10,
            "stop_type": "pickup",
            "saved_location_id": self.pickup_location.id,
            "address": self.pickup_location.address,
            "pallets_in": 10,
            "status": "arrived",
        })
        self.plan = self.LP.browse(self.LP.create_load_plan(self.vehicle.id, "2026-07-20", driver_id=self.driver_partner.id)["id"])
        self.plan.add_job(self.job.id)

    def test_driver_dates_returns_three_day_window(self):
        dates = self.Job.with_user(self.driver_user).get_driver_available_dates()
        self.assertEqual(len(dates["days"]), 3)
        self.assertTrue(any(day["is_today"] for day in dates["days"]))

    def test_unconfirmed_pickup_defaults_actual_to_expected_not_zero(self):
        state = self.job._pickup_completion_step_state()
        self.assertFalse(state["actual_confirmed"])
        self.assertEqual(state["actual"], 10)
        self.assertEqual(state["actual_saved"], 0)
        summary = self.job._driver_job_summary()
        self.assertFalse(summary["pickup_actuals_confirmed"])
        self.assertEqual(summary["actual_received_pallet_count"], 0)

    def test_confirm_actual_pickup_is_idempotent(self):
        result = self.Job.with_user(self.driver_user).driver_confirm_pickup_actuals(self.pickup_stop.id, {
            "actual_received_pallet_count": 10,
            "route_sheet_received": True,
        })
        self.assertTrue(result["success"])
        self.job.invalidate_recordset()
        self.assertTrue(self.job.pickup_actuals_confirmed_at)
        self.assertEqual(result["actual_received_pallet_count"], 10)
        self.assertEqual(result["layout_type"], "straight")
        self.assertEqual(len(self.job.item_ids.filtered(lambda item: item.status != "cancelled" and not item.pending_future_pickup)), 10)
        self.Job.with_user(self.driver_user).driver_confirm_pickup_actuals(self.pickup_stop.id, {
            "actual_received_pallet_count": 10,
        })
        self.assertEqual(len(self.job.item_ids.filtered(lambda item: item.status != "cancelled" and not item.pending_future_pickup)), 10)

    def test_confirm_actual_pickup_blocks_impossible_layout(self):
        with self.assertRaises(UserError):
            self.Job.with_user(self.driver_user).driver_confirm_pickup_actuals(self.pickup_stop.id, {
                "actual_received_pallet_count": 15,
            })

    def test_finalize_pickup_marks_confirmed_and_reserves_future_pickup(self):
        terra_customer = self.env["res.partner"].create({"name": "Terra Financial"})
        terra_job = self.Job.create({
            "partner_id": terra_customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": self.driver_partner.id,
            "vehicle_id": self.vehicle.id,
            "scheduled_pickup": "2026-07-20 09:00:00",
            "route_definition_mode": "exact_stops",
            "stops_confirmation_state": "confirmed",
            "approximate_skids": 2,
        })
        terra_pickup = self.Stop.create({
            "job_id": terra_job.id, "sequence": 10, "stop_type": "pickup",
            "address": "1 Royal Gate Blvd Unit F, Woodbridge, ON L4L 8Z7",
        })
        self.Stop.create({
            "job_id": terra_job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "290 N Front St, Belleville, ON",
        })
        self.plan.add_job(terra_job.id)
        self.env["prema.dispatch.load.plan.job"].search([
            ("load_plan_id", "=", self.plan.id), ("job_id", "=", terra_job.id),
        ]).write({"reserved_floor_positions": 2})
        self.Stop.create({
            "job_id": self.job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "740 Division St, Cobourg, ON",
        })
        self.Stop.create({
            "job_id": self.job.id, "sequence": 30, "stop_type": "dropoff",
            "address": "871 Chemong Rd, Peterborough, ON",
        })
        self.Job.with_user(self.driver_user).driver_confirm_pickup_actuals(self.pickup_stop.id, {
            "actual_received_pallet_count": 10,
            "route_sheet_received": True,
        })
        result = self.Job.with_user(self.driver_user).driver_finalize_pickup_intake(self.pickup_stop.id, {})
        self.assertTrue(result["success"])
        self.job.invalidate_recordset()
        self.assertEqual(self.job.stops_confirmation_state, "confirmed")
        reservations = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.plan.id),
            ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"),
            ("active", "=", True),
            ("related_pickup_stop_id", "=", terra_pickup.id),
        ])
        self.assertEqual(len(reservations), 2)

    def test_finalize_pickup_without_actual_count_keeps_unconfirmed_state(self):
        self.Stop.create({
            "job_id": self.job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "740 Division St, Cobourg, ON",
        })
        result = self.Job.with_user(self.driver_user).driver_finalize_pickup_intake(self.pickup_stop.id, {
            "stops_confirmation_state": "partial",
        })
        self.assertTrue(result["success"])
        self.job.invalidate_recordset()
        self.assertFalse(self.job.pickup_actuals_confirmed_at)
        self.assertEqual(self.job.actual_received_pallet_count, 0)

    def test_combined_route_excludes_planning_only_and_keeps_terra_pickup_before_deliveries(self):
        terra_customer = self.env["res.partner"].create({"name": "Terra Route Customer"})
        terra_job = self.Job.create({
            "partner_id": terra_customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": self.driver_partner.id,
            "vehicle_id": self.vehicle.id,
            "scheduled_pickup": "2026-07-20 09:00:00",
            "route_definition_mode": "exact_stops",
            "stops_confirmation_state": "confirmed",
        })
        terra_pickup = self.Stop.create({
            "job_id": terra_job.id, "sequence": 10, "stop_type": "pickup",
            "address": "Terra pickup",
        })
        self.Stop.create({
            "job_id": terra_job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "Belleville",
        })
        self.Stop.create({
            "job_id": self.job.id, "sequence": 20, "stop_type": "dropoff",
            "address": "Ottawa, Ontario", "planning_only": True,
        })
        united_delivery = self.Stop.create({
            "job_id": self.job.id, "sequence": 30, "stop_type": "dropoff",
            "address": "Cobourg",
        })
        ordered = self.Job.combined_vehicle_day_stops(self.job | terra_job, fields.Date.to_date("2026-07-20"))
        self.assertNotIn("Ottawa, Ontario", ordered.mapped("address"))
        ordered_ids = ordered.ids
        self.assertLess(ordered_ids.index(terra_pickup.id), ordered_ids.index(united_delivery.id))


class TestRouteVisitCombine(TestStopsPendingBase):
    def test_combine_shared_delivery_creates_one_visit_two_independent_stops(self):
        shared_loc = self.Location.create({
            "name": "Healthy Planet – Belleville", "business_name": "Healthy Planet – Belleville",
            "address": "290 N Front St, Belleville, ON",
        })
        job_a = self.Job.create({"partner_id": self.customer.id, "stage_id": self.stage_draft.id, "vehicle_id": self.vehicle.id})
        job_b_customer = self.env["res.partner"].create({"name": "Other Financial Customer"})
        job_b = self.Job.create({"partner_id": job_b_customer.id, "stage_id": self.stage_draft.id, "vehicle_id": self.vehicle.id})
        stop_a = self.Stop.create({"job_id": job_a.id, "sequence": 10, "stop_type": "dropoff", "saved_location_id": shared_loc.id})
        stop_b = self.Stop.create({"job_id": job_b.id, "sequence": 10, "stop_type": "dropoff", "saved_location_id": shared_loc.id})

        plan_data = self.LP.create_load_plan(self.vehicle.id, "2026-07-20")
        plan = self.LP.browse(plan_data["id"])
        plan.add_job(job_a.id)
        plan.add_job(job_b.id)

        candidates = plan.find_shared_visit_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(set(candidates[0]["stop_ids"]), {stop_a.id, stop_b.id})

        result = plan.combine_physical_visit([stop_a.id, stop_b.id])
        self.assertTrue(result["success"])
        visit = self.env["prema.dispatch.route.visit"].browse(result["route_visit_id"])
        self.assertEqual(len(visit.stop_link_ids), 2)
        self.assertEqual(set(visit.stop_link_ids.mapped("job_id.id")), {job_a.id, job_b.id}, "Underlying jobs must remain separate")

        # Completing one stop's delivery must not affect the other's completion state.
        link_a = visit.stop_link_ids.filtered(lambda l: l.stop_id.id == stop_a.id)
        link_a.write({"completion_state": "completed"})
        link_b = visit.stop_link_ids.filtered(lambda l: l.stop_id.id == stop_b.id)
        self.assertEqual(link_b.completion_state, "pending")

    def test_combine_rejects_stops_from_different_addresses(self):
        loc1 = self.Location.create({"name": "Loc 1", "address": "1 Main St"})
        loc2 = self.Location.create({"name": "Loc 2", "address": "2 Main St"})
        job = self.Job.create({"partner_id": self.customer.id, "stage_id": self.stage_draft.id, "vehicle_id": self.vehicle.id})
        s1 = self.Stop.create({"job_id": job.id, "sequence": 10, "stop_type": "dropoff", "saved_location_id": loc1.id})
        s2 = self.Stop.create({"job_id": job.id, "sequence": 20, "stop_type": "dropoff", "saved_location_id": loc2.id})
        plan_data = self.LP.create_load_plan(self.vehicle.id, "2026-07-21")
        plan = self.LP.browse(plan_data["id"])
        plan.add_job(job.id)
        with self.assertRaises(UserError):
            plan.combine_physical_visit([s1.id, s2.id])


class TestFuturePickupAndRehandle(TestStopsPendingBase):
    def setUp(self):
        super().setUp()
        self.job_united = self.Job.create({"partner_id": self.customer.id, "stage_id": self.stage_draft.id, "vehicle_id": self.vehicle.id})
        self.terra_customer = self.env["res.partner"].create({"name": "Terra Freska Financial Customer"})
        self.job_terra = self.Job.create({"partner_id": self.terra_customer.id, "stage_id": self.stage_draft.id, "vehicle_id": self.vehicle.id})
        self.terra_pickup_stop = self.Stop.create({
            "job_id": self.job_terra.id, "sequence": 10, "stop_type": "pickup", "address": "Terra Freska pickup",
        })
        plan_data = self.LP.create_load_plan(self.vehicle.id, "2026-07-20")
        self.plan = self.LP.browse(plan_data["id"])
        self.plan.add_job(self.job_united.id)
        self.plan.add_job(self.job_terra.id)

    def test_reserve_future_positions_picks_accessible_vacant_slots(self):
        result = self.plan.reserve_future_positions(self.job_terra.id, 2, self.plan.version)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["reserved_position_ids"]), 2)

    def test_no_rehandle_when_reserved_positions_stay_vacant(self):
        self.plan.reserve_future_positions(self.job_terra.id, 2, self.plan.version)
        plan_result = self.plan.get_future_pickup_plan(self.job_terra.id)
        self.assertFalse(plan_result["rehandle_required"])
        self.assertEqual(plan_result["message"], "NO TEMPORARY UNLOADING REQUIRED")

    def test_exact_rehandle_steps_when_reserved_position_becomes_blocked(self):
        self.plan.invalidate_recordset()
        reserve_result = self.plan.reserve_future_positions(self.job_terra.id, 1, self.plan.version)
        reserved_position_id = reserve_result["reserved_position_ids"][0]
        blocker = self.Item.create({
            "job_id": self.job_united.id, "name": "U-Blocker", "load_plan_id": self.plan.id,
        })
        self.plan.invalidate_recordset()
        self.plan.assign_pallet_to_position(blocker.id, reserved_position_id, self.plan.version)
        self.plan.invalidate_recordset()

        plan_result = self.plan.get_future_pickup_plan(self.job_terra.id)
        self.assertTrue(plan_result["rehandle_required"])
        actions = [s["action"] for s in plan_result["steps"]]
        self.assertEqual(actions, ["temporary_unload", "load_future_pickup", "reload"])
        self.assertEqual(plan_result["steps"][0]["item_id"], blocker.id)
        self.assertEqual(plan_result["steps"][2]["item_id"], blocker.id)

    def test_confirm_future_pickup_loads_item_into_reserved_position(self):
        self.plan.invalidate_recordset()
        reserve_result = self.plan.reserve_future_positions(self.job_terra.id, 1, self.plan.version)
        op_id = reserve_result["operation_ids"][0]
        tf_item = self.Item.create({
            "job_id": self.job_terra.id, "name": "TF-01", "load_plan_id": self.plan.id,
            "available_after_stop_id": self.terra_pickup_stop.id,
        })
        self.plan.invalidate_recordset()
        self.assertTrue(tf_item.pending_future_pickup, "TF item must not appear onboard before the Terra pickup stop is completed")
        self.assertEqual(self.plan.confirmed_pallet_count, 0)

        self.terra_pickup_stop.write({"actual_departure_time": "2026-07-20 05:45:00"})
        tf_item.invalidate_recordset()
        self.assertFalse(tf_item.pending_future_pickup)

        self.plan.confirm_future_pickup_operation(op_id, tf_item.id, self.plan.version)
        self.plan.invalidate_recordset()
        self.assertEqual(tf_item.status, "loaded")
        self.assertTrue(tf_item.position_id)


class TestLocationExtractionService(TransactionCase):
    def setUp(self):
        super().setUp()
        from odoo.addons.prema_dispatch.services.location_extraction_service import LocationExtractionService
        self.service = LocationExtractionService(self.env)

    def test_validate_payload_rejects_wrong_context(self):
        with self.assertRaises(UserError):
            self.service.validate_payload({"success": True, "extraction_context": "pickup_from"}, "ship_to")

    def test_validate_payload_rejects_unknown_keys(self):
        with self.assertRaises(UserError):
            self.service.validate_payload({"success": True, "extraction_context": "ship_to", "hacked_field": "x"}, "ship_to")

    def test_normalize_store_pattern_extracts_chain_and_number(self):
        data = self.service.normalize_store_pattern({"business_name": "FOODLAND STORE #3290", "location_number": ""})
        self.assertEqual(data["chain_name"], "Foodland")
        self.assertEqual(data["location_number"], "3290")

    def test_normalize_store_pattern_does_not_override_explicit_number(self):
        data = self.service.normalize_store_pattern({"business_name": "METRO #153", "location_number": "999"})
        self.assertEqual(data["location_number"], "999", "An already-extracted explicit number must not be overwritten")


class TestLoadPlanPhotoUploadRegression(TransactionCase):
    def test_driver_app_js_no_longer_uses_stop_id_zero_for_loading_photo(self):
        """Regression guard for the specific bug called out in the production
        safety review: the Load Plan loading-photo button must never call
        openScanner(0, "pod") — it must go through the Load Plan document
        upload flow with document_type = loading_photo."""
        js_path = os.path.join(os.path.dirname(__file__), "..", "static", "src", "js", "driver_app.js")
        with open(js_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("openScanner(0,'pod')", content.replace('"', "'"))
        self.assertIn("loadPlanId:S.loadPlan.id", content)
        self.assertIn("documentType:'loading_photo'".replace("'", "'"), content.replace('"', "'"))
        self.assertIn("/dispatch/driver/loadplan/document/upload", content)

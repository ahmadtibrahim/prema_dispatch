"""
Phase 2-6 — Load Plan backend regression tests: model/capacity,
shared-skid counting, position uniqueness, optimistic concurrency,
stale-plan detection, locking/snapshot, transfer handoff, warehouse
permissions, and the public QR summary's data minimization.
"""
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from .mock_google_apis import install_google_mocks


class TestLoadPlanBase(TransactionCase):
    def setUp(self):
        super().setUp()
        install_google_mocks(self)
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.Item = self.env["prema.dispatch.item"]
        self.LP = self.env["prema.dispatch.load.plan"]
        self.stage_draft = self.env["prema.dispatch.stage"].search([("stage_type", "=", "draft")], limit=1)
        self.stage_picked_up = self.env["prema.dispatch.stage"].search([("code", "=", "picked_up")], limit=1)
        self.vehicle = self.env["fleet.vehicle"].search([], limit=1)
        self.customer = self.env["res.partner"].create({"name": "LoadPlan Test Customer"})
        self.driver_a_partner = self.env["res.partner"].create({"name": "LP Driver A"})
        self.driver_b_partner = self.env["res.partner"].create({"name": "LP Driver B"})
        self.vehicle.write({
            "default_pallet_layout": "straight",
            "straight_pallet_capacity": 12,
            "pin_wheel_pallet_capacity": 13,
            "turned_pallet_capacity": 14,
            "layout_configuration_verified": False,
        })
        self.job = self.Job.create({
            "partner_id": self.customer.id, "stage_id": self.stage_draft.id,
            "driver_id": self.driver_a_partner.id, "vehicle_id": self.vehicle.id,
        })
        self.stop1 = self.Stop.create({"job_id": self.job.id, "sequence": 10, "stop_type": "dropoff", "address": "Stop 1"})
        self.stop2 = self.Stop.create({"job_id": self.job.id, "sequence": 20, "stop_type": "dropoff", "address": "Stop 2"})
        self.stop3 = self.Stop.create({"job_id": self.job.id, "sequence": 30, "stop_type": "dropoff", "address": "Stop 3"})

        self.driver_a_user = self._make_user("lp_driver_a@example.com", self.driver_a_partner,
                                              "base.group_user", "prema_dispatch.group_dispatch_driver")
        self.driver_b_user = self._make_user("lp_driver_b@example.com", self.driver_b_partner,
                                              "base.group_user", "prema_dispatch.group_dispatch_driver")
        self.dispatcher_user = self._make_user("lp_dispatcher@example.com",
                                                self.env["res.partner"].create({"name": "LP Dispatcher"}),
                                                "base.group_user", "prema_dispatch.group_dispatcher")
        self.manager_user = self._make_user("lp_manager@example.com",
                                             self.env["res.partner"].create({"name": "LP Manager"}),
                                             "base.group_user", "prema_dispatch.group_dispatch_manager")
        self.warehouse_user = self._make_user("lp_warehouse@example.com",
                                               self.env["res.partner"].create({"name": "LP Warehouse"}),
                                               "base.group_user", "prema_dispatch.group_dispatch_warehouse")

    def _make_user(self, login, partner, *groups):
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": partner.name, "login": login, "partner_id": partner.id,
            "groups_id": [(6, 0, [self.env.ref(g).id for g in groups])],
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
        })

    def _make_plan(self, as_user=None):
        env = self.env if not as_user else self.env(user=as_user)
        return env["prema.dispatch.load.plan"].create_load_plan(
            self.vehicle.id, "2026-08-15", driver_id=self.driver_a_partner.id,
        )


class TestLoadPlanModel(TestLoadPlanBase):

    def test_01_create_defaults_to_straight(self):
        data = self._make_plan()
        self.assertEqual(data["layout_template"]["layout_type"], "straight")
        self.assertEqual(data["counts"]["max_positions"], 12)

    def test_02_capacity_escalation_to_pinwheel(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        items = self.Item
        for i in range(12):
            items |= self.Item.create({"job_id": self.job.id, "name": f"P{i}", "load_plan_id": plan.id})
        plan.invalidate_recordset()
        self.assertEqual(plan.layout_template_id.layout_type, "straight")
        # Assign all 12 to real Straight positions before the 13th pallet
        # arrives, so the Pin-Wheel escalation has real assignments to preserve.
        for item, pos in zip(items, plan.layout_template_id.position_ids):
            plan.assign_pallet_to_position(item.id, pos.id, plan.version)
            plan.invalidate_recordset()
        # 13th pallet triggers evaluate_layout_for_capacity() via create()'s hook
        self.Item.create({"job_id": self.job.id, "name": "P13", "load_plan_id": plan.id})
        plan.invalidate_recordset()
        proposal = plan.evaluate_layout_for_capacity()
        self.assertIsNotNone(proposal, "13 confirmed pallets on a 12-position Straight template must produce a proposal")
        self.assertTrue(proposal.get("requires_confirmation"), "must never apply the layout change without confirmation")
        plan.invalidate_recordset()
        self.assertEqual(plan.layout_template_id.layout_type, "straight", "layout must not change until confirmed")
        # Confirm the escalation explicitly — existing assignments preserved
        pinwheel = self.env.ref("prema_dispatch.layout_tpl_pinwheel_26ft")
        result = plan.change_layout(pinwheel.id, plan.version, confirm_remap=True)
        self.assertIn("Preserved 12", result["layout_change_summary"])
        plan.invalidate_recordset()
        self.assertEqual(plan.layout_template_id.layout_type, "pin_wheel")
        preserved = sum(1 for i in items if i.position_id)
        self.assertEqual(preserved, 12, "all 12 originally-assigned pallets must map onto the same position codes in Pin-Wheel")

    def test_03_shared_skid_counts_as_one_pallet_three_allocations(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.add_job(self.job.id)  # allocations must stay within plan-linked jobs
        item = self.Item.create({"job_id": self.job.id, "name": "Shared Skid", "load_plan_id": plan.id})
        plan.assign_stops_to_pallet(item.id, [
            {"stop_id": self.stop1.id}, {"stop_id": self.stop2.id}, {"stop_id": self.stop3.id},
        ], plan.version)
        plan.invalidate_recordset(); item.invalidate_recordset()
        self.assertTrue(item.shared_skid)
        self.assertEqual(len(item.stop_allocation_ids.filtered("active")), 3)
        self.assertEqual(plan.confirmed_pallet_count, 1, "one physical pallet, not three")

    def test_03b_five_stop_max_allowed_sixth_rejected(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.add_job(self.job.id)
        stop4 = self.Stop.create({"job_id": self.job.id, "sequence": 40, "stop_type": "dropoff", "address": "Stop 4"})
        stop5 = self.Stop.create({"job_id": self.job.id, "sequence": 50, "stop_type": "dropoff", "address": "Stop 5"})
        stop6 = self.Stop.create({"job_id": self.job.id, "sequence": 60, "stop_type": "dropoff", "address": "Stop 6"})
        item = self.Item.create({"job_id": self.job.id, "name": "U-03", "load_plan_id": plan.id})
        plan.assign_stops_to_pallet(item.id, [
            {"stop_id": self.stop1.id}, {"stop_id": self.stop2.id}, {"stop_id": self.stop3.id},
            {"stop_id": stop4.id}, {"stop_id": stop5.id},
        ], plan.version)
        plan.invalidate_recordset(); item.invalidate_recordset()
        self.assertEqual(len(item.stop_allocation_ids.filtered("active")), 5)
        with self.assertRaises(UserError):
            plan.assign_stops_to_pallet(item.id, [
                {"stop_id": self.stop1.id}, {"stop_id": self.stop2.id}, {"stop_id": self.stop3.id},
                {"stop_id": stop4.id}, {"stop_id": stop5.id}, {"stop_id": stop6.id},
            ], plan.version)

    def test_03c_partial_unload_keeps_position_until_last_allocation(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.add_job(self.job.id)
        item = self.Item.create({"job_id": self.job.id, "name": "U-06", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id, plan.version)
        plan.invalidate_recordset()
        plan.assign_stops_to_pallet(item.id, [
            {"stop_id": self.stop1.id}, {"stop_id": self.stop2.id},
        ], plan.version)
        item.write({"status": "loaded"})
        self.stop1.action_mark_completed()
        item.invalidate_recordset()
        self.assertEqual(item.status, "partially_unloaded")
        self.assertEqual(item.position_id.id, pos.id)
        self.stop2.action_mark_completed()
        item.invalidate_recordset()
        self.assertEqual(item.status, "delivered")

    def test_04_position_uniqueness_blocks_double_assignment(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item_a = self.Item.create({"job_id": self.job.id, "name": "A", "load_plan_id": plan.id})
        item_b = self.Item.create({"job_id": self.job.id, "name": "B", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item_a.id, pos.id, plan.version)
        plan.invalidate_recordset()
        with self.assertRaises(UserError):
            plan.assign_pallet_to_position(item_b.id, pos.id, plan.version)

    def test_05_optimistic_concurrency_conflict(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        stale_version = plan.version
        plan.assign_pallet_to_position(item.id, pos.id, stale_version)  # version now bumped
        plan.invalidate_recordset()
        with self.assertRaises(UserError):
            plan.assign_pallet_to_position(item.id, pos.id, stale_version)  # reusing the OLD version

    def test_06_stale_plan_preserves_assignments(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id, plan.version)
        plan.invalidate_recordset()
        # Item creation itself already marks the plan stale ("pallet added"
        # is an explicit trigger) — clear that first so this test isolates
        # the add_job() trigger specifically, per its own name/purpose.
        plan.clear_stale(plan.version)
        plan.invalidate_recordset()
        self.assertFalse(plan.is_stale)
        other_job = self.Job.create({"partner_id": self.customer.id, "stage_id": self.stage_draft.id})
        plan.add_job(other_job.id)
        plan.invalidate_recordset(); item.invalidate_recordset()
        self.assertTrue(plan.is_stale)
        self.assertEqual(item.position_id.id, pos.id, "stale marking must never move an existing assignment")

    def test_07_lock_creates_snapshot_and_blocks_edits(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id, plan.version)
        plan.invalidate_recordset()
        plan.lock_load_plan(reason="test lock")
        plan.invalidate_recordset()
        self.assertTrue(plan.is_locked)
        self.assertTrue(plan.final_snapshot_json)
        with self.assertRaises(AccessError):
            plan.with_user(self.driver_a_user).mark_pallet_loaded(item.id, plan.version)

    def test_08_manager_unlock_requires_reason(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.lock_load_plan()
        plan.invalidate_recordset()
        with self.assertRaises(UserError):
            plan.with_user(self.manager_user).unlock_load_plan("")
        plan.with_user(self.manager_user).unlock_load_plan("dispatcher requested changes")
        plan.invalidate_recordset()
        self.assertFalse(plan.is_locked)

    def test_09_transfer_handoff_preserves_identity_and_history(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "Transfer Me", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id, plan.version)
        plan.invalidate_recordset()
        other_vehicle = self.env["fleet.vehicle"].search([("id", "!=", self.vehicle.id)], limit=1)
        if not other_vehicle:
            self.skipTest("Need a second vehicle for handoff test")
        original_job_id = item.job_id.id
        result = plan.execute_handoff(item.id, other_vehicle.id, to_operating_date="2026-08-15")
        item.invalidate_recordset()
        self.assertEqual(item.job_id.id, original_job_id, "financial job must never change on handoff")
        self.assertNotEqual(item.load_plan_id.id, plan.id)
        self.assertFalse(item.position_id, "receiving plan must not blindly copy the old position")
        giving_events = plan.event_ids.filtered(lambda e: e.event_type == "pallet_handed_off")
        receiving_events = result["to_load_plan"]
        self.assertTrue(giving_events)

    def test_10_warehouse_can_mark_loaded_not_add_job(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        pos = plan.layout_template_id.position_ids[0]
        plan.assign_pallet_to_position(item.id, pos.id, plan.version)
        plan.invalidate_recordset()
        plan.with_user(self.warehouse_user).mark_pallet_loaded(item.id, plan.version)
        plan.invalidate_recordset(); item.invalidate_recordset()
        self.assertEqual(item.status, "loaded")
        other_job = self.Job.create({"partner_id": self.customer.id, "stage_id": self.stage_draft.id})
        with self.assertRaises(AccessError):
            plan.with_user(self.warehouse_user).add_job(other_job.id)

    def test_11_warehouse_payload_strips_customer_and_invoice(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.add_job(self.job.id)
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        plan.assign_stops_to_pallet(item.id, [{"stop_id": self.stop1.id, "invoice_id": False}], plan.version)
        wh_payload = plan.with_user(self.warehouse_user).get_load_plan_for_warehouse()
        for pos in wh_payload["positions"]:
            if pos["item"]:
                for stop in pos["item"]["stops"]:
                    self.assertNotIn("customer", stop)
                    self.assertNotIn("invoice_id", stop)
        for job in wh_payload["jobs"]:
            self.assertNotIn("customer", job)
            self.assertNotIn("job_name", job)

    def test_12_driver_isolated_from_other_drivers_load_plan(self):
        data = self._make_plan()  # driver_a's plan
        with self.assertRaises(AccessError):
            self.LP.with_user(self.driver_b_user).browse(data["id"]).get_load_plan()

    def test_13_public_qr_summary_whitelist_only(self):
        item = self.Item.create({"job_id": self.job.id, "name": "QR Item"})
        result = self.Item.get_public_pallet_summary(item.qr_token)
        self.assertTrue(result["success"])
        allowed_keys = {"success", "reference", "truck", "position", "stop_numbers", "shared_skid", "exception_state", "status"}
        self.assertTrue(set(result.keys()).issubset(allowed_keys))
        self.assertNotIn("rate", result); self.assertNotIn("invoice", str(result).lower())

    def test_14_public_qr_unknown_token(self):
        result = self.Item.get_public_pallet_summary("nonexistent-token")
        self.assertFalse(result["success"])

    def test_15_recommendation_is_advisory_only(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        item = self.Item.create({"job_id": self.job.id, "name": "X", "load_plan_id": plan.id})
        rec = plan.recommend_layout()
        plan.invalidate_recordset(); item.invalidate_recordset()
        self.assertFalse(item.position_id, "a recommendation must never be silently applied")
        self.assertIn("positions", rec)

    # ── Unverified-layout safety gate (production deployment addendum) ──

    def test_16_confirm_loading_blocked_until_unverified_ack(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        # The straight template ships is_verified=False, but on a
        # production-clone DB a manager may already have verified it —
        # force the unverified state the test is about.
        plan.layout_template_id.write({"is_verified": False, "verified_by": False})
        plan.invalidate_recordset()
        self.assertFalse(plan.layout_template_id.is_verified)
        with self.assertRaises(UserError) as cm:
            plan.confirm_loading(plan.version)
        self.assertIn("UNVERIFIED VEHICLE LAYOUT", str(cm.exception))

    def test_17_dispatcher_ack_unblocks_confirm_loading(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.with_user(self.dispatcher_user).acknowledge_unverified_layout("reviewed with driver")
        plan.invalidate_recordset()
        self.assertTrue(plan.unverified_layout_acknowledged)
        self.assertEqual(plan.unverified_layout_acknowledged_by.id, self.dispatcher_user.id)
        result = plan.confirm_loading(plan.version)  # must not raise now
        self.assertEqual(result["state"], "loaded")

    def test_18_driver_cannot_acknowledge_unverified_layout(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        with self.assertRaises(AccessError):
            plan.with_user(self.driver_a_user).acknowledge_unverified_layout()

    def test_19_layout_change_resets_acknowledgment(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.with_user(self.dispatcher_user).acknowledge_unverified_layout()
        plan.invalidate_recordset()
        self.assertTrue(plan.unverified_layout_acknowledged)
        pinwheel = self.env.ref("prema_dispatch.layout_tpl_pinwheel_26ft")
        plan.change_layout(pinwheel.id, plan.version, confirm_remap=True)
        plan.invalidate_recordset()
        self.assertFalse(plan.unverified_layout_acknowledged, "switching templates must require a fresh acknowledgement")
        with self.assertRaises(UserError):
            plan.confirm_loading(plan.version)

    def test_20_turned_is_proposed_for_fourteen(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        items = self.Item
        for i in range(12):
            items |= self.Item.create({"job_id": self.job.id, "name": f"T{i}", "load_plan_id": plan.id})
        for item, pos in zip(items, plan.layout_template_id.position_ids):
            plan.assign_pallet_to_position(item.id, pos.id, plan.version)
            plan.invalidate_recordset()
        for i in range(2):  # 14 total pallets should propose Turned
            self.Item.create({"job_id": self.job.id, "name": f"Extra{i}", "load_plan_id": plan.id})
        plan.invalidate_recordset()
        proposal = plan.evaluate_layout_for_capacity()
        self.assertTrue(proposal and proposal.get("requires_confirmation"))
        self.assertIn("turned", (proposal.get("notification") or "").lower())
        plan.invalidate_recordset()
        self.assertEqual(plan.layout_template_id.layout_type, "straight")

    def test_20b_more_than_fourteen_blocks(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        for i in range(15):
            self.Item.create({"job_id": self.job.id, "name": f"B{i}", "load_plan_id": plan.id})
        plan.invalidate_recordset()
        proposal = plan.evaluate_layout_for_capacity()
        self.assertTrue(proposal and proposal.get("no_valid_layout"))

    def test_21_acknowledgment_logged_in_event_timeline(self):
        data = self._make_plan()
        plan = self.LP.browse(data["id"])
        plan.with_user(self.dispatcher_user).acknowledge_unverified_layout("test reason")
        events = plan.event_ids.filtered(lambda e: e.event_type == "unverified_layout_acknowledged")
        self.assertTrue(events)
        self.assertEqual(events[0].reason, "test reason")
        self.assertEqual(events[0].changed_by.id, self.dispatcher_user.id)

    def test_22_vehicle_layout_verification_sets_vehicle_flag(self):
        self.vehicle.with_user(self.manager_user).action_verify_pallet_layout_configuration()
        self.vehicle.invalidate_recordset()
        self.assertTrue(self.vehicle.layout_configuration_verified)
        self.assertEqual(self.vehicle.layout_verified_by.id, self.manager_user.id)

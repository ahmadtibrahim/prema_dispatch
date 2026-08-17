"""Load Plan population, multi-customer aggregate + rehandling
(Manual-UAT part 5).

Fixes the zero-item load plan bug: adding a job to a plan attaches its
floor items (one item per physical pallet) immediately. ONE plan holds
every customer's jobs on the vehicle/date — each item keeps its own
job/customer ownership. Movement-bridge items carry the stable pallet
labels (U-01, TF-01…). Temporary unload / reload operations leave a
clean event trail.

Runs in the prema_logistics_booking test phase (both modules loaded).
"""
from odoo.tests.common import TransactionCase


class TestLoadPlanPopulation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["prema.dispatch.job"]
        cls.Stop = cls.env["prema.dispatch.stop"]
        cls.Item = cls.env["prema.dispatch.item"]
        cls.LP = cls.env["prema.dispatch.load.plan"]
        cls.stage_draft = cls.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1)
        cls.vehicle = cls.env["fleet.vehicle"].search([], limit=1)
        cls.customer_a = cls.env["res.partner"].create(
            {"name": "LP Pop Customer A"})
        cls.customer_b = cls.env["res.partner"].create(
            {"name": "LP Pop Customer B"})

    def _make_job(self, partner, items):
        job = self.Job.create({
            "partner_id": partner.id,
            "stage_id": self.stage_draft.id,
            "vehicle_id": self.vehicle.id,
        })
        for i, name in enumerate(items):
            self.Item.create({
                "job_id": job.id, "name": name, "sequence": (i + 1) * 10,
                "load_unit_type": "pallet",
            })
        return job

    def _make_plan(self):
        data = self.LP.create_load_plan(
            self.vehicle.id, "2026-08-18", driver_id=False)
        return self.LP.browse(data["id"])

    # ── Population (zero-item fix) ────────────────────────────────

    def test_01_add_job_populates_plan_items(self):
        """The zero-item bug: adding a job must attach its floor items to
        the plan immediately — a plan with a job link but no pallets is
        useless to the dispatcher."""
        job = self._make_job(self.customer_a, ["U-01", "U-02", "U-03"])
        self.assertFalse(job.item_ids.mapped("load_plan_id"))
        plan = self._make_plan()
        plan.add_job(job.id)
        self.assertEqual(len(plan.pallet_ids), 3)
        self.assertEqual(
            set(plan.pallet_ids.mapped("name")), {"U-01", "U-02", "U-03"})

    def test_02_multi_customer_single_plan_keeps_ownership(self):
        """ONE plan across customers: both jobs ride the same truck/date
        plan; every item keeps its own job (and thereby its customer) —
        the plan is a physical aggregate, never a financial merge."""
        job_a = self._make_job(self.customer_a, ["U-01", "U-02"])
        job_b = self._make_job(self.customer_b, ["V-01"])
        plan = self._make_plan()
        plan.add_job(job_a.id)
        plan.add_job(job_b.id)
        self.assertEqual(len(plan.load_plan_job_ids), 2)
        self.assertEqual(len(plan.pallet_ids), 3)
        by_item = {i.name: i for i in plan.pallet_ids}
        self.assertEqual(by_item["U-01"].job_id.partner_id, self.customer_a)
        self.assertEqual(by_item["V-01"].job_id.partner_id, self.customer_b)

    def test_03_movement_bridge_stable_labels_on_plan(self):
        """Movement-v1 items carry the stable pallet labels (U-01/TF-01)
        and join the vehicle's plan via add_job — the trace
        booking pallet → dispatch item → load plan holds end to end."""
        partner = self.env["res.partner"].search([], limit=1)
        booking = self.env["logistics.booking"].create({
            "partner_id": partner.id,
            "booking_number": "LP-POP-03",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 2, "physical_pallets": 2, "weight_lbs": 1000.0,
            "state": "confirmed",
            "calculated_price": 300.0,
            "route_model_version": "movement_v1",
        })
        ud = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
            "location_name": "United Dairy"})
        blv = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
            "location_name": "Belleville"})
        for label, seq in (("U-01", 10), ("TF-01", 20)):
            pallet = self.env["logistics.booking.pallet"].create({
                "booking_id": booking.id, "sequence": seq, "label": label,
                "weight_lbs": 500.0, "pickup_stop_id": ud.id})
            self.env["logistics.booking.pallet.stop.allocation"].create({
                "pallet_id": pallet.id, "delivery_stop_id": blv.id,
                "unload_sequence": 10})
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertEqual(set(job.item_ids.mapped("name")), {"U-01", "TF-01"})
        plan = self._make_plan()
        plan.add_job(job.id)
        self.assertEqual(set(plan.pallet_ids.mapped("name")),
                         {"U-01", "TF-01"})
        # Every planned pallet traces back to its booking pallet.
        self.assertEqual(
            set(plan.pallet_ids.mapped("logistics_booking_pallet_id").ids),
            set(booking.pallet_ids.ids))

    # ── Rehandling: temporary unload / reload ─────────────────────

    def test_04_temporary_unload_reload_cycle(self):
        """Temporary unload (position released) then reload elsewhere —
        the repositioning the driver does when a pallet must be moved to
        reach another one — leaves a full event trail."""
        job = self._make_job(self.customer_a, ["U-01"])
        plan = self._make_plan()
        plan.add_job(job.id)
        item = plan.pallet_ids[0]
        positions = plan.layout_template_id.position_ids.filtered(
            lambda p: not p.blocked).sorted("sequence")
        self.assertGreaterEqual(len(positions), 2)

        plan.assign_pallet_to_position(item.id, positions[0].id)
        self.assertEqual(item.position_id, positions[0])

        # Temporary unload (driver needs the position, e.g. to re-slot).
        plan.unassign_pallet(item.id)
        self.assertFalse(item.position_id)

        # Reload into another position.
        plan.assign_pallet_to_position(item.id, positions[1].id)
        self.assertEqual(item.position_id, positions[1])

        types = plan.event_ids.mapped("event_type")
        self.assertEqual(types.count("pallet_assigned"), 2)
        self.assertEqual(types.count("pallet_unassigned"), 1)

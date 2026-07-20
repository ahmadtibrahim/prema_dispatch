"""
Phase 1A — cross-driver authorization / IDOR regression tests.

Confirmed by direct code audit before this fix: driver_add_evidence,
driver_update_stop, driver_delete_stop, driver_update_service_time,
regeocode_stop, driver_reorder_stops, driver_finish_job, and
driver_upload_entrance_photo on prema.dispatch.job took a client-supplied
id and acted on it with zero verification that the requesting driver was
actually assigned to that job/stop. These tests confirm the fix
(services/dispatch_auth.py + the two new driver-scoped ir.rule records in
security/dispatch_security.xml) without changing any of that existing
business behavior for dispatch staff.

Run with:
  ./odoo-bin -c odoo18.conf -d <test-db> --test-enable \
      --test-tags /prema_dispatch:TestDriverAuthorization -u prema_dispatch
"""
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestDriverAuthorization(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.customer = self.env["res.partner"].create({"name": "IDOR Test Customer"})

        # Two distinct drivers, each with their own job + stop.
        self.driver_a_partner = self.env["res.partner"].create({"name": "IDOR Driver A"})
        self.driver_b_partner = self.env["res.partner"].create({"name": "IDOR Driver B"})

        self.job_a = self._make_job(self.driver_a_partner)
        self.stop_a = self._add_stop(self.job_a)
        self.job_b = self._make_job(self.driver_b_partner)
        self.stop_b = self._add_stop(self.job_b)

        # base.group_user (Internal User) is included on every one of these
        # to match the real account shape: action_create_driver_account()
        # (res_partner_dispatch.py) always grants Internal User + Driver
        # together, never Driver alone — a driver-group-only account (as an
        # earlier version of this fixture had) can't even read
        # prema.dispatch.stage, which the Driver App's page load needs.
        self.driver_a_user = self._make_user(
            "idor_driver_a@example.com", self.driver_a_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        self.driver_b_user = self._make_user(
            "idor_driver_b@example.com", self.driver_b_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        self.dispatcher_user = self._make_user(
            "idor_dispatcher@example.com",
            self.env["res.partner"].create({"name": "IDOR Dispatcher"}),
            "base.group_user", "prema_dispatch.group_dispatcher",
        )
        self.manager_user = self._make_user(
            "idor_manager@example.com",
            self.env["res.partner"].create({"name": "IDOR Manager"}),
            "base.group_user", "prema_dispatch.group_dispatch_manager",
        )
        # base.group_system alone (no dispatch-specific group) has no
        # model-level write access to prema.dispatch.stop per this
        # module's own pre-existing ir.model.access.csv (unrelated to this
        # patch) — a real Administrator account managing dispatch also
        # holds Dispatch Manager, so this mirrors an actual deployment
        # rather than an artificial group_system-only account.
        self.admin_user = self._make_user(
            "idor_admin@example.com",
            self.env["res.partner"].create({"name": "IDOR Admin"}),
            "base.group_system",
            "prema_dispatch.group_dispatch_manager",
        )

    def _make_job(self, driver_partner):
        return self.Job.create({
            "partner_id": self.customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": driver_partner.id,
        })

    def _add_stop(self, job, seq=10):
        return self.Stop.create({
            "job_id": job.id,
            "sequence": seq,
            "stop_type": "dropoff",
            "address": "123 Test St, Toronto, ON",
        })

    def _make_user(self, login, partner, *group_xmlids):
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": partner.name,
            "login": login,
            "partner_id": partner.id,
            "groups_id": [(6, 0, [self.env.ref(g).id for g in group_xmlids])],
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
        })

    # ── 1-3: Driver A can access their own records ──────────────

    def test_01_driver_a_can_read_own_job(self):
        job = self.Job.with_user(self.driver_a_user).browse(self.job_a.id)
        self.assertEqual(job.name, self.job_a.name)

    def test_02_driver_a_can_read_own_stop(self):
        stop = self.Stop.with_user(self.driver_a_user).browse(self.stop_a.id)
        self.assertEqual(stop.address, self.stop_a.address)

    def test_03_driver_a_can_update_own_stop(self):
        result = self.Job.with_user(self.driver_a_user).driver_update_service_time(
            self.stop_a.id, 30
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["minutes"], 30)

    # ── 4-10: Driver A cannot access/mutate Driver B's records ──

    def test_04_driver_a_cannot_read_driver_b_job(self):
        job = self.Job.with_user(self.driver_a_user).browse(self.job_b.id)
        with self.assertRaises(AccessError):
            job.name  # noqa: B018 - triggers the record-rule read check

    def test_05_driver_a_cannot_read_driver_b_stop(self):
        stop = self.Stop.with_user(self.driver_a_user).browse(self.stop_b.id)
        with self.assertRaises(AccessError):
            stop.address  # noqa: B018

    def test_06_driver_a_cannot_update_driver_b_stop(self):
        result = self.Job.with_user(self.driver_a_user).driver_update_service_time(
            self.stop_b.id, 45
        )
        self.assertFalse(result["success"])
        self.assertIn("Not authorized", result["error"])
        self.stop_b.invalidate_recordset()
        self.assertNotEqual(self.stop_b.service_time_minutes, 45)

    def test_07_driver_a_cannot_delete_driver_b_stop(self):
        result = self.Job.with_user(self.driver_a_user).driver_delete_stop(self.stop_b.id)
        self.assertFalse(result["success"])
        self.assertIn("Not authorized", result["error"])
        self.assertTrue(self.stop_b.exists())

    def test_08_driver_a_cannot_upload_pod_to_driver_b_stop(self):
        result = self.Job.with_user(self.driver_a_user).driver_add_evidence(
            self.stop_b.id, "pod", "dGVzdA==", "fake.jpg"
        )
        self.assertFalse(result["success"])
        self.assertIn("Not authorized", result["error"])

    def test_09_driver_a_cannot_upload_pop_to_driver_b_stop(self):
        result = self.Job.with_user(self.driver_a_user).driver_add_evidence(
            self.stop_b.id, "pop", "dGVzdA==", "fake.jpg"
        )
        self.assertFalse(result["success"])
        self.assertIn("Not authorized", result["error"])

    def test_10_driver_a_cannot_reorder_driver_b_stops(self):
        stop_b2 = self._add_stop(self.job_b, seq=20)
        original_sequence = self.stop_b.sequence
        result = self.Job.with_user(self.driver_a_user).driver_reorder_stops(
            self.job_b.id, [stop_b2.id, self.stop_b.id]
        )
        self.assertFalse(result["success"])
        self.assertIn("Not authorized", result["error"])
        self.stop_b.invalidate_recordset()
        self.assertEqual(self.stop_b.sequence, original_sequence)

    # ── 11: tampered/nonexistent id ──────────────────────────────

    def test_11_tampered_stop_id_rejected(self):
        fake_id = self.stop_b.id + 999999
        result = self.Job.with_user(self.driver_a_user).driver_update_stop(
            fake_id, "completed"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Stop not found")
        # Also confirm a real-but-foreign id is rejected by authorization,
        # not just a nonexistent one.
        result2 = self.Job.with_user(self.driver_a_user).driver_update_stop(
            self.stop_b.id, "completed"
        )
        self.assertFalse(result2["success"])
        self.assertIn("Not authorized", result2["error"])
        self.stop_b.invalidate_recordset()
        self.assertNotEqual(self.stop_b.status, "completed")

    # ── 12-14: dispatch staff remain fully unaffected ────────────

    def test_12_dispatcher_can_access_both_drivers_jobs(self):
        Job = self.Job.with_user(self.dispatcher_user)
        self.assertEqual(Job.browse(self.job_a.id).name, self.job_a.name)
        self.assertEqual(Job.browse(self.job_b.id).name, self.job_b.name)
        result = Job.driver_update_service_time(self.stop_a.id, 20)
        self.assertTrue(result["success"])
        result = Job.driver_update_service_time(self.stop_b.id, 20)
        self.assertTrue(result["success"])

    def test_13_manager_can_access_both_drivers_jobs(self):
        Job = self.Job.with_user(self.manager_user)
        self.assertEqual(Job.browse(self.job_a.id).name, self.job_a.name)
        self.assertEqual(Job.browse(self.job_b.id).name, self.job_b.name)
        result = Job.driver_update_service_time(self.stop_a.id, 25)
        self.assertTrue(result["success"])
        result = Job.driver_update_service_time(self.stop_b.id, 25)
        self.assertTrue(result["success"])

    def test_14_admin_remains_unaffected(self):
        Job = self.Job.with_user(self.admin_user)
        self.assertEqual(Job.browse(self.job_a.id).name, self.job_a.name)
        self.assertEqual(Job.browse(self.job_b.id).name, self.job_b.name)
        result = Job.driver_update_service_time(self.stop_a.id, 15)
        self.assertTrue(result["success"])
        result = Job.driver_update_service_time(self.stop_b.id, 15)
        self.assertTrue(result["success"])

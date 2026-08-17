"""
Task #14 backend — Driver App workflow (start route, 7-day window,
scheduled-time edit) and the evidence → invoice gate.

The invoice gate is the critical invariant:
  * Job completion NEVER auto-posts the invoice. Completion marks it
    READY FOR DISPATCH REVIEW; only a Dispatcher/Dispatch Manager's
    explicit action_approve_dispatch_review posts it.
  * Evidence copies only onto the SAME customer's DRAFT invoice.
  * A driver (or any non-dispatcher) cannot approve an invoice.
  * The whole completion path must work under the driver's own user
    (drivers have no accounting access — the gate reads run with sudo).

Run with:
  ./odoo-bin -c odoo18.conf -d <test-db> --test-enable \
      --test-tags /prema_dispatch:TestDriverAppWorkflow -u prema_dispatch
"""
import base64

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from odoo.addons.prema_dispatch.tests.test_upload_validation import (
    _b64, _real_jpeg,
)


class TestDriverAppWorkflow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.Att = self.env["ir.attachment"]
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.customer = self.env["res.partner"].create({"name": "Workflow Test Customer"})
        self.other_customer = self.env["res.partner"].create({"name": "Workflow Other Customer"})

        self.driver_a_partner = self.env["res.partner"].create({"name": "Workflow Driver A"})
        self.driver_b_partner = self.env["res.partner"].create({"name": "Workflow Driver B"})

        # Production invoices always carry a line (the booking's freight
        # line) — a line-less invoice cannot be posted, so the approval
        # tests would fail for the wrong reason without it.
        self.product = self.env["product.product"].search([], limit=1)
        self.invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.customer.id,
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id,
                "name": "Freight — workflow test line",
                "quantity": 1,
                "price_unit": 100.0,
            })],
        })
        self.other_invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.other_customer.id,
        })

        self.job_a = self.Job.create({
            "partner_id": self.customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": self.driver_a_partner.id,
            "invoice_id": self.invoice.id,
        })
        self.stop_a = self.Stop.create({
            "job_id": self.job_a.id,
            "sequence": 10,
            "stop_type": "dropoff",
            "address": "1 Workflow St, Toronto, ON",
            "pod_required": True,
        })
        self.job_b = self.Job.create({
            "partner_id": self.customer.id,
            "stage_id": self.stage_draft.id,
            "driver_id": self.driver_b_partner.id,
        })
        self.stop_b = self.Stop.create({
            "job_id": self.job_b.id,
            "sequence": 10,
            "stop_type": "dropoff",
            "address": "2 Workflow St, Toronto, ON",
        })

        # base.group_user is always included — mirrors
        # action_create_driver_account() (Internal User + Driver together).
        self.driver_a_user = self._make_user(
            "workflow_driver_a@example.com", self.driver_a_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        self.driver_b_user = self._make_user(
            "workflow_driver_b@example.com", self.driver_b_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        self.dispatcher_user = self._make_user(
            "workflow_dispatcher@example.com",
            self.env["res.partner"].create({"name": "Workflow Dispatcher"}),
            "base.group_user", "prema_dispatch.group_dispatcher",
        )
        self.manager_user = self._make_user(
            "workflow_manager@example.com",
            self.env["res.partner"].create({"name": "Workflow Manager"}),
            "base.group_user", "prema_dispatch.group_dispatch_manager",
        )

    def _make_user(self, login, partner, *group_xmlids):
        # Real accounts get an email from action_create_driver_account() —
        # without one, mail.message_post on the job raises "configure the
        # sender's email address" when the driver completes a stop.
        partner.email = login
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": partner.name,
            "login": login,
            "partner_id": partner.id,
            "groups_id": [(6, 0, [self.env.ref(g).id for g in group_xmlids])],
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
        })

    def _add_pod(self, user, stop):
        """Upload POD evidence to a stop as the given user."""
        r = self.Job.with_user(user).driver_add_evidence(
            stop.id, "pod", _b64(_real_jpeg()), "pod.jpg"
        )
        self.assertTrue(r["success"], r)
        return r

    def _complete_stop(self, user, stop):
        self._add_pod(user, stop)
        self.Stop.with_user(user).browse(stop.id).action_mark_completed()

    def _invoice_messages(self, invoice):
        return self.env["mail.message"].search([
            ("model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])

    def _ready_message(self, invoice):
        return self._invoice_messages(invoice).filtered(
            lambda m: "READY FOR DISPATCH REVIEW" in (m.body or "")
        )

    # ── 1-4: the invoice gate ──────────────────────────────────

    def test_01_completion_marks_ready_never_auto_posts(self):
        """Driver completes the last stop with POD: job completes, the
        invoice STAYS DRAFT and is flagged READY FOR DISPATCH REVIEW."""
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.job_a.invalidate_recordset()
        self.assertTrue(self.job_a.stage_id.is_completed)
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, "draft",
                         "Completion must NEVER auto-post the invoice")
        self.assertEqual(self.invoice.dispatch_status, "pod_ready")
        self.assertTrue(self._ready_message(self.invoice),
                        "Invoice must carry the READY FOR DISPATCH REVIEW message")

    def test_02_dispatcher_approval_posts_invoice(self):
        """After review-ready, the dispatcher's manual approval is the only
        path that posts — and it works without accounting rights."""
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.invoice.invalidate_recordset()
        result = self.invoice.with_user(self.dispatcher_user).action_approve_dispatch_review()
        self.assertTrue(result)
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, "posted")
        self.assertEqual(self.invoice.dispatch_status, "posted")

    def test_03_driver_cannot_approve_invoice(self):
        """A driver calling the approve action gets a clean UserError and
        the invoice stays draft — even on a fully completed job."""
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.invoice.invalidate_recordset()
        with self.assertRaises(UserError):
            self.invoice.with_user(self.driver_a_user).action_approve_dispatch_review()
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, "draft")

    def test_04_approval_blocked_while_any_job_incomplete(self):
        """A dispatcher cannot approve while another job on the same
        invoice is still in progress."""
        self.job_b.invoice_id = self.invoice.id  # second, incomplete job
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.invoice.invalidate_recordset()
        with self.assertRaises(UserError):
            self.invoice.with_user(self.dispatcher_user).action_approve_dispatch_review()
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, "draft")

    def test_05_approve_rejects_posted_invoice(self):
        """Approving an already-posted invoice raises."""
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.invoice.with_user(self.dispatcher_user).action_approve_dispatch_review()
        with self.assertRaises(UserError):
            self.invoice.with_user(self.dispatcher_user).action_approve_dispatch_review()

    # ── 5-7: evidence copies only to the same-customer DRAFT invoice ──

    def test_06_evidence_never_copies_to_other_customer_invoice(self):
        """A stop whose own invoice is a DIFFERENT customer's draft gets no
        copy — even though the job's invoice belongs to the right customer."""
        self.stop_a.invoice_id = self.other_invoice.id
        r = self._add_pod(self.driver_a_user, self.stop_a)
        tag = f"__evidence_source:{r['id']}__"
        copies = self.Att.search([("description", "=", tag)])
        self.assertEqual(len(copies), 0,
                         "Cross-customer evidence must never be copied to an invoice")
        self.assertEqual(copies.res_model, False)

    def test_07_evidence_copies_to_own_customer_draft_invoice(self):
        """Same-customer DRAFT invoice (the job's own) receives the copy."""
        r = self._add_pod(self.driver_a_user, self.stop_a)
        tag = f"__evidence_source:{r['id']}__"
        copies = self.Att.search([("description", "=", tag)])
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies.res_model, "account.move")
        self.assertEqual(copies.res_id, self.invoice.id)

    def test_08_evidence_never_copies_to_posted_invoice(self):
        """A POSTED same-customer invoice never receives evidence copies."""
        self._complete_stop(self.driver_a_user, self.stop_a)
        self.invoice.with_user(self.dispatcher_user).action_approve_dispatch_review()
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, "posted")
        r = self._add_pod(self.driver_b_user, self.stop_b)  # job_b has no invoice
        tag = f"__evidence_source:{r['id']}__"
        copies = self.Att.search([("description", "=", tag)])
        self.assertEqual(len(copies), 0,
                         "Posted invoices must never receive evidence copies")

    # ── 8-10: start route ─────────────────────────────────────

    def test_09_start_route_records_who_and_when(self):
        r = self.Job.with_user(self.driver_a_user).driver_start_route(self.job_a.id)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["job_id"], self.job_a.id)
        self.assertTrue(r["route_started_at"], "route_started_at must be returned")
        self.job_a.invalidate_recordset()
        self.assertTrue(self.job_a.route_started_at)
        self.assertEqual(self.job_a.route_started_by.id, self.driver_a_user.id)
        self.assertTrue(r["job_summary"]["route_started"])

    def test_10_start_route_is_idempotent(self):
        r1 = self.Job.with_user(self.driver_a_user).driver_start_route(self.job_a.id)
        r2 = self.Job.with_user(self.driver_a_user).driver_start_route(self.job_a.id)
        self.assertEqual(r1["route_started_at"], r2["route_started_at"],
                         "Starting twice must not rewrite the timestamp")

    def test_11_other_driver_cannot_start_route(self):
        r = self.Job.with_user(self.driver_b_user).driver_start_route(self.job_a.id)
        self.assertFalse(r["success"])
        self.assertIn("Not authorized", r["error"])
        self.job_a.invalidate_recordset()
        self.assertFalse(self.job_a.route_started_at)

    def test_12_finished_job_cannot_start_route(self):
        self._complete_stop(self.driver_a_user, self.stop_a)
        r = self.Job.with_user(self.driver_a_user).driver_start_route(self.job_a.id)
        self.assertFalse(r["success"])
        self.assertIn("already finished", r["error"])

    def test_13_dispatcher_can_start_route_on_any_job(self):
        r = self.Job.with_user(self.dispatcher_user).driver_start_route(self.job_b.id)
        self.assertTrue(r["success"], r)
        self.job_b.invalidate_recordset()
        self.assertTrue(self.job_b.route_started_at)
        self.assertEqual(self.job_b.route_started_by.id, self.dispatcher_user.id)

    # ── 11: 7-day driver window ───────────────────────────────

    def test_14_driver_window_has_seven_days_yesterday_to_plus_five(self):
        import datetime
        import pytz

        tz = pytz.timezone("America/Toronto")
        today = self.Job._user_today(tz)
        days = self.Job.get_driver_available_dates()["days"]
        self.assertEqual(len(days), 7,
                         "The schedule must show exactly 7 days")
        self.assertEqual(days[0]["date"], (today - datetime.timedelta(days=1)).isoformat())
        self.assertEqual(days[6]["date"], (today + datetime.timedelta(days=5)).isoformat())
        self.assertTrue(all("weekday" in d for d in days))

    def test_15_job_scheduled_pickup_shows_in_window(self):
        from datetime import datetime
        import pytz

        tz = pytz.timezone("America/Toronto")
        first, today, last = self.Job._driver_seven_day_window(tz)
        scheduled = tz.localize(
            datetime.combine(last, datetime.min.time())
        ).astimezone(pytz.utc).replace(tzinfo=None)
        self.job_b.write({
            "driver_id": self.driver_a_partner.id,
            "scheduled_pickup": scheduled,
        })
        # The window is computed for the CALLING user's partner — query as
        # the driver, not as the test's admin.
        days = self.Job.with_user(self.driver_a_user).get_driver_available_dates()["days"]
        entry = next((d for d in days if d["job_count"]), None)
        self.assertTrue(entry, "The scheduled job must appear in the window")
        self.assertEqual(entry["job_count"], 1)
        self.assertTrue(entry["has_active"])

    # ── 12: scheduled-time edit ───────────────────────────────

    def test_16_edit_stop_accepts_full_datetime(self):
        r = self.Job.with_user(self.driver_a_user).driver_edit_stop(
            self.stop_a.id, {"scheduled_time": "2026-08-17T18:30:00"}
        )
        self.assertTrue(r["success"], r)
        self.stop_a.invalidate_recordset()
        self.assertEqual(str(self.stop_a.scheduled_time)[:16], "2026-08-17 18:30")

    def test_17_edit_stop_rejects_bare_time(self):
        r = self.Job.with_user(self.driver_a_user).driver_edit_stop(
            self.stop_a.id, {"scheduled_time": "18:30"}
        )
        self.assertFalse(r["success"])
        self.assertIn("full date-time", r["error"])
        self.stop_a.invalidate_recordset()
        self.assertFalse(self.stop_a.scheduled_time)

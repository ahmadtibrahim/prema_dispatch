import re
from pathlib import Path

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase


class TestDriverGuidedFlowV7(TransactionCase):
    """Release-gate contracts for the phone-first guided Driver App.

    Browser/mobile UAT is still required, but these tests protect the core
    workflow decisions from being accidentally removed by later refactors.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(get_module_path("prema_dispatch"))
        cls.js = (cls.root / "static/src/js/driver_guided_flow_v7.js").read_text(encoding="utf-8")
        cls.hotfix = (cls.root / "static/src/js/driver_guided_flow_v7_hotfix.js").read_text(encoding="utf-8")
        cls.css = (cls.root / "static/src/css/driver_guided_flow_v7.css").read_text(encoding="utf-8")
        cls.py = (cls.root / "models/driver_guided_flow.py").read_text(encoding="utf-8")
        cls.manifest = (cls.root / "__manifest__.py").read_text(encoding="utf-8")

    def test_assets_loaded_after_v6(self):
        self.assertIn("static/src/css/driver_guided_flow_v7.css", self.manifest)
        self.assertIn("static/src/js/driver_guided_flow_v7.js", self.manifest)
        self.assertIn("static/src/js/driver_guided_flow_v7_hotfix.js", self.manifest)
        self.assertGreater(
            self.manifest.index("driver_guided_flow_v7.js"),
            self.manifest.index("driver_native_nav_v6.js"),
        )
        self.assertGreater(
            self.manifest.index("driver_guided_flow_v7_hotfix.js"),
            self.manifest.index("driver_guided_flow_v7.js"),
        )

    def test_stop_model_has_non_terminal_deferred_and_exception_states(self):
        status = self.env["prema.dispatch.stop"]._fields["status"]
        selection = status.selection(self.env["prema.dispatch.stop"]) if callable(status.selection) else status.selection
        values = dict(selection)
        self.assertEqual(values.get("deferred"), "Come Back Later")
        self.assertEqual(values.get("exception"), "Exception / Needs Resolution")
        self.assertIn("driver_deferred_reason", self.env["prema.dispatch.stop"]._fields)
        self.assertIn("driver_exception_reason", self.env["prema.dispatch.stop"]._fields)

    def test_deferred_is_not_implemented_as_terminal_skip(self):
        self.assertIn('action == "defer"', self.py)
        self.assertIn('"status": "deferred"', self.py)
        self.assertIn("Stop saved for later", self.py)
        self.assertNotIn('"status": "skipped"', self.py)

    def test_driver_can_return_to_deferred_stop(self):
        self.assertIn("resume_deferred", self.py)
        self.assertIn("returnToDeferred", self.js)
        self.assertIn("Return to This Stop", self.js)
        self.assertIn("driver_deferred_until", self.py)
        self.assertIn("When should this stop come back up?", self.hotfix)

    def test_driver_audit_events_are_internal_notes(self):
        self.assertIn('subtype_xmlid="mail.mt_note"', self.py)
        self.assertIn("_post_driver_audit", self.py)

    def test_guided_actions_never_fail_silently(self):
        # 2026-08-20 UAT defect: defer/report_problem/resume_deferred/
        # resume_exception/make_next called rpc() bare — a JSON-RPC error
        # (unhandled server exception) was an unhandled rejection and the
        # driver got zero feedback. All five must route through the guarded
        # helper, and the audit note must be best-effort server-side so a
        # missing author email can never roll back a driver action.
        self.assertIn('guidedStatusCall(stop, "defer"', self.js)
        self.assertIn('guidedStatusCall(stop, "resume_deferred"', self.js)
        self.assertIn('guidedStatusCall(stop, "report_problem"', self.js)
        self.assertIn('guidedStatusCall(stop, "resume_exception"', self.js)
        self.assertIn('guidedStatusCall(stop, "make_next"', self.js)
        self.assertIn("catch (e) {", self.js)
        self.assertIn("audit note skipped for job", self.py)
        self.assertIn("except Exception:", self.py)

    def test_start_route_is_home_level_and_stop_route_button_removed(self):
        self.assertIn("START ROUTE", self.js)
        self.assertIn(".da-route-start-btn", self.js)
        self.assertIn("btn.remove()", self.js)

    def test_prearrival_screen_is_navigation_first(self):
        self.assertIn('data-v7="navigate"', self.js)
        self.assertIn("Come Back Later", self.js)
        self.assertIn("Report a Problem", self.js)
        self.assertIn("I'm Here", self.js)
        self.assertIn("renderSimplifiedStop", self.js)

    def test_arrival_unlocks_guided_workflow(self):
        self.assertIn("Tap I'm Here before starting stop work", self.js)
        self.assertIn("Stop work is now unlocked", self.js)
        self.assertIn("Continue Pickup", self.js)
        self.assertIn("Continue Delivery", self.js)

    def test_pickup_is_sequential_and_position_assignment_uses_load_plan(self):
        # Kicker is built from a template literal (`Pickup · Step N of M`),
        # so the literal never appears verbatim in the source.
        self.assertIn('? "Pickup" : "Delivery"} · Step', self.js)
        self.assertIn("Verify the destination already assigned by Dispatch", self.js)
        self.assertIn("Place the same pallets", self.js)
        self.assertIn('"/dispatch/driver/loadplan/assign"', self.js)
        self.assertIn("Take Pallet Photo", self.js)
        self.assertIn("Confirm Pickup", self.js)

    def test_pickup_actual_count_can_advance_before_final_gate(self):
        self.assertIn('result?.code === "pickup_gate_blocked"', self.hotfix)
        self.assertIn("Pallet count saved — continue the pickup steps", self.hotfix)
        self.assertIn("ensurePickupLoadPlan(true)", self.hotfix)

    def test_delivery_is_sequential_and_stop_specific(self):
        self.assertIn("Unload only the freight for this stop", self.js)
        self.assertIn("I physically verified the unload", self.js)
        self.assertIn("Confirm Delivery", self.js)

    def test_guided_workflow_has_close_back_continue(self):
        self.assertIn('data-v7="close"', self.js)
        self.assertIn('data-v7="back"', self.js)
        self.assertIn('data-v7="continue"', self.js)
        self.assertIn("Progress saved", self.js)

    def test_google_maps_handoff_remains_primary_navigation(self):
        self.assertIn("APP.openExternalNav", self.js)
        self.assertIn("https://www.google.com/maps/dir/", self.js)
        self.assertIn('dir_action: "navigate"', self.js)

    def test_out_of_sequence_change_requires_reason_and_is_audited(self):
        self.assertIn("makeOutOfSequenceNext", self.js)
        # 2026-08-20: the call was re-wired through the guarded helper (it
        # used to rpc() bare with action:"make_next" in the payload).
        self.assertIn('guidedStatusCall(stop, "make_next"', self.js)
        self.assertIn("driver_sequence_override_reason", self.py)
        self.assertIn("Driver changed stop sequence", self.py)

    def test_driving_mode_lock_exists(self):
        self.assertIn("DRIVING_MPS", self.js)
        self.assertIn("Driving Mode", self.js)
        self.assertIn("da-v7-driving", self.css)

    def test_raw_load_plan_jargon_is_translated_for_driver(self):
        self.assertIn("Load plan not ready", self.js)
        self.assertIn("Vehicle layout must be confirmed by Dispatch", self.js)
        self.assertIn('el.textContent = "Plan updated"', self.js)

    def test_mobile_layout_has_small_screen_breakpoints(self):
        self.assertIn("@media(max-width:600px)", self.css)
        self.assertIn("@media(max-width:360px)", self.css)
        self.assertIn("min-height:48px", self.css)


class TestDriverGuidedFlowV7MutationStorm(TransactionCase):
    """Static contracts against the 2026-08-19 UAT blocker.

    The v7 guided layer mounted a MutationObserver on #app and then
    unconditionally rewrote innerHTML/textContent on every audit pass —
    each pass re-triggering the observer and queueing one rAF per mutation
    record. Browser latency escalated 5.5s → 9.4s → 90.7s on the
    arrived/pickup screen. These contracts pin the idempotency and
    coalescing guarantees so the storm cannot be reintroduced by a
    refactor without a red test.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(get_module_path("prema_dispatch"))
        cls.js = (cls.root / "static/src/js/driver_guided_flow_v7.js").read_text(encoding="utf-8")
        cls.v6 = (cls.root / "static/src/js/driver_flow_v6.js").read_text(encoding="utf-8")

    @staticmethod
    def _function_body(source, name):
        match = re.search(rf"function {name}\(.*?\n    \}}", source, re.S)
        self = TestDriverGuidedFlowV7MutationStorm
        if not match:
            raise AssertionError(f"{name} not found")
        return match.group(0)

    def test_observer_queues_one_coalesced_audit_per_frame(self):
        # The observer callback must route through a queued flag — never call
        # auditDom per mutation record.
        self.assertIn("auditQueued", self.js)
        self.assertIn("queueAudit", self.js)
        self.assertRegex(self.js, r"new MutationObserver\(queueAudit\)")
        self.assertIn("auditQueued = true", self.js)
        self.assertIn("auditQueued = false", self.js)
        self.assertNotIn("new MutationObserver(() => requestAnimationFrame(auditDom))", self.js)
        # Same guarantee on the v6 layer, which shares the page.
        self.assertIn("auditQueued", self.v6)
        self.assertNotIn("new MutationObserver(() => requestAnimationFrame(auditDom))", self.v6)

    def test_render_simplified_stop_is_key_guarded(self):
        body = self._function_body(self.js, "renderSimplifiedStop")
        # Exactly one innerHTML write, and it must sit behind the render-key
        # guard (key stamped on the rendered card; no write when unchanged).
        self.assertEqual(body.count("body.innerHTML = html"), 1)
        self.assertIn("renderKey", body)
        self.assertIn("if (rootCard?.dataset?.v7RenderKey === renderKey) return;", body)
        self.assertIn("dataset.v7RenderKey = renderKey", body)

    def test_render_guide_uses_content_signature(self):
        body = self._function_body(self.js, "renderGuide")
        self.assertIn("lastGuideRenderKey", body)
        self.assertIn("if (contentKey !== lastGuideRenderKey)", body)
        # Signature covers the required inputs: stop id, workflow mode/step,
        # completion state, load-plan state, and pending-evidence state.
        self.assertIn("stop.id, guide.mode, guide.step, guide.unloadConfirmed", body)
        self.assertIn("pickup ? S.loadPlan : null", body)
        self.assertIn('hasPendingEvidence(stop.id, pickup ? "pop" : "pod")', body)
        # Progress/body writes are behind compare-then-write guards.
        self.assertIn("if (progress.innerHTML !== progressHtml) progress.innerHTML = progressHtml;", body)
        self.assertIn("if (body.innerHTML !== bodyHtml) body.innerHTML = bodyHtml;", body)

    def test_post_process_load_plan_compares_before_writing(self):
        body = self._function_body(self.js, "postProcessLoadPlan")
        self.assertIn('if (el.textContent !== "Plan updated") el.textContent = "Plan updated";', body)
        self.assertIn("if (el.innerHTML !== intended)", body)

    def test_post_process_list_writes_are_guarded(self):
        body = self._function_body(self.js, "postProcessList")
        self.assertIn('badge.textContent !== "↪ Come Back Later"', body)
        self.assertIn('badge.textContent !== "⚠ Exception"', body)
        self.assertIn('if (nextButton.textContent !== "Navigate") nextButton.textContent = "Navigate";', body)
        # onclick is bound exactly once (a fresh closure each pass would
        # rewrite the reflected attribute and re-trigger the observer).
        self.assertIn("if (!nextButton.dataset.v7Bound)", body)
        self.assertIn('nextButton.dataset.v7Bound = "1";', body)

    def test_v6_pickup_action_order_reorders_only_when_needed(self):
        body = self._function_body(self.v6, "enforcePickupActionOrder")
        self.assertIn("const needsFix", body)
        self.assertIn("if (needsFix) {", body)

    def test_perf_counters_exist_for_uat_measurement(self):
        self.assertIn("__v7Perf", self.js)
        self.assertIn("audits", self.js)
        self.assertIn("stopRenders", self.js)
        self.assertIn("guideRenders", self.js)
        self.assertIn("lpWrites", self.js)


class TestGuidedActionsEmailLessDriver(TransactionCase):
    """Behavioral contracts for the 2026-08-20 UAT defect: every guided
    transition (defer / resume_deferred / report_problem / resume_exception
    / make_next) 500'd with "Unable to send message, please configure the
    sender's email address." when the driver account has no email —
    message_post requires an author email and the failing audit note rolled
    back the state change the driver had just made. Production driver
    accounts are always created from partners WITH an email
    (action_create_driver_account uses partner.email as login), so this is
    a hardening for manually-created accounts — and it was exactly what
    blocked the UAT walkthrough.
    """

    def setUp(self):
        super().setUp()
        self.Job = self.env["prema.dispatch.job"]
        self.Stop = self.env["prema.dispatch.stop"]
        self.stage_draft = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        self.customer = self.env["res.partner"].create({"name": "Guided Test Customer"})

        # Email-less driver: the defect case (manual account creation).
        self.bare_partner = self.env["res.partner"].create({"name": "Guided Bare Driver"})
        self.bare_driver = self._make_user(
            "guided_bare_driver@example.com", self.bare_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )
        # Email'd driver: the normal production account shape.
        self.mailed_partner = self.env["res.partner"].create({
            "name": "Guided Mailed Driver",
            "email": "guided_mailed_driver@example.com",
        })
        self.mailed_driver = self._make_user(
            "guided_mailed_driver@example.com", self.mailed_partner,
            "base.group_user", "prema_dispatch.group_dispatch_driver",
        )

        self.job_bare = self._make_job(self.bare_partner)
        self.stop_a = self._add_stop(self.job_bare, seq=10)
        self.stop_b = self._add_stop(self.job_bare, seq=20)
        self.job_mailed = self._make_job(self.mailed_partner)
        self.stop_c = self._add_stop(self.job_mailed, seq=10)

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

    def test_01_defer_succeeds_for_email_less_driver(self):
        result = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_a.id, "defer", {"reason": "long_wait"}
        )
        self.assertTrue(result.get("success"), result)
        self.assertEqual(result.get("status"), "deferred")
        self.stop_a.invalidate_recordset()
        self.assertEqual(self.stop_a.status, "deferred")
        self.assertEqual(self.stop_a.driver_deferred_reason, "long_wait")
        # Deferred stays open but moves behind the other serviceable stop.
        self.assertEqual(self.stop_a.sequence, 30)

    def test_02_resume_deferred_restores_original_sequence(self):
        self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_a.id, "defer", {"reason": "customer_closed"}
        )
        result = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_a.id, "resume_deferred", {"make_current": False}
        )
        self.assertTrue(result.get("success"), result)
        self.stop_a.invalidate_recordset()
        self.assertEqual(self.stop_a.status, "pending")
        self.assertEqual(self.stop_a.sequence, 10)
        self.assertFalse(self.stop_a.driver_deferred_reason)
        self.assertFalse(self.stop_a.driver_deferred_at)

    def test_03_report_problem_and_resume_exception(self):
        result = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_b.id, "report_problem",
            {"reason": "refused_freight", "notes": "Customer refused the freight"},
        )
        self.assertTrue(result.get("success"), result)
        self.stop_b.invalidate_recordset()
        self.assertEqual(self.stop_b.status, "exception")
        self.assertEqual(self.stop_b.driver_exception_reason, "refused_freight")
        self.assertEqual(self.stop_b.driver_exception_notes, "Customer refused the freight")
        self.assertEqual(self.stop_b.driver_exception_previous_status, "pending")

        result2 = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_b.id, "resume_exception", {}
        )
        self.assertTrue(result2.get("success"), result2)
        self.stop_b.invalidate_recordset()
        self.assertEqual(self.stop_b.status, "pending")
        self.assertFalse(self.stop_b.driver_exception_reason)
        self.assertFalse(self.stop_b.driver_exception_opened_at)

    def test_04_make_next_moves_stop_to_front(self):
        result = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_b.id, "make_next", {"reason": "Customer appointment timing"}
        )
        self.assertTrue(result.get("success"), result)
        self.stop_b.invalidate_recordset()
        self.assertEqual(self.stop_b.sequence, 9)  # min(siblings)=10 → 10-1
        self.assertEqual(self.stop_b.driver_sequence_override_reason, "Customer appointment timing")

    def test_05_audit_note_posts_when_driver_has_email(self):
        self.Job.with_user(self.mailed_driver).driver_update_stop(
            self.stop_c.id, "defer", {"reason": "appointment_later"}
        )
        note = self.env["mail.message"].search([
            ("model", "=", "prema.dispatch.job"),
            ("res_id", "=", self.job_mailed.id),
            ("subtype_id", "=", self.env.ref("mail.mt_note").id),
        ], order="id desc", limit=1)
        self.assertTrue(note, "audit note should exist for an email'd driver")
        self.assertIn("Driver deferred stop", note.body)

    def test_06_closed_stop_is_rejected_before_any_post(self):
        self.Stop.browse(self.stop_a.id).write({"status": "completed"})
        result = self.Job.with_user(self.bare_driver).driver_update_stop(
            self.stop_a.id, "defer", {"reason": "other"}
        )
        self.assertFalse(result.get("success"), result)
        self.assertIn("Closed stops", result.get("error", ""))

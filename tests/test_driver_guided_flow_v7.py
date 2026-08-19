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
        self.assertIn('action: "make_next"', self.js)
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

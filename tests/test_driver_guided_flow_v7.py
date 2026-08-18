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
        cls.css = (cls.root / "static/src/css/driver_guided_flow_v7.css").read_text(encoding="utf-8")
        cls.py = (cls.root / "models/driver_guided_flow.py").read_text(encoding="utf-8")
        cls.manifest = (cls.root / "__manifest__.py").read_text(encoding="utf-8")

    def test_assets_loaded_after_v6(self):
        self.assertIn("static/src/css/driver_guided_flow_v7.css", self.manifest)
        self.assertIn("static/src/js/driver_guided_flow_v7.js", self.manifest)
        self.assertGreater(
            self.manifest.index("driver_guided_flow_v7.js"),
            self.manifest.index("driver_native_nav_v6.js"),
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
        self.assertIn("Pickup · Step", self.js)
        self.assertIn("Verify the destination already assigned by Dispatch", self.js)
        self.assertIn("Place the same pallets", self.js)
        self.assertIn('"/dispatch/driver/loadplan/assign"', self.js)
        self.assertIn("Take Pallet Photo", self.js)
        self.assertIn("Confirm Pickup", self.js)

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

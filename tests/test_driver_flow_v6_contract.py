from pathlib import Path

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase


class TestDriverFlowV6Contract(TransactionCase):
    """Fast release-gate checks for browser workflow regressions.

    These do not replace mobile UAT; they make the high-risk state transitions
    impossible to accidentally remove in a later refactor without a failing
    addon test.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(get_module_path("prema_dispatch"))
        cls.js = (cls.root / "static/src/js/driver_flow_v6.js").read_text(encoding="utf-8")
        cls.manifest = (cls.root / "__manifest__.py").read_text(encoding="utf-8")

    def test_driver_asset_is_loaded(self):
        self.assertIn("static/src/js/driver_flow_v6.js", self.manifest)
        self.assertIn('location.pathname.startsWith("/dispatch/driver")', self.js)

    def test_navigation_arrival_opens_same_stop_not_home(self):
        self.assertIn("APP.navArrived = fixedArriveFromNavigation", self.js)
        self.assertIn('showScreen("sStop")', self.js)
        self.assertIn("S.navAsTab = false", self.js)

    def test_google_maps_native_navigation_contract(self):
        self.assertIn('dir_action: "navigate"', self.js)
        self.assertIn("https://www.google.com/maps/dir/", self.js)

    def test_pickup_confirmation_is_backend_gate_driven(self):
        self.assertIn("pickup_gate_ready", self.js)
        self.assertIn("confirm.disabled = !gate.gateReady", self.js)
        self.assertIn("assign-stops-pallets", self.js)

    def test_completion_overlay_is_closed_before_load_plan(self):
        self.assertIn('text === "Open Load Plan"', self.js)
        self.assertIn("closeBlockingOverlays();", self.js)

    def test_return_to_base_precedes_end_work(self):
        self.assertIn("/dispatch/driver/work/base", self.js)
        self.assertIn("/dispatch/driver/work/end-day", self.js)
        self.assertIn("maybeAutoEndAtBase", self.js)
        self.assertIn("allOperationalStopsClosed", self.js)

    def test_scanner_has_escape_path(self):
        self.assertIn("scannerEscapeHardening", self.js)
        self.assertIn("closeScanner", self.js)

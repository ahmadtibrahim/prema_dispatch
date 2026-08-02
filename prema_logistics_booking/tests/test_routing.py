"""Routing service tests."""
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.routing_service import RoutingService


class TestRouting(TransactionCase):
    def test_01_direct_sellable_route(self):
        """R1→R13 should be direct (sellable line)."""
        svc = RoutingService(self.env)
        result = svc.determine_routing("R1", "R13")
        self.assertEqual(result.strategy, "direct")

    def test_02_hub_transfer_for_non_sellable(self):
        """R3→R15 should be hub_transfer via R1."""
        svc = RoutingService(self.env)
        result = svc.determine_routing("R3", "R15")
        self.assertEqual(result.strategy, "hub_transfer")
        self.assertEqual(result.via_hub, "R1")

    def test_03_custom_quote_for_unsupported(self):
        """Unknown regions get custom_quote."""
        svc = RoutingService(self.env)
        result = svc.determine_routing("R99", "Z99")
        self.assertEqual(result.strategy, "custom_quote")

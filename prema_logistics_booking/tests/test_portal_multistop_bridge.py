from odoo.tests.common import TransactionCase

from ..services.portal_multistop_bridge import _movement_counts


class TestPortalMultistopBridge(TransactionCase):

    def test_two_pickups_can_share_one_delivery_stop(self):
        """Different origins feeding one facility remain one DL stop key."""
        movements = [
            {
                "key": "P1",
                "pickup_stop_key": "PU1",
                "delivery_stop_keys": ["DL1"],
                "weight_lbs": 500.0,
            },
            {
                "key": "P2",
                "pickup_stop_key": "PU2",
                "delivery_stop_keys": ["DL1"],
                "weight_lbs": 600.0,
            },
        ]

        pickup_counts, pickup_weights, delivery_counts, delivery_weights = (
            _movement_counts(movements)
        )

        self.assertEqual(pickup_counts, {"PU1": 1, "PU2": 1})
        self.assertEqual(pickup_weights, {"PU1": 500.0, "PU2": 600.0})
        self.assertEqual(delivery_counts, {"DL1": 2})
        self.assertEqual(delivery_weights, {"DL1": 1100.0})

    def test_shared_pallet_is_one_physical_pickup(self):
        """One shared physical pallet may unload at multiple destinations."""
        movements = [
            {
                "key": "P1",
                "pickup_stop_key": "PU1",
                "delivery_stop_keys": ["DL1", "DL2"],
                "delivery_weights": [250.0, 250.0],
                "weight_lbs": 500.0,
                "shared": True,
            },
        ]

        pickup_counts, pickup_weights, delivery_counts, delivery_weights = (
            _movement_counts(movements)
        )

        self.assertEqual(pickup_counts, {"PU1": 1})
        self.assertEqual(pickup_weights, {"PU1": 500.0})
        self.assertEqual(delivery_counts, {"DL1": 1, "DL2": 1})
        self.assertEqual(delivery_weights, {"DL1": 250.0, "DL2": 250.0})

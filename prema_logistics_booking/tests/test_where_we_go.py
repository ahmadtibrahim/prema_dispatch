"""NetworkAvailabilityService / Where We Go RPC tests."""
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.network_availability_service import (
    NetworkAvailabilityService,
)


class TestNetworkAvailabilityService(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Region = cls.env["logistics.region"]
        cls.origin = Region.create({"code": "WWGO", "name": "WWG Test Origin", "customer_visible": True})
        cls.direct_dest = Region.create({"code": "WWGD", "name": "WWG Test Direct Dest", "customer_visible": True})
        cls.unreachable_dest = Region.create({"code": "WWGU", "name": "WWG Test Unreachable Dest", "customer_visible": True})

        VehicleModel = cls.env["fleet.vehicle.model"]
        model = VehicleModel.search([], limit=1)
        if not model:
            brand = cls.env["fleet.vehicle.model.brand"].search([], limit=1) or \
                cls.env["fleet.vehicle.model.brand"].create({"name": "WWG-Brand"})
            model = VehicleModel.create({"name": "WWG-Model", "brand_id": brand.id})
        cls.vehicle = cls.env["fleet.vehicle"].create({
            "name": "WWG-Test-Truck", "license_plate": "WWGTEST", "model_id": model.id,
            "x_operational_logistics": True, "x_max_pallets": 14, "x_max_payload_lbs": 20000.0,
            "straight_pallet_capacity": 12, "pin_wheel_pallet_capacity": 13, "turned_pallet_capacity": 14,
        })

        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "WWG-Test-Corridor", "direction": "eastbound", "phase": 1, "truck_slot": 1, "weekday": "1",
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.origin.id,
             "pickup_allowed": True, "delivery_allowed": False},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.direct_dest.id,
             "pickup_allowed": False, "delivery_allowed": True},
        ])
        cls.env["logistics.corridor.departure"].create({
            "corridor_id": cls.corridor.id,
            "departure_date": cls._next_weekday(),
            "departure_time": 1.0, "cutoff_time": 16.0,
            "status": "scheduled", "vehicle_id": cls.vehicle.id, "max_capacity": 12,
        })

    @classmethod
    def _next_weekday(cls):
        import datetime
        today = datetime.date.today()
        return today + datetime.timedelta(days=1)

    def test_direct_destination_classified_correctly(self):
        svc = NetworkAvailabilityService(self.env)
        results = svc.list_destinations_from(self.origin)
        by_id = {r["region_id"]: r for r in results}
        self.assertIn(self.direct_dest.id, by_id)
        self.assertEqual(by_id[self.direct_dest.id]["status"], "direct")
        self.assertEqual(len(by_id[self.direct_dest.id]["legs"]), 1)

    def test_unreachable_destination_reports_reason(self):
        svc = NetworkAvailabilityService(self.env)
        results = svc.list_destinations_from(self.origin)
        by_id = {r["region_id"]: r for r in results}
        self.assertIn(self.unreachable_dest.id, by_id)
        self.assertEqual(by_id[self.unreachable_dest.id]["status"], "unavailable")
        self.assertTrue(by_id[self.unreachable_dest.id]["reason"])

    def test_rpc_requires_dispatch_group(self):
        plain_user = self.env["res.users"].create({
            "name": "WWG Plain User", "login": "wwg_plain_user",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            self.env["logistics.region"].with_user(plain_user).get_network_map_data()
        with self.assertRaises(AccessError):
            self.env["logistics.region"].with_user(plain_user).get_network_destinations(
                "logistics.region", self.origin.id
            )

    def test_rpc_works_for_dispatcher(self):
        dispatcher = self.env["res.users"].create({
            "name": "WWG Dispatcher", "login": "wwg_dispatcher",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("prema_dispatch.group_dispatcher").id,
            ])],
        })
        data = self.env["logistics.region"].with_user(dispatcher).get_network_map_data()
        self.assertIn("regions", data)
        self.assertIn("hubs", data)

    def test_corridor_two_way_and_rate_plan_fields_default_safely(self):
        """is_two_way / effective_rate_plan_ids must never crash and must
        default to False/empty when no pairing or rate plan exists."""
        self.assertFalse(self.corridor.is_two_way)
        self.assertFalse(self.corridor.effective_rate_plan_ids)

        reverse_corridor = self.env["logistics.corridor"].create({
            "name": "WWG-Test-Corridor-Return", "direction": "westbound",
            "phase": 1, "truck_slot": 1, "weekday": "2",
        })
        self.env["logistics.corridor.stop"].create([
            {"corridor_id": reverse_corridor.id, "sequence": 10, "region_id": self.direct_dest.id,
             "pickup_allowed": True, "delivery_allowed": False},
            {"corridor_id": reverse_corridor.id, "sequence": 20, "region_id": self.origin.id,
             "pickup_allowed": False, "delivery_allowed": True},
        ])
        self.corridor.return_corridor_id = reverse_corridor.id
        self.assertTrue(self.corridor.is_two_way)

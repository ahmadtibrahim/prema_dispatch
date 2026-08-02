"""Schedule engine tests."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
from odoo.tests import TransactionCase
from odoo.addons.prema_logistics_booking.services.schedule_service import ScheduleService

BUSINESS_TZ = ZoneInfo("America/Toronto")


class TestSchedule(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Lane = cls.env["logistics.lane"]
        cls.SLevel = cls.env["logistics.service.level"]
        cls.SOffering = cls.env["logistics.service.offering"]
        cls.Schedule = cls.env["logistics.lane.schedule"]

        r1 = cls.Region.create({"code": "S1", "name": "Sched Region 1"})
        r2 = cls.Region.create({"code": "S2", "name": "Sched Region 2"})
        lane = cls.Lane.create({"origin_region_id": r1.id, "destination_region_id": r2.id, "active": True, "ltl_capable": True, "ftl_capable": True})
        slevel = cls.SLevel.create({"code": "SCHED_TEST", "name": "Schedule Test", "reefer_food_eligible": True})
        cls.offering = cls.SOffering.create({"lane_id": lane.id, "service_level_id": slevel.id, "temperature_mode": "dry", "shipment_type": "both"})

    def test_01_next_weekday_pickup(self):
        """Schedule returns next available weekday pickup."""
        self.Schedule.create({"service_offering_id": self.offering.id, "cutoff_time": 16.0, "pickup_monday": True, "pickup_tuesday": True, "pickup_wednesday": True, "pickup_thursday": True, "pickup_friday": True, "delivery_offset_type": "next_day", "active": True})
        svc = ScheduleService(self.env)
        # Test before cutoff on a Monday
        ref = datetime(2026, 7, 27, 10, 0, tzinfo=BUSINESS_TZ)  # Monday
        result = svc.next_pickup_and_delivery(self.offering, ref)
        self.assertTrue(result.available)
        self.assertEqual(result.pickup_date, date(2026, 7, 27))  # Same day
        self.assertEqual(result.delivery_date, date(2026, 7, 28))  # Next day

    def test_02_after_cutoff_rolls_to_next_day(self):
        """After cutoff, pickup rolls to next available day."""
        self.Schedule.create({"service_offering_id": self.offering.id, "cutoff_time": 16.0, "pickup_monday": True, "pickup_tuesday": True, "pickup_wednesday": True, "pickup_thursday": True, "pickup_friday": True, "delivery_offset_type": "next_day", "active": True})
        svc = ScheduleService(self.env)
        ref = datetime(2026, 7, 27, 17, 0, tzinfo=BUSINESS_TZ)  # Monday after 4 PM
        result = svc.next_pickup_and_delivery(self.offering, ref)
        self.assertTrue(result.available)
        self.assertEqual(result.pickup_date, date(2026, 7, 28))  # Tuesday

    def test_03_weekend_skip(self):
        """Weekend pickup not available, rolls to Monday."""
        self.Schedule.create({"service_offering_id": self.offering.id, "cutoff_time": 16.0, "pickup_monday": True, "pickup_tuesday": False, "pickup_wednesday": False, "pickup_thursday": False, "pickup_friday": False, "delivery_offset_type": "next_day", "active": True})
        svc = ScheduleService(self.env)
        ref = datetime(2026, 8, 1, 10, 0, tzinfo=BUSINESS_TZ)  # Saturday
        result = svc.next_pickup_and_delivery(self.offering, ref)
        self.assertTrue(result.available)
        self.assertEqual(result.pickup_date.weekday(), 0)  # Monday

    def test_04_no_schedule_returns_not_configured(self):
        """No active schedule returns not_configured."""
        svc = ScheduleService(self.env)
        result = svc.next_pickup_and_delivery(self.offering)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "not_configured")

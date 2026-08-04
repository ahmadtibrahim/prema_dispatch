"""Focused regression tests for the corridor/Planner unification release."""

from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "prema_v5")
class TestCorridorPricingAndSchedule(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Region = cls.env["logistics.region"]
        cls.Corridor = cls.env["logistics.corridor"]
        cls.Stop = cls.env["logistics.corridor.stop"]
        cls.Departure = cls.env["logistics.corridor.departure"]

    def _corridor(self, **values):
        base = {
            "name": "V5 Corridor Test",
            "direction": "round_trip",
            "rate_per_km": 4.0,
            "planned_pallets": 6,
            "minimum_booking_charge": 150.0,
            "departure_horizon_weeks": 8,
        }
        base.update(values)
        return self.Corridor.with_context(skip_departure_reconcile=True).create(base)

    def test_corridor_is_customer_pricing_authority(self):
        region = self.Region.create({"code": "V5PRICE", "name": "V5 Pricing Region"})
        corridor = self._corridor(same_day_return=True)
        self.Stop.create({
            "corridor_id": corridor.id,
            "sequence": 10,
            "region_id": region.id,
            "distance_from_origin_km": 110.0,
        })
        corridor.invalidate_recordset()
        self.assertAlmostEqual(corridor.pallet_rate_per_km, 4.0 / 6.0, places=6)
        self.assertEqual(corridor.full_distance_km, 220.0)
        self.assertEqual(corridor.full_revenue_target, 880.0)
        self.assertEqual(corridor.minimum_booking_charge, 150.0)

    def test_eight_week_departure_horizon_and_weekday_rebuild(self):
        corridor = self._corridor(
            direction="eastbound",
            operate_tuesday=True,
            start_time=0.0,
        )
        start = date(2030, 1, 7)  # Monday
        summary = corridor._reconcile_departure_horizon(today=start)
        departures = self.Departure.search([
            ("corridor_id", "=", corridor.id),
            ("status", "=", "scheduled"),
        ])
        self.assertEqual(summary["created"], 8)
        self.assertEqual(len(departures), 8)
        self.assertTrue(all(item.departure_date.weekday() == 1 for item in departures))
        self.assertTrue(all(item.departure_time == 0.0 for item in departures))

        corridor.with_context(skip_departure_reconcile=True).write({
            "operate_tuesday": False,
            "operate_friday": True,
        })
        corridor._reconcile_departure_horizon(today=start)
        rebuilt = self.Departure.search([
            ("corridor_id", "=", corridor.id),
            ("status", "=", "scheduled"),
        ])
        self.assertEqual(len(rebuilt), 8)
        self.assertTrue(all(item.departure_date.weekday() == 4 for item in rebuilt))


@tagged("post_install", "-at_install", "prema_v5")
class TestRecurringAgreementJobs(TransactionCase):
    def test_agreement_accepts_ten_jobs_and_rejects_eleventh(self):
        partner = self.env["res.partner"].create({"name": "V5 Recurring Customer"})
        pickup = self.env["logistics.region"].create({
            "code": "V5RPU", "name": "V5 Pickup", "is_official_ltl_region": True,
        })
        delivery = self.env["logistics.region"].create({
            "code": "V5RDO", "name": "V5 Delivery", "is_official_ltl_region": True,
        })
        agreement = self.env["logistics.recurring.agreement"].create({
            "partner_id": partner.id,
            "start_date": date(2030, 1, 1),
            "end_date": date(2030, 12, 31),
        })
        Job = self.env["logistics.recurring.job"]
        values = {
            "agreement_id": agreement.id,
            "pickup_kind": "region",
            "pickup_region_id": pickup.id,
            "delivery_kind": "region",
            "delivery_region_id": delivery.id,
            "pallets": 1,
            "weight_lbs": 500.0,
        }
        for number in range(10):
            Job.create(dict(values, name=f"Recurring route {number + 1}"))
        self.assertEqual(agreement.job_count, 10)
        with self.assertRaises(ValidationError):
            Job.create(dict(values, name="Recurring route 11"))

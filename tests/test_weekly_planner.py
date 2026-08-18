"""Phase 7 — Weekly Capacity Planner + recurring integration (spec §40-§47).

The planner is operational truck/day allocation layered on top of the
existing recurring agreement/job system: cards are one occurrence each,
dragging reserves capacity, one-off changes never touch the agreement,
actual bookings generate a configurable number of days before departure,
holidays/blackouts block occurrences, and FTL cards hold the whole truck.
"""
import datetime

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWeeklyPlanner(TransactionCase):

    def setUp(self):
        super().setUp()
        self.plan_env = self.env(context=dict(
            self.env.context, skip_departure_reconcile=True))
        self.Plan = self.env["logistics.weekly.plan"]
        self.Reservation = self.env["logistics.weekly.plan.reservation"]
        self.Job = self.env["logistics.recurring.job"]
        self.Agreement = self.env["logistics.recurring.agreement"]

        # This week, Monday.
        self.week_start = fields.Date.today() - datetime.timedelta(
            days=fields.Date.today().weekday())
        self.friday = self.week_start + datetime.timedelta(days=4)

        # ── truck with canonical legacy capacities (12/13/14) ─────────
        brand = self.env["fleet.vehicle.model.brand"].search(
            [], limit=1) or self.env["fleet.vehicle.model.brand"].create(
            {"name": "PLAN7-BRAND"})
        model = self.env["fleet.vehicle.model"].create(
            {"name": "PLAN7-MODEL", "brand_id": brand.id})
        self.truck = self.env["fleet.vehicle"].create({
            "name": "PLAN7-Truck", "license_plate": "PLAN7T",
            "model_id": model.id,
            "x_operational_logistics": True, "x_max_payload_lbs": 40000.0,
            "straight_pallet_capacity": 12,
            "pin_wheel_pallet_capacity": 13,
            "turned_pallet_capacity": 14,
        })

        # ── corridor + regions + departure ────────────────────────────
        self.region_a = self.env["logistics.region"].create(
            {"code": "PL7-A", "name": "PLAN7 Region A"})
        self.region_b = self.env["logistics.region"].create(
            {"code": "PL7-B", "name": "PLAN7 Region B"})
        # Corridor and departure created with skip_departure_reconcile so the
        # corridor create does not auto-generate this Friday's horizon row and
        # collide with the explicit departure below (see test_vehicle_capacity).
        self.corridor = self.plan_env["logistics.corridor"].create({
            "name": "PLAN7-Corridor", "direction": "eastbound",
            "operate_friday": True, "start_time": 7.0,
            "default_vehicle_id": self.truck.id,
        })
        self.env["logistics.corridor.stop"].create([
            {"corridor_id": self.corridor.id, "sequence": 10,
             "region_id": self.region_a.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 0.0},
            {"corridor_id": self.corridor.id, "sequence": 20,
             "region_id": self.region_b.id,
             "pickup_allowed": True, "delivery_allowed": True,
             "distance_from_origin_km": 100.0},
        ])
        # Self-contained FSA→region mapping: location-kind jobs below ride
        # THIS corridor regardless of what the DB's FSA table contains. The
        # production clone has real FSAs (K7M→Quebec City Region, H1A→Greater
        # Montreal) with no corridor between them, so real postal codes would
        # make corridor resolution fail here. A7A/Z7Z are unpopulated in the
        # clone's FSA table (verified), keeping the fixture deterministic on
        # any database.
        self.env["logistics.fsa"].create([
            {"fsa": "A7A", "region_id": self.region_a.id},
            {"fsa": "Z7Z", "region_id": self.region_b.id},
        ])
        self.departure = self.plan_env["logistics.corridor.departure"].create({
            "corridor_id": self.corridor.id,
            "departure_date": self.friday,
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": self.truck.id,
        })

        # ── recurring customer: 3 pallets every Friday ────────────────
        self.customer = self.env["res.partner"].create(
            {"name": "PLAN7 Customer", "is_company": True})
        self.agreement = self.Agreement.create({
            "partner_id": self.customer.id,
            "start_date": self.week_start - datetime.timedelta(days=60),
            "end_date": self.week_start + datetime.timedelta(days=180),
        })
        self.job = self.Job.create({
            "agreement_id": self.agreement.id,
            "name": "PLAN7 Friday Route",
            "pickup_kind": "region",
            "pickup_region_id": self.region_a.id,
            "delivery_kind": "region",
            "delivery_region_id": self.region_b.id,
            "frequency": "weekly",
            "preferred_weekday": "4",  # Friday
            "pallets": 3,
            "weight_lbs": 1500.0,
        })
        self.agreement.action_activate()

        self.plan = self.Plan.create({"week_start": self.week_start})
        self.plan.corridor_ids = [(6, 0, [self.corridor.id])]

    # ── helpers ─────────────────────────────────────────────────────

    def _card(self):
        self.plan.action_generate_week()
        return self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", self.job.id),
        ], limit=1)

    # ── §40/§41 week assembly ───────────────────────────────────────

    def test_01_generate_week_creates_one_card_per_occurrence(self):
        self.plan.action_generate_week()
        cards = self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", self.job.id)])
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.plan_date, self.friday)
        self.assertEqual(card.pallets, 3)       # defaults from the job
        self.assertEqual(card.weight_lbs, 1500.0)
        self.assertEqual(card.load_type, "ltl")
        self.assertEqual(card.state, "planned")
        self.assertFalse(card.vehicle_id)       # unassigned until dragged

    def test_02_generate_week_is_idempotent_and_walks_occurrences(self):
        self.plan.action_generate_week()
        self.plan.action_generate_week()
        self.assertEqual(
            self.Reservation.search_count([
                ("plan_id", "=", self.plan.id),
                ("recurring_job_id", "=", self.job.id)]),
            1)
        # biweekly job: dates must match the job's own occurrence walk
        biweekly = self.Job.create({
            "agreement_id": self.agreement.id,
            "name": "PLAN7 Biweekly",
            "pickup_kind": "region",
            "pickup_region_id": self.region_a.id,
            "delivery_kind": "region",
            "delivery_region_id": self.region_b.id,
            "frequency": "biweekly",
            "preferred_weekday": "1",  # Tuesday
            "pallets": 2,
            "weight_lbs": 1000.0,
        })
        start = self.week_start
        end = start + datetime.timedelta(days=6)
        expected = []
        occ = biweekly._next_occurrence(start)
        while occ <= end:
            expected.append(occ)
            occ = biweekly._next_occurrence(occ + datetime.timedelta(days=1))
        self.plan.action_generate_week()
        bi_cards = self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", biweekly.id),
        ])
        self.assertEqual(
            sorted(c.plan_date for c in bi_cards),
            sorted(expected))

    # ── §43/§45 one-off changes never touch the agreement ───────────

    def test_03_one_off_change_does_not_modify_agreement(self):
        card = self._card()
        card.write({
            "plan_date": self.friday + datetime.timedelta(days=1),  # → Sat
            "pallets": 6,          # this week only
            "vehicle_id": self.truck.id,
            "change_note": "Frontline needed 6 pallets this week",
        })
        self.job.invalidate_recordset()
        self.assertEqual(self.job.pallets, 3)
        self.assertEqual(self.job.preferred_weekday, "4")
        self.assertEqual(self.agreement.start_date,
                         self.week_start - datetime.timedelta(days=60))
        self.assertEqual(card.plan_date,
                         self.friday + datetime.timedelta(days=1))
        self.assertEqual(card.state, "planned")
        # the NEXT regular occurrence is unaffected
        next_plan = self.Plan.create({
            "week_start": self.week_start + datetime.timedelta(days=7)})
        next_plan.action_generate_week()
        next_cards = self.Reservation.search([
            ("plan_id", "=", next_plan.id),
            ("recurring_job_id", "=", self.job.id),
        ])
        self.assertEqual(len(next_cards), 1)
        self.assertEqual(next_cards[0].plan_date,
                         self.friday + datetime.timedelta(days=7))
        self.assertEqual(next_cards[0].pallets, 3)

    # ── §40 capacity grid ───────────────────────────────────────────

    def test_04_capacity_grid_shows_committed_and_available(self):
        card = self._card()
        card.vehicle_id = self.truck.id  # dispatcher drags card onto truck
        self.plan.action_refresh_grid()
        cell = self.env["logistics.weekly.plan.day"].search([
            ("plan_id", "=", self.plan.id),
            ("plan_date", "=", self.friday),
        ], limit=1)
        self.assertTrue(cell)
        self.assertEqual(cell.vehicle_id, self.truck)
        self.assertEqual(cell.capacity_pallets, 13)   # canonical max layout
        self.assertEqual(cell.committed_pallets, 3)
        self.assertEqual(cell.available_pallets, 10)
        # no grid cells outside the corridor's operating days
        self.assertEqual(
            self.env["logistics.weekly.plan.day"].search_count(
                [("plan_id", "=", self.plan.id)]), 1)

    def test_05_grid_cell_flags_holiday(self):
        calendar = self.env["logistics.holiday.calendar"].create(
            {"name": "PLAN7 Holidays"})
        self.env["logistics.holiday.calendar.line"].create({
            "calendar_id": calendar.id, "date": self.friday,
            "description": "Civic Holiday",
        })
        self.corridor.holiday_calendar_ids = [(6, 0, [calendar.id])]
        self.plan.action_refresh_grid()
        cell = self.env["logistics.weekly.plan.day"].search([
            ("plan_id", "=", self.plan.id),
            ("plan_date", "=", self.friday),
        ], limit=1)
        self.assertTrue(cell.is_holiday)

    # ── §42/§47 capacity authority integration ──────────────────────

    def test_06_reservation_reduces_portal_capacity(self):
        from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
            VehicleCapacityService)
        service = VehicleCapacityService(self.env)
        card = self._card()
        card.vehicle_id = self.truck.id
        result = service.evaluate(self.truck, self.departure, 0)
        self.assertEqual(result["reserved_pallets"], 3)
        self.assertEqual(result["remaining_sellable_capacity"], 10)
        self.assertFalse(result["exclusive_vehicle_reserved"])
        # portal overbooking refused…
        refused = service.check_and_reserve(self.departure, 11, 5500.0, "ltl")
        self.assertFalse(refused["capacity_valid"])
        # …exactly the remaining positions accepted
        accepted = service.check_and_reserve(self.departure, 10, 5000.0, "ltl")
        self.assertTrue(accepted["capacity_valid"])
        # departure display surface sees the same number
        self.departure._compute_capacity_display()
        self.assertEqual(self.departure.capacity_reserved_pallets, 3)
        self.assertEqual(self.departure.remaining_sellable_capacity, 10)

    def test_07_ftl_card_holds_the_whole_vehicle(self):
        from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
            VehicleCapacityService)
        service = VehicleCapacityService(self.env)
        card = self._card()
        card.write({"vehicle_id": self.truck.id, "load_type": "ftl"})
        result = service.evaluate(self.truck, self.departure, 0)
        self.assertTrue(result["exclusive_vehicle_reserved"])
        self.assertEqual(result["remaining_sellable_capacity"], 0)
        self.assertIn(card.id, result["exclusive_reservation_ids"])
        refused = service.check_and_reserve(self.departure, 1, 500.0, "ltl")
        self.assertFalse(refused["capacity_valid"])
        self.departure._compute_capacity_display()
        self.assertTrue(self.departure.exclusive_vehicle_reserved)
        self.assertIn(card.name, self.departure.exclusive_booking_ref)

    # ── §44 booking generation ──────────────────────────────────────

    def _location(self, name, postal, city, province):
        return self.env["prema.dispatch.location"].create({
            "name": name,
            "business_name": name,
            "address": "%s Main St" % name,
            "street": "%s Main St" % name,
            "city": city,
            "province_code": province,
            "postal_code": postal,
            "google_verified": True,
            "google_place_id": "ChIJPLAN7-%s" % name.replace(" ", ""),
        })

    def _generation_job(self):
        pickup = self._location("PLAN7 Pickup", "A7A 0A0", "Ottawa", "ON")
        delivery = self._location("PLAN7 Delivery", "Z7Z 9Z9", "Montreal", "QC")
        job = self.Job.create({
            "agreement_id": self.agreement.id,
            "name": "PLAN7 Generated Friday Route",
            "pickup_kind": "location",
            "pickup_location_id": pickup.id,
            "delivery_kind": "location",
            "delivery_location_id": delivery.id,
            "frequency": "weekly",
            "preferred_weekday": "4",
            "pallets": 3,
            "weight_lbs": 1500.0,
            "auto_generate": True,
        })
        self.agreement.action_activate()
        return job

    def test_08_due_booking_generates_n_days_before(self):
        job = self._generation_job()
        self.plan.generate_days_before = 5
        self.plan.action_generate_week()
        card = self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", job.id),
        ], limit=1)
        # one-off move: this week's occurrence rides TODAY so the shared
        # (recurring_job_id, pickup_date) dedup key can be exercised. The
        # booking rides the corridor's scheduled departure, so give the
        # corridor a same-day departure too — otherwise the booking lands on
        # the fixture's Friday departure and pickup_date != plan_date.
        self.plan_env["logistics.corridor.departure"].create({
            "corridor_id": self.corridor.id,
            "departure_date": fields.Date.today(),
            "departure_time": 7.0,
            "status": "scheduled",
            "vehicle_id": self.truck.id,
        })
        card.plan_date = fields.Date.today()
        self.assertTrue(card.is_due)
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 0)
        self.plan._generate_due_bookings()
        booking = self.env["logistics.booking"].search(
            [("recurring_job_id", "=", job.id)])
        self.assertEqual(len(booking), 1)
        self.assertEqual(booking.pickup_date, card.plan_date)
        self.assertEqual(booking.pallets, card.pallets)
        self.assertEqual(card.state, "booking_generated")
        self.assertEqual(card.booking_id, booking)
        # idempotent: running again creates nothing new
        self.plan._generate_due_bookings()
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 1)
        # the JOB's own generator (logistics.recurring.job) deduplicates
        # against the same date — whichever runs first wins
        job.next_shipment_date = card.plan_date
        self.assertFalse(job._generate_if_due())
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 1)

    def test_09_not_due_waits_and_manual_force_generates(self):
        job = self._generation_job()
        self.plan.generate_days_before = 2  # plan_date today+3 → NOT due
        self.plan.action_generate_week()
        card = self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", job.id),
        ], limit=1)
        self.assertFalse(card.is_due)
        self.assertFalse(card._generate_booking())
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 0)
        # a dispatcher can still force-generate now (§44 manual override)
        self.assertTrue(card._generate_booking(force=True))
        self.assertEqual(card.state, "booking_generated")
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 1)

    # ── §46 holiday/blackout ────────────────────────────────────────

    def test_10_holiday_blocks_occurrence_and_generation(self):
        job = self._generation_job()
        self.plan.generate_days_before = 5
        calendar = self.env["logistics.holiday.calendar"].create(
            {"name": "PLAN7 Holidays"})
        self.env["logistics.holiday.calendar.line"].create({
            "calendar_id": calendar.id, "date": self.friday,
        })
        self.corridor.holiday_calendar_ids = [(6, 0, [calendar.id])]
        self.plan.action_generate_week()
        card = self.Reservation.search([
            ("plan_id", "=", self.plan.id),
            ("recurring_job_id", "=", job.id),
        ], limit=1)
        self.assertTrue(card.is_blocked)
        self.assertIn("Holiday", card.blocked_reason)
        self.assertIsNone(card._generate_booking(force=True))
        self.assertEqual(
            self.env["logistics.booking"].search_count(
                [("recurring_job_id", "=", job.id)]), 0)

    # ── §45 one-off cancel frees capacity, next week continues ──────

    def test_11_cancel_occurrence_frees_capacity(self):
        from odoo.addons.prema_logistics_booking.services.vehicle_capacity_service import (
            VehicleCapacityService)
        service = VehicleCapacityService(self.env)
        card = self._card()
        card.vehicle_id = self.truck.id
        self.assertEqual(
            service.evaluate(self.truck, self.departure, 0)[
                "remaining_sellable_capacity"], 10)
        card.action_cancel_occurrence()
        self.assertEqual(card.state, "cancelled")
        self.assertEqual(
            service.evaluate(self.truck, self.departure, 0)[
                "remaining_sellable_capacity"], 13)
        # next week's occurrence still planned normally
        next_plan = self.Plan.create({
            "week_start": self.week_start + datetime.timedelta(days=7)})
        next_plan.action_generate_week()
        next_cards = self.Reservation.search([
            ("plan_id", "=", next_plan.id),
            ("recurring_job_id", "=", self.job.id),
        ])
        self.assertEqual(len(next_cards), 1)
        self.assertEqual(next_cards[0].state, "planned")

    # ── §43 week start validation ───────────────────────────────────

    def test_12_week_start_must_be_monday(self):
        with self.assertRaises(ValidationError):
            self.Plan.create(
                {"week_start": self.friday})

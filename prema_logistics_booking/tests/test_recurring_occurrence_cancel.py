"""MP1 D-B3 (§17) recurring occurrence cancellation — verification.

A cancelled GENERATED booking is a dispatcher's deliberate one-off skip:
the occurrence is occupied, so neither generator may resurrect it on a
same-day re-run, and the agreement/job continue normally afterwards.

  C1  logistics.recurring.job._generate_if_due: a cancelled booking for
      (job, pickup_date) blocks regeneration (returns False, no new
      booking); the booking's action_cancel trail (reason + source) is
      intact and the agreement/job are untouched.
  C2  weekly-plan card _generate_booking adopts the cancelled booking
      (booking_id + booking_generated) instead of rebooking, and the new
      booking_cancelled board marker (§16) is set.
  C3  action_cancel_occurrence follows the booking: an ACTIVE generated
      booking still refuses card cancellation (cancel the booking
      first); a card whose booking was already cancelled may be marked
      cancelled.

No real generation runs (no orchestration/network): every path below is
the pre-generation occupancy check.
"""

import datetime

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestRecurringOccurrenceCancel(TransactionCase):
    """§17 recurring occurrence cancel semantics."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create(
            {"name": "D-B3 Recurring Customer"})

        def _loc(name, street):
            return cls.env["prema.dispatch.location"].create({
                "name": name, "address": street, "street": street,
                "city": "Ayr", "province_code": "ON", "postal_code": "N0B 1E0",
                "pin_lat": 43.29, "pin_lng": -80.45,
                "google_verified": True, "google_place_id": name,
            })

        cls.loc_pickup = _loc("D-B3 Rec Pickup", "1406 Test Line 8, Ayr, ON")
        cls.loc_delivery = _loc("D-B3 Rec Delivery",
                                "2277 Test Sideroad 15, Ayr, ON")

        today = datetime.date.today()
        cls.today = today

        # WEEKDAYS on the job runs Monday–Saturday ('0'–'5'): a Sunday
        # run must not feed weekday()==6. The occurrence under test is
        # pinned via next_shipment_date (the due anchor), so the
        # weekday only shapes future _next_occurrence rolls.
        weekday = today.weekday()
        if weekday == 6:  # Sunday — not a lane weekday
            weekday = 0

        cls.agreement = cls.env["logistics.recurring.agreement"].create({
            "partner_id": cls.partner.id,
            "start_date": today - datetime.timedelta(days=60),
            "end_date": today + datetime.timedelta(days=400),
            "state": "active", "active": True,
        })
        cls.job = cls.env["logistics.recurring.job"].create({
            "agreement_id": cls.agreement.id,
            "name": "D-B3 Weekly Lane",
            "pickup_location_id": cls.loc_pickup.id,
            "delivery_location_id": cls.loc_delivery.id,
            "frequency": "weekly",
            "preferred_weekday": str(weekday),
            "next_shipment_date": today,
            "auto_generate": True,
            "pallets": 2, "weight_lbs": 800.0,
        })

    def _cancelled_booking(self, pickup_date, reason="carrier no-show"):
        """A generated booking for (job, pickup_date) cancelled through
        the booking's own action_cancel (the commercial cancel path)."""
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "recurring_job_id": self.job.id,
            "pickup_date": pickup_date,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 2, "weight_lbs": 800.0,
            "calculated_price": 250.0,
        })
        booking.action_cancel(reason=reason, source="company")
        return booking

    # ── C1: recurring job generator ────────────────────────────────

    def test_c1_cancelled_occurrence_blocks_regeneration(self):
        """_generate_if_due on the same due date must not resurrect a
        cancelled occurrence; agreement and job roll on untouched."""
        job = self.job
        booking = self._cancelled_booking(self.today)
        self.assertEqual(booking.state, "cancelled")
        self.assertEqual(booking.cancellation_reason, "carrier no-show")
        self.assertEqual(booking.cancellation_source, "company")

        # Same-day re-run: blocked by the occupancy of ANY booking —
        # cancelled included (pre-§17 this re-generated the occurrence).
        result = job._generate_if_due()
        self.assertFalse(result, "cancelled occurrence must not regenerate")
        bookings = self.env["logistics.booking"].sudo().search([
            ("recurring_job_id", "=", job.id),
            ("pickup_date", "=", self.today),
        ])
        self.assertEqual(len(bookings), 1,
                         "exactly the cancelled booking — no resurrection")

        # Agreement untouched: still active; the job still carries its
        # next occurrence (the generator rolls past the skip naturally).
        self.assertEqual(self.agreement.state, "active")
        self.assertTrue(self.agreement.active)
        self.assertTrue(job.active)
        self.assertEqual(job.next_shipment_date, self.today)

    # ── C2/C3: weekly-plan card path ───────────────────────────────

    def _plan_and_card(self, plan_date):
        plan = self.env["logistics.weekly.plan"].create({
            "week_start": plan_date - datetime.timedelta(
                days=plan_date.weekday()),
            "generate_days_before": 0,
            "state": "confirmed",
        })
        card = self.env["logistics.weekly.plan.reservation"].create({
            "plan_id": plan.id,
            "recurring_job_id": self.job.id,
            "plan_date": plan_date,
        })
        return plan, card

    def test_c2_cancelled_booking_adopted_and_marked(self):
        """The card adopts the cancelled booking (never rebooks) and the
        booking_cancelled board marker (§16) is set."""
        _, card = self._plan_and_card(self.today)
        booking = self._cancelled_booking(self.today, reason="skip week")

        result = card._generate_booking(force=True)
        self.assertFalse(result, "no new booking for a skipped occurrence")
        self.assertEqual(card.state, "booking_generated")
        self.assertEqual(card.booking_id.id, booking.id,
                         "the card ADOPTS the cancelled booking")
        self.assertTrue(card.booking_cancelled,
                         "board marker distinguishes the cancelled booking")
        self.assertEqual(
            self.env["logistics.booking"].search_count([
                ("recurring_job_id", "=", self.job.id),
                ("pickup_date", "=", self.today)]), 1)

        # The dispatcher can now mark the card cancelled to acknowledge
        # the skip (relaxed action_cancel_occurrence, §17).
        card.action_cancel_occurrence()
        self.assertEqual(card.state, "cancelled")
        self.assertTrue(card.change_note)
        self.env.invalidate_all()
        self.assertEqual(booking.state, "cancelled")

    def test_c3_active_booking_still_refuses_card_cancel(self):
        """An ACTIVE generated booking cannot be cancelled via the card —
        the booking is the commercial record; the marker stays off."""
        other_day = self.today + datetime.timedelta(days=7)
        _, card = self._plan_and_card(other_day)
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "recurring_job_id": self.job.id,
            "pickup_date": other_day,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 2, "weight_lbs": 800.0,
            "calculated_price": 250.0,
        })  # state confirmed — active, NOT cancelled

        result = card._generate_booking(force=True)
        self.assertFalse(result)
        self.assertEqual(card.state, "booking_generated")
        self.assertEqual(card.booking_id.id, booking.id)
        self.assertFalse(card.booking_cancelled,
                         "marker only for cancelled bookings")

        with self.assertRaises(UserError):
            card.action_cancel_occurrence()
        self.assertEqual(card.state, "booking_generated",
                         "refused: the active booking must be cancelled "
                         "through the booking itself")
        # Cancelling the booking then makes the marker + the card follow.
        booking.action_cancel(reason="route change", source="company")
        self.assertTrue(card.booking_cancelled)
        card.action_cancel_occurrence()
        self.assertEqual(card.state, "cancelled")

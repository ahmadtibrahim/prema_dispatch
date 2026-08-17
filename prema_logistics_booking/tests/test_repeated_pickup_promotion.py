"""Repeated-pickup booking → one dispatch job (Manual-UAT audit 21/22).

A booking covering a load with a repeated pickup at the same warehouse
(Ajax: pickup 12 → Oshawa 12, pickup 6 → Whitby 1 — the
D-AJX-OSH-WHI-NCL-PTB-CAM-FOX-070624 / D-WOO-BEL-110624 audit report)
must promote to EXACTLY ONE dispatch job, and the same warehouse may
appear multiple times as stops inside that one job.

These tests live in the prema_logistics_booking suite (not
prema_dispatch's) because the promotion code is the booking module's
`_create_dispatch_job()` bridge — dispatch's own test phase loads before
the booking module and can never see `logistics.booking` in the
registry (prema_logistics_booking depends ON prema_dispatch, so its
tests run with both modules loaded).
"""
from odoo.tests.common import TransactionCase


class TestRepeatedPickupPromotion(TransactionCase):
    """Audit tests 21-22: one job per booking, repeated pickups as stops."""

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create(
            {"name": "Repeated Pickup Customer"})

    def _make_repeated_pickup_booking(self):
        """Ajax split-route booking (pickup 12 → Oshawa 12, pickup 6 →
        Whitby 1) as a movement_v1 booking — the canonical source for
        dispatch jobs since the estimator→job promotion path was removed
        (estimators now live on the quote side)."""
        ajax = self.env["prema.dispatch.location"].create({
            "name": "Ajax Test Warehouse",
            "business_name": "Ajax Test Warehouse",
            "address": "689 Salem Rd N, Ajax, ON",
            "location_type": "warehouse",
        })
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "booking_number": "AUD-4-REPEAT-01",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": 2000.0,
            "state": "confirmed",
            "calculated_price": 400.0,
            "route_model_version": "movement_v1",
        })
        pick1 = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
            "location_name": "Ajax Test Warehouse", "saved_location_id": ajax.id})
        drop1 = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
            "location_name": "Carolyn's Liquidations #2"})
        pick2 = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 30, "stop_type": "pickup",
            "location_name": "Ajax Test Warehouse", "saved_location_id": ajax.id})
        drop2 = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 40, "stop_type": "delivery",
            "location_name": "Foodland Pringle Creek"})
        Pallet = self.env["logistics.booking.pallet"]
        Allocation = self.env["logistics.booking.pallet.stop.allocation"]
        for i in range(2):
            p = Pallet.create({
                "booking_id": booking.id, "sequence": 10 + i, "label": "A-%02d" % (i + 1),
                "weight_lbs": 500.0, "pickup_stop_id": pick1.id})
            Allocation.create({
                "pallet_id": p.id, "delivery_stop_id": drop1.id, "unload_sequence": 10})
        for i in range(2):
            p = Pallet.create({
                "booking_id": booking.id, "sequence": 30 + i, "label": "B-%02d" % (i + 1),
                "weight_lbs": 500.0, "pickup_stop_id": pick2.id})
            Allocation.create({
                "pallet_id": p.id, "delivery_stop_id": drop2.id, "unload_sequence": 10})
        return booking, ajax

    def test_01_book_load_dedupes_repeated_pickup(self):
        """Audit test 21: one booking covering a load with a repeated
        pickup at the same warehouse (Ajax) must produce exactly ONE
        dispatch job, and re-promoting must never create a second one —
        the dedup that used to live in the estimator→job promotion (the
        Ajax invoice bug) is now structurally prevented: bookings are the
        single source."""
        booking, _ajax = self._make_repeated_pickup_booking()

        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertEqual(
            len(booking.dispatch_job_ids), 1,
            "Repeated pickup from the same warehouse must produce one dispatch job, not two",
        )
        again = booking._create_dispatch_job()
        self.assertEqual(
            again.id, job.id,
            "Re-promoting the same booking must return the same dispatch job",
        )
        self.assertEqual(len(booking.dispatch_job_ids), 1)

    def test_02_promoted_job_keeps_repeated_pickup_as_stops(self):
        """Audit test 22: the same warehouse can appear multiple times as
        stops inside the one promoted dispatch job."""
        booking, ajax = self._make_repeated_pickup_booking()

        job = booking._create_dispatch_job()
        self.assertEqual(len(booking.dispatch_job_ids), 1)
        pickups_at_ajax = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup" and s.saved_location_id.id == ajax.id
        )
        self.assertEqual(
            len(pickups_at_ajax), 2,
            "Same warehouse should appear as 2 pickup stops inside the one job",
        )

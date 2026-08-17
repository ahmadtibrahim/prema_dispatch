"""Shared pallet weight portions + reconciliation (Manual-UAT part 5).

A physical shared pallet consumes ONE truck position; its delivery
allocations carry the PORTION of weight delivered at each stop, and the
active portions must sum to the pallet's weight. PARTIALLY DELIVERED
stays onboard until the final allocation completes.

Runs in the prema_logistics_booking test phase (both modules loaded).
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestSharedPalletPortions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].search([], limit=1)

    def _booking_with_stops(self, n_deliveries=2, weight=2000.0):
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "booking_number": "SP-PORT-01",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": weight,
            "state": "confirmed",
            "calculated_price": 300.0,
            "route_model_version": "movement_v1",
        })
        pu = self.env["logistics.booking.stop"].create({
            "booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
            "location_name": "United Dairy", "city": "Brampton"})
        deliveries = []
        seq = 20
        for i in range(n_deliveries):
            deliveries.append(self.env["logistics.booking.stop"].create({
                "booking_id": booking.id, "sequence": seq,
                "stop_type": "delivery",
                "location_name": "Stop %d" % (i + 1),
                "city": "Belleville" if i == 0 else "Ottawa"}))
            seq += 10
        # Stable stop keys (created after the rows exist — the key embeds
        # the record id), matching the real confirmation flow.
        for stop in [pu] + deliveries:
            stop.stop_key = "s%d" % stop.id
        return booking, pu, deliveries

    def _create_pallets(self, booking, movements):
        BookingOrchestrationService(self.env)._create_booking_pallets(
            booking, movements)
        return booking.pallet_ids

    def _shared_movement(self, pu, deliveries, weight=2000.0,
                         delivery_weights=None, delivery_pieces=None):
        movement = {
            "key": "p1", "label": "S-01", "weight_lbs": weight,
            "shared": True, "pickup_stop_key": "s%d" % pu.id,
            "delivery_stop_keys": ["s%d" % d.id for d in deliveries],
        }
        if delivery_weights is not None:
            movement["delivery_weights"] = delivery_weights
        if delivery_pieces is not None:
            movement["delivery_pieces"] = delivery_pieces
        return movement

    # ── Portion defaults + validation ─────────────────────────────

    def test_01_shared_pallet_auto_splits_portion_weights(self):
        """A shared pallet created without entered portions is split
        evenly across its active deliveries — and the split always sums
        back to the pallet weight."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        pallets = self._create_pallets(
            booking, [self._shared_movement(pu, deliveries)])
        pallet = pallets[0]
        self.assertTrue(pallet.shared)
        self.assertEqual(len(pallet.delivery_allocation_ids), 2)
        allocs = pallet.delivery_allocation_ids
        total = sum(a.weight_lbs for a in allocs)
        self.assertEqual(round(total, 1), 2000.0)
        for a in allocs:
            self.assertEqual(a.weight_lbs, 1000.0)

    def test_02_explicit_portion_weights_are_honored(self):
        """Entered delivery_weights/delivery_pieces are kept as-is (the
        customer knows their split — the system never overrides it)."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        pallets = self._create_pallets(booking, [self._shared_movement(
            pu, deliveries, delivery_weights=[1200.0, 800.0],
            delivery_pieces=[60, 40])])
        allocs = pallets[0].delivery_allocation_ids.sorted("unload_sequence")
        self.assertEqual([a.weight_lbs for a in allocs], [1200.0, 800.0])
        self.assertEqual([a.piece_count for a in allocs], [60, 40])
        self.assertEqual(sum(a.weight_lbs for a in allocs), 2000.0)

    def test_03_portion_mismatch_is_rejected(self):
        """Portions that do not add up to the pallet weight are refused —
        the truck delivers exactly what it picked up."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        pallets = self._create_pallets(
            booking, [self._shared_movement(pu, deliveries)])
        alloc = pallets[0].delivery_allocation_ids[0]
        with self.assertRaises(ValidationError):
            alloc.weight_lbs = 1300.0  # 1300 + 1000 != 2000

    def test_04_deactivating_a_delivery_requires_resplit(self):
        """Removing one delivery from a weighted shared pallet must be
        accompanied by re-splitting the portions — a silent removal would
        leave weight unaccounted."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=3,
                                                           weight=3000.0)
        pallets = self._create_pallets(
            booking, [self._shared_movement(pu, deliveries, weight=3000.0)])
        allocs = pallets[0].delivery_allocation_ids
        for a in allocs:
            self.assertEqual(a.weight_lbs, 1000.0)
        with self.assertRaises(ValidationError):
            allocs[2].active = False  # 2000 remaining != 3000

    def test_05_legacy_zero_weight_allocations_stay_unchecked(self):
        """Pallets from before per-portion weights (all-zero allocations)
        are not blocked by the new constraint — deactivation and delivery
        writes keep working."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        pallet = self.env["logistics.booking.pallet"].create({
            "booking_id": booking.id, "sequence": 10, "label": "U-01",
            "weight_lbs": 2000.0, "shared": True, "pickup_stop_id": pu.id,
        })
        allocs = self.env["logistics.booking.pallet.stop.allocation"]
        for i, d in enumerate(deliveries):
            allocs |= self.env["logistics.booking.pallet.stop.allocation"].create({
                "pallet_id": pallet.id, "delivery_stop_id": d.id,
                "unload_sequence": (i + 1) * 10})
        # No weights entered anywhere → constraint sleeps.
        allocs[1].active = False
        allocs[0].delivered = True

    def test_06_single_delivery_pallet_takes_whole_weight(self):
        """A non-shared pallet's only allocation is the whole pallet."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=1)
        movement = self._shared_movement(pu, deliveries)
        movement["shared"] = False
        pallets = self._create_pallets(booking, [movement])
        alloc = pallets[0].delivery_allocation_ids[0]
        self.assertEqual(alloc.weight_lbs, 2000.0)

    # ── Dispatch bridge: one position + portion mirror ─────────────

    def test_07_shared_pallet_is_one_position_and_mirrors_portions(self):
        """The dispatch bridge creates ONE item (one truck position) for
        the shared pallet, and the dispatch stop allocations mirror the
        booking portions 1:1 by unload_sequence."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        self._create_pallets(booking, [self._shared_movement(
            pu, deliveries, delivery_weights=[1200.0, 800.0],
            delivery_pieces=[60, 40])])
        job = booking._create_dispatch_job()
        self.assertTrue(job)
        self.assertEqual(len(job.item_ids), 1,
                         "A shared pallet is ONE physical position")
        item = job.item_ids[0]
        self.assertEqual(item.shared_skid, True)
        self.assertEqual(item.load_unit_type, "shared_pallet")
        self.assertEqual(item.logistics_booking_pallet_id,
                         booking.pallet_ids[0])
        d_allocs = item.stop_allocation_ids.sorted("unload_sequence")
        b_allocs = booking.pallet_ids[0].delivery_allocation_ids.sorted(
            "unload_sequence")
        self.assertEqual(len(d_allocs), 2)
        # Reconciliation trace: booking pallet → dispatch item → dispatch
        # allocations carries the same portions at the same sequences.
        self.assertEqual(
            [a.unload_sequence for a in d_allocs],
            [a.unload_sequence for a in b_allocs])
        self.assertEqual([a.weight_lbs for a in d_allocs],
                         [1200.0, 800.0])
        self.assertEqual([a.piece_count for a in d_allocs], [60, 40])

    # ── PARTIALLY DELIVERED stays onboard ─────────────────────────

    def test_08_partially_delivered_stays_onboard_until_last_stop(self):
        """First delivery stop: pallet goes partially_delivered (stays on
        the truck, counted onboard). Final stop: delivered."""
        booking, pu, deliveries = self._booking_with_stops(n_deliveries=2)
        self._create_pallets(booking, [self._shared_movement(pu, deliveries)])
        job = booking._create_dispatch_job()
        pallet = booking.pallet_ids[0]
        self.assertEqual(pallet.state, "pending_pickup")
        # This test exercises the CUSTODY machine, not the proof UX —
        # clear the POP/POD gate so completion is not blocked by missing
        # attachments.
        job.stop_ids.write({"pop_required": False, "pod_required": False})

        # Pickup completes → onboard.
        pickup_stop = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")
        pickup_stop.action_mark_completed()
        self.assertEqual(pallet.state, "onboard")

        # First delivery completes → PARTIALLY DELIVERED, still onboard.
        stops = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        first = stops.sorted("sequence")[0]
        first.action_mark_completed()
        self.assertEqual(pallet.state, "partially_delivered")
        self.assertEqual(pallet.delivery_allocation_ids.filtered(
            "delivered"), pallet.delivery_allocation_ids[0:1])
        self.assertEqual(job.onboard_pallet_count, 1,
                         "Partially delivered pallet stays onboard")

        # Second delivery completes → fully delivered, off the truck.
        last = stops.sorted("sequence")[1]
        last.action_mark_completed()
        self.assertEqual(pallet.state, "delivered")
        self.assertTrue(all(pallet.delivery_allocation_ids.mapped("delivered")))
        self.assertEqual(job.onboard_pallet_count, 0)

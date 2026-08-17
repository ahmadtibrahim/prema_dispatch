"""Milk-run booking-side operations: shared-pallet custody, the
booking state machine, mixed physical visits, tracking privacy, and
multi-stop invoice descriptions with frozen pricing."""
import datetime

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestMilkRunOperationsBooking(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.partner = cls.env["res.partner"].search([], limit=1)

    def _confirm_booking(self, movements, stops=None, idem="x"):
        svc = BookingOrchestrationService(self.env)
        stops = stops or {
            "stp-ud": {"stop_key": "stp-ud", "stop_type": "pickup",
                       "company_name": "United Dairy", "postal_code": "X0A",
                       "city": "Kingston", "latitude": 44.23,
                       "longitude": -76.49},
            "stp-tf": {"stop_key": "stp-tf", "stop_type": "pickup",
                       "company_name": "TerraFreska", "postal_code": "X0B",
                       "city": "Brampton", "latitude": 43.73,
                       "longitude": -79.76},
            "stp-blv": {"stop_key": "stp-blv", "stop_type": "delivery",
                        "company_name": "Belleville Depot", "postal_code": "X0C",
                        "city": "Belleville", "latitude": 44.16,
                        "longitude": -77.38},
            "stp-ott": {"stop_key": "stp-ott", "stop_type": "delivery",
                        "company_name": "Ottawa DC", "postal_code": "X0D",
                        "city": "Ottawa", "latitude": 45.42,
                        "longitude": -75.70},
        }
        pickups = [s for s in stops.values() if s["stop_type"] == "pickup"]
        deliveries = [s for s in stops.values() if s["stop_type"] == "delivery"]
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pricing_method": "manual",
            "agreed_rate": 500.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "pallets": len(movements),
            "physical_pallets": len(movements),
            "weight_lbs": len(movements) * 500.0,
            "pickup_stops": pickups,
            "delivery_stops": deliveries,
            "route_model_version": "movement_v1",
            "pallet_movements": movements,
            "idempotency_key": "test:milkrun:ops:%s" % idem,
        }, source_channel="internal")
        return svc.confirm_from_internal(norm, skip_invoice=True)

    def _movements(self, shared_first=False):
        movements = []
        for i in range(3):
            movements.append({
                "key": "u%d" % (i + 1), "label": "U-%02d" % (i + 1),
                "weight_lbs": 500.0, "shared": False,
                "pickup_stop_key": "stp-ud", "delivery_stop_keys": ["stp-ott"],
            })
        if shared_first:
            movements[0]["shared"] = True
            movements[0]["delivery_stop_keys"] = ["stp-blv", "stp-ott"]
        for i in range(2):
            movements.append({
                "key": "t%d" % (i + 1), "label": "TF-%02d" % (i + 1),
                "weight_lbs": 400.0, "shared": False,
                "pickup_stop_key": "stp-tf", "delivery_stop_keys": ["stp-blv"],
            })
        return movements

    def _pod_att(self):
        return self.env["ir.attachment"].create({
            "name": "pod.jpg",
            "datas": "aGVsbG8=",
            "res_model": "prema.dispatch.stop",
        })

    # ── Shared pallet custody ───────────────────────────────────────

    def test_01_shared_pallet_partial_then_final_delivery(self):
        """U-01 is shared across Belleville + Ottawa. After Belleville it
        must be PARTIALLY delivered — never fully delivered — and only
        Ottawa completes custody."""
        movements = self._movements(shared_first=True)
        booking = self._confirm_booking(movements, idem="shared1")
        job = booking.dispatch_job_ids
        shared_pallet = booking.pallet_ids.filtered(
            lambda p: p.label == "U-01")
        self.assertEqual(len(shared_pallet.delivery_allocation_ids), 2)
        stops = job.stop_ids.sorted("sequence")
        blv = stops[2]  # Belleville dropoff
        ott = stops[3]  # Ottawa dropoff
        # Complete Belleville first (with POD).
        blv.write({"pod_attachment_ids": [(4, self._pod_att().id)]})
        blv.action_mark_completed()
        item = job.item_ids.filtered(
            lambda i: i.logistics_booking_pallet_id == shared_pallet)
        self.assertEqual(item.status, "partially_unloaded")
        # Booking pallet: partially delivered, first allocation done,
        # final allocation still pending.
        self.assertEqual(shared_pallet.state, "partially_delivered")
        blv_alloc = shared_pallet.delivery_allocation_ids.filtered(
            lambda a: a.delivery_stop_id.stop_key == "stp-blv")
        ott_alloc = shared_pallet.delivery_allocation_ids.filtered(
            lambda a: a.delivery_stop_id.stop_key == "stp-ott")
        self.assertTrue(blv_alloc.delivered)
        self.assertFalse(ott_alloc.delivered)
        # Final allocation at Ottawa completes custody.
        ott.write({"pod_attachment_ids": [(4, self._pod_att().id)]})
        ott.action_mark_completed()
        self.assertEqual(item.status, "delivered")
        self.assertEqual(shared_pallet.state, "delivered")
        self.assertTrue(ott_alloc.delivered)
        self.assertTrue(blv_alloc.delivered)

    # ── Booking state machine ───────────────────────────────────────

    def test_02_state_machine_confirmed_to_completed(self):
        movements = self._movements()
        booking = self._confirm_booking(movements, idem="states")
        self.assertEqual(booking.state, "confirmed")
        job = booking.dispatch_job_ids
        stops = job.stop_ids.sorted("sequence")
        ud, tf, blv, ott = stops
        # Dispatch exists → planned.
        booking.sync_state_from_dispatch()
        self.assertEqual(booking.state, "planned")
        # First pickup activity → in_execution.
        ud.action_mark_en_route()
        self.assertEqual(booking.state, "in_execution")
        # Pickups complete (POPs attached); deliveries not yet → still
        # in_execution.
        ud.write({"pop_attachment_ids": [(4, self._pod_att().id)]})
        tf.write({"pop_attachment_ids": [(4, self._pod_att().id)]})
        ud.action_mark_completed()
        tf.action_mark_completed()
        self.assertEqual(booking.state, "in_execution")
        # Deliveries complete with PODs → all deliveries done, but actuals
        # not yet confirmed → delivered (not completed).
        blv.write({"pod_attachment_ids": [(4, self._pod_att().id)]})
        ott.write({"pod_attachment_ids": [(4, self._pod_att().id)]})
        blv.action_mark_completed()
        ott.action_mark_completed()
        self.assertEqual(booking.state, "delivered")
        # Per-stop actuals confirmed → all operational/evidence
        # requirements satisfied → completed.
        ud.confirm_pickup_actuals(3, 1500.0)
        tf.confirm_pickup_actuals(2, 800.0)
        blv.confirm_delivery_actuals(2, 800.0)
        ott.confirm_delivery_actuals(3, 1500.0)
        self.assertEqual(booking.state, "completed")

    def test_03_legacy_booking_state_untouched_by_sync(self):
        """sync_state_from_dispatch never touches legacy bookings."""
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "booking_number": "MR-LEGACY-1",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 500.0,
            "state": "confirmed",
            "calculated_price": 100.0,
        })
        booking.sync_state_from_dispatch()
        self.assertEqual(booking.state, "confirmed")

    # ── Mixed physical visit ────────────────────────────────────────

    def test_04_mixed_visit_combines_pickup_and_delivery(self):
        # United Dairy facility hosts BOTH a pickup and a delivery stop.
        stops = {
            "stp-ud": {"stop_key": "stp-ud", "stop_type": "pickup",
                       "company_name": "United Dairy", "postal_code": "X0A",
                       "city": "Kingston", "latitude": 44.23,
                       "longitude": -76.49},
            "stp-ud-del": {"stop_key": "stp-ud-del", "stop_type": "delivery",
                           "company_name": "United Dairy", "postal_code": "X0A",
                           "city": "Kingston", "latitude": 44.23,
                           "longitude": -76.49},
            "stp-blv": {"stop_key": "stp-blv", "stop_type": "delivery",
                        "company_name": "Belleville Depot", "postal_code": "X0C",
                        "city": "Belleville", "latitude": 44.16,
                        "longitude": -77.38},
        }
        movements = [
            {"key": "u1", "label": "U-01", "weight_lbs": 500.0,
             "shared": False, "pickup_stop_key": "stp-ud",
             "delivery_stop_keys": ["stp-ud-del"]},
            {"key": "t1", "label": "TF-01", "weight_lbs": 400.0,
             "shared": False, "pickup_stop_key": "stp-ud",
             "delivery_stop_keys": ["stp-blv"]},
        ]
        booking = self._confirm_booking(movements, stops=stops, idem="mixed")
        job = booking.dispatch_job_ids
        dispatch_stops = job.stop_ids.sorted("sequence")
        ud_pickup = dispatch_stops[0]
        ud_delivery = dispatch_stops.filtered(
            lambda s: s.logistics_booking_stop_id.stop_key == "stp-ud-del")
        # Same physical facility → the two logical stops must share the
        # same saved location to be combined.
        loc = self.env["prema.dispatch.location"].create({
            "name": "United Dairy Facility",
            "address": "1 Dairy Way, Kingston, ON",
            "pin_lat": 44.23, "pin_lng": -76.49,
        })
        ud_pickup.write({"saved_location_id": loc.id})
        ud_delivery.write({"saved_location_id": loc.id})
        result = job.combine_physical_visit([ud_pickup.id, ud_delivery.id])
        self.assertTrue(result["success"])
        self.assertEqual(result["visit_type"], "mixed")
        visit = self.env["prema.dispatch.route.visit"].browse(
            result["route_visit_id"])
        self.assertEqual(visit.visit_type, "mixed")
        self.assertEqual(visit.mixed_action_order, "unload_then_load")
        self.assertEqual(len(visit.stop_link_ids), 2)

    # ── Tracking privacy ────────────────────────────────────────────

    def test_05_tracking_display_only_own_stops(self):
        movements = self._movements()
        booking = self._confirm_booking(movements, idem="privacy")
        display = booking._tracking_stops_display()
        self.assertEqual(len(display), 4)
        names = {d["name"] for d in display}
        self.assertIn("United Dairy", names)
        self.assertIn("Ottawa DC", names)
        for entry in display:
            # Every serialized stop belongs to THIS booking (booking stop
            # ids only) — no other customer's stop can appear.
            self.assertIn(
                entry["name"],
                {"United Dairy", "TerraFreska", "Belleville Depot",
                 "Ottawa DC"},
            )

    # ── Accounting: frozen price + multi-stop description ───────────

    def test_06_invoice_description_lists_all_stops_price_frozen(self):
        movements = self._movements()
        booking = self._confirm_booking(movements, idem="invoice")
        booking.write({"calculated_price": 777.77})
        invoice = booking._create_draft_invoice()
        self.assertTrue(invoice)
        description = booking._generate_invoice_description()
        self.assertIn("Pickup 1:", description)
        self.assertIn("Pickup 2:", description)
        self.assertIn("Delivery 1:", description)
        self.assertIn("Delivery 2:", description)
        self.assertIn("United Dairy", description)
        self.assertIn("Ottawa DC", description)
        total = sum(invoice.invoice_line_ids.mapped("price_subtotal"))
        self.assertAlmostEqual(total, 777.77, places=2)
        # Dispatcher reordering never reprices the invoice: re-sequence
        # dispatch stops and confirm the invoice total is unchanged.
        job = booking.dispatch_job_ids
        for index, stop in enumerate(reversed(job.stop_ids.sorted("sequence"))):
            stop.write({"sequence": (index + 1) * 10})
        invoice.invalidate_recordset(["invoice_line_ids"])
        total_after = sum(invoice.invoice_line_ids.mapped("price_subtotal"))
        self.assertAlmostEqual(total_after, 777.77, places=2)

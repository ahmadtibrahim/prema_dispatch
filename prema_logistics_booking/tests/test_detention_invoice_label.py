"""MP1 D-B3 (§18.6) detention invoice line — booking-module side.

The approved detention charge still appends ONE line to the booking's
existing DRAFT invoice, exclusively via action_add_to_invoice (approval
itself never touches the invoice and the base freight line is never
changed). The line label now carries the §18 frozen context: stop kind
("Pickup"/"Delivery detention"), exception-catalog code, billing span,
and the frozen minimum/cap.

  L1  approve alone creates no invoice and no line.
  L2  action_add_to_invoice appends one line to the booking's draft
      invoice; the base freight line is untouched.
  L3  The label carries kind / exception / span / min / cap; reruns are
      idempotent (no duplicate lines).
"""

from odoo.tests.common import TransactionCase


class TestDetentionInvoiceLabel(TransactionCase):
    """§18.6 detention → invoice line label (booking-module side)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Mid-load test phase: prema.dispatch models were set up while
        # logistics.booking did not exist yet, so their comodels were
        # pinned to _unknown. The final registry.setup_models pass fixes
        # them at boot — repair them here (same hack as the dispatch-side
        # detention tests) so items can freeze their booking.
        for model, field in (
                ("prema.dispatch.detention.item", "booking_id"),
                ("prema.dispatch.job", "logistics_booking_id")):
            f = cls.env[model]._fields.get(field)
            if f and f.comodel_name == "_unknown":
                f.comodel_name = "logistics.booking"
        cls.partner = cls.env["res.partner"].create(
            {"name": "D-B3 Invoice Customer"})
        cls.facility = cls.env["prema.dispatch.location"].create({
            "name": "D-B3 Invoice Facility",
            "address": "126 Test St, Ontario",
            "pin_lat": 43.6,
            "pin_lng": -79.4,
        })

    def _booking_and_item(self, dwell=95):
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 1,
            "weight_lbs": 500,
            "calculated_price": 250.0,
        })
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "logistics_booking_id": booking.id,
        })
        stop = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "stop_type": "dropoff",
            "saved_location_id": self.facility.id,
            "actual_arrival_time": "2026-09-07 09:00:00",
            "actual_departure_time": "2026-09-07 10:%02d:00"
            % (dwell - 60),
        })
        self.env["prema.dispatch.detention.rule"].create({
            "partner_id": self.partner.id,
            "facility_id": self.facility.id,
            "free_minutes": 30, "increment_minutes": 30,
            "rate_per_increment": 25.0,
            "minimum_charge": 60.0, "maximum_charge": 200.0,
        })
        item = self.env["prema.dispatch.detention.item"].sudo()\
            ._suggest_for_stop(stop)
        self.assertTrue(item)
        self.assertEqual(item.booking_id.id, booking.id,
                         "item froze the booking from the job")
        return booking, item

    def _freight_line(self, invoice):
        return invoice.invoice_line_ids.filtered(
            lambda l: "Detention" not in (l.name or ""))

    def _detention_line(self, invoice, item):
        return invoice.invoice_line_ids.filtered(
            lambda l, i=item.id: (l.name or "") and
            ("Detention #%s" % i) in l.name)

    def test_l1_approve_alone_never_invoices(self):
        """§18.6: the approved amount becomes a line ONLY via
        action_add_to_invoice — approval touches no invoice."""
        booking, item = self._booking_and_item()
        item.action_approve()
        self.assertEqual(item.state, "approved")
        self.assertEqual(item.approved_amount, 75.0)
        self.assertFalse(item.invoiced)
        invoices = self.env["account.move"].sudo().search([
            ("logistics_booking_id", "=", booking.id),
            ("move_type", "=", "out_invoice"),
        ])
        self.assertEqual(len(invoices), 0,
                         "no invoice may exist after approval alone")

    def test_l2_l3_line_label_and_idempotency(self):
        """One appended line with the frozen §18 label; base freight
        untouched; reruns never duplicate."""
        booking, item = self._booking_and_item()
        item.write({"exception_type": "customer_delay",
                    "reason_notes": "dock held for paperwork"})
        item.action_approve()

        item.action_add_to_invoice()
        invoice = booking.invoice_id or self.env["account.move"].sudo().search(
            [("logistics_booking_id", "=", booking.id),
             ("move_type", "=", "out_invoice")], limit=1)
        self.assertTrue(invoice, "add-to-invoice opened the booking draft")
        self.assertEqual(invoice.state, "draft")
        det = self._detention_line(invoice, item)
        self.assertEqual(len(det), 1)
        self.assertEqual(det.price_unit, 75.0)

        # §18 label parts: kind + exception code, span, frozen extras.
        label = det.name or ""
        self.assertIn("Detention #%s" % item.id, label)
        self.assertIn("Delivery detention (Customer-Caused Delay)", label)
        self.assertIn("3 × 30 min", label)
        self.assertIn("min 60", label)
        self.assertIn("cap 200", label)
        self.assertIn("dock held for paperwork", label)

        # Base freight untouched: the freight line keeps its price and
        # only the detention line was added.
        freight = self._freight_line(invoice)
        self.assertEqual(len(freight), 1)
        self.assertEqual(freight.price_unit, 250.0)

        # Idempotent rerun: no second line, no second invoice.
        item.action_add_to_invoice()
        self.assertEqual(len(invoice.invoice_line_ids), 2)
        self.assertEqual(len(self._detention_line(invoice, item)), 1)
        self.assertTrue(item.invoiced)
        self.assertEqual(item.invoice_line_id.id, det.id)

    def test_l3_pickup_kind_label(self):
        """A pickup-kind item labels the line 'Pickup detention'."""
        booking, item = self._booking_and_item()
        # Re-freeze the same draft item as pickup-kind via the rule's
        # pickup side (the stop type drives the kind at suggestion).
        stop = item.stop_id
        stop.write({"stop_type": "pickup"})
        self.env["prema.dispatch.detention.rule"].search([
            ("partner_id", "=", self.partner.id)], limit=1).write({
                "pickup_free_minutes": 30,
                "pickup_increment_minutes": 30,
                "pickup_rate_per_increment": 25.0,
            })
        item2 = self.env["prema.dispatch.detention.item"].sudo()\
            ._suggest_for_stop(stop)
        self.assertEqual(item2.stop_kind, "pickup")
        item2.action_approve()
        item2.action_add_to_invoice()
        invoice = booking.invoice_id or self.env["account.move"].sudo().search(
            [("logistics_booking_id", "=", booking.id),
             ("move_type", "=", "out_invoice")], limit=1)
        det = self._detention_line(invoice, item2)
        self.assertEqual(len(det), 1)
        self.assertIn("Pickup detention", det.name)

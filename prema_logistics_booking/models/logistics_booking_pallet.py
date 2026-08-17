"""Canonical physical pallet movement model for milk-run bookings.

One logistics.booking.pallet row = ONE physical pallet/skid unit with
exactly one pickup stop and one-or-more delivery allocations. This is the
persistent source of truth for pallet movement; the legacy
`_pallet_allocs` JSON remains a derived compatibility view.
"""
from odoo import fields, models


class LogisticsBookingPallet(models.Model):
    _name = "logistics.booking.pallet"
    _description = "Booking Physical Pallet Movement"
    _order = "booking_id, sequence, id"

    booking_id = fields.Many2one(
        "logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    label = fields.Char(string="Pallet Label")
    weight_lbs = fields.Float(string="Weight (lbs)", digits=(10, 1))
    commodity = fields.Char()
    temperature_notes = fields.Char(string="Temperature Requirement")
    shared = fields.Boolean(string="Shared Pallet", default=False)
    pickup_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Pickup Stop", required=True,
        ondelete="restrict", index=True)
    reference = fields.Char(string="Customer Reference")
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ("pending_pickup", "Pending Pickup"),
        ("onboard", "Onboard Truck"),
        ("partially_delivered", "Partially Delivered"),
        ("delivered", "Delivered"),
    ], default="pending_pickup", required=True)

    delivery_allocation_ids = fields.One2many(
        "logistics.booking.pallet.stop.allocation", "pallet_id",
        string="Delivery Allocations")

    def sync_custody_from_dispatch_item(self, item):
        """Shared-pallet custody: a shared physical pallet is NEVER marked
        fully delivered at its first allocation — it goes to
        partially_delivered until its FINAL active delivery allocation
        completes.

        The bridge preserves unload sequences 1:1 between the booking
        allocations and the dispatch stop allocations, so delivered
        dispatch allocations map onto booking allocations by
        unload_sequence."""
        self.ensure_one()
        allocations = self.delivery_allocation_ids.filtered("active")
        if item.status == "delivered":
            self.state = "delivered"
            allocations.write({"delivered": True})
            return
        if item.status == "partially_unloaded":
            self.state = "partially_delivered"
            delivered_sequences = set(
                item.stop_allocation_ids.filtered("delivered")
                .mapped("unload_sequence"))
            for alloc in allocations:
                if not alloc.delivered and alloc.unload_sequence in delivered_sequences:
                    alloc.delivered = True
            return
        if item.status in ("loaded", "in_transit", "out_for_delivery"):
            self.state = "onboard"
            return


class LogisticsBookingPalletStopAllocation(models.Model):
    _name = "logistics.booking.pallet.stop.allocation"
    _description = "Booking Pallet Delivery Allocation"
    _order = "pallet_id, unload_sequence, id"

    pallet_id = fields.Many2one(
        "logistics.booking.pallet", required=True, ondelete="cascade",
        index=True)
    delivery_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Delivery Stop", required=True,
        ondelete="restrict", index=True)
    unload_sequence = fields.Integer(default=10)
    delivered = fields.Boolean(default=False)
    active = fields.Boolean(default=True)

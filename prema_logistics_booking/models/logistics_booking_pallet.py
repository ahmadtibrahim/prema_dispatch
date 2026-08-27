"""Canonical physical pallet movement model for milk-run bookings.

One logistics.booking.pallet row = ONE physical pallet/skid unit with
exactly one pickup stop and one-or-more delivery allocations. This is the
persistent source of truth for pallet movement; the legacy
`_pallet_allocs` JSON remains a derived compatibility view.

A SHARED pallet is still ONE physical position on the truck: however many
delivery allocations it has, capacity counts the movement once. Each
allocation carries the PORTION of the pallet's weight delivered at that
stop; the active allocations' weights must sum to the pallet weight.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _

# Tolerance for "allocation weights sum to pallet weight" (rounding).
_WEIGHT_SUM_TOLERANCE_LB = 0.1


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
    pickup_origin_display = fields.Char(
        string="Pickup Origin", compute="_compute_movement_display",
    )
    delivery_destinations_display = fields.Char(
        string="Delivery Destination(s)", compute="_compute_movement_display",
    )
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ("pending_pickup", "Pending Pickup"),
        ("onboard", "Onboard Truck"),
        ("partially_delivered", "Partially Delivered"),
        ("delivered", "Delivered"),
    ], default="pending_pickup", required=True)

    @api.depends(
        "pickup_stop_id.location_name", "pickup_stop_id.company_name",
        "pickup_stop_id.city", "delivery_allocation_ids.active",
        "delivery_allocation_ids.delivery_stop_id.location_name",
        "delivery_allocation_ids.delivery_stop_id.company_name",
        "delivery_allocation_ids.delivery_stop_id.city",
    )
    def _compute_movement_display(self):
        def label(stop):
            if not stop:
                return ""
            name = stop.location_name or stop.company_name or stop.city or "Stop"
            city = (stop.city or "").strip()
            if city and city.lower() not in name.lower():
                name = "%s — %s" % (name, city)
            return name

        for pallet in self:
            pallet.pickup_origin_display = label(pallet.pickup_stop_id)
            destinations = pallet.delivery_allocation_ids.filtered("active").sorted("unload_sequence").mapped("delivery_stop_id")
            pallet.delivery_destinations_display = ", ".join(
                label(stop) for stop in destinations if stop
            ) or "—"

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

    def _auto_split_portions(self):
        """Fill portion weights for pallets that carry none yet:
        single-delivery pallets take the whole weight; shared pallets
        split it evenly across active allocations. Never overwrites
        portions that were already entered."""
        for pallet in self:
            if not pallet.weight_lbs:
                continue
            active = pallet.delivery_allocation_ids.filtered("active")
            if not active or any(a.weight_lbs for a in active):
                continue
            split = round(pallet.weight_lbs / len(active), 1)
            # ONE batched write: the sum-to-pallet constraint must see
            # every portion at once, or the first allocation updated
            # mid-split fails validation.
            active.write({"weight_lbs": split})

    def _validate_portion_sum(self):
        """Pallet invariant: the active allocations' portion weights must
        add up to the pallet weight — once every allocation carries a
        weight (legacy pallets with no portion data stay untouched)."""
        for pallet in self:
            if not pallet.weight_lbs:
                continue
            active = pallet.delivery_allocation_ids.filtered("active")
            if not active or not all(a.weight_lbs for a in active):
                continue  # portions not fully entered yet — nothing to check
            # Round: an even 3-way split of 2000 lb stores as
            # 666.7 → sum 2000.1000000000001; that is 2000.1, not an error.
            total = round(sum(a.weight_lbs or 0.0 for a in active), 1)
            if abs(total - pallet.weight_lbs) > _WEIGHT_SUM_TOLERANCE_LB:
                raise ValidationError(_(
                    "Pallet %s portions do not add up to the pallet "
                    "weight: allocations total %.1f lb, pallet is %.1f lb. "
                    "Re-split the delivery portions."
                ) % (pallet.label or pallet.sequence, total, pallet.weight_lbs))


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
    # Delivery PORTION of a shared pallet: how much of the physical
    # pallet's weight (and how many pieces) come off at THIS stop. The
    # active allocations' weights must sum to the pallet's weight.
    weight_lbs = fields.Float(string="Portion Weight (lbs)",
                              digits=(10, 1))
    piece_count = fields.Integer(string="Pieces")
    delivered = fields.Boolean(default=False)
    active = fields.Boolean(default=True)

    @api.constrains("weight_lbs", "active")
    def _check_portion_weights_sum_to_pallet(self):
        """A shared pallet's delivery portions must add up to the whole
        pallet — the truck delivers exactly what it picked up.

        Skipped while a build is in progress (portion_batch context): the
        orchestration creates allocations one at a time and validates the
        completed pallet explicitly at the end. Deactivating a delivery
        therefore requires re-splitting the portions, which is correct: a
        portion removed from one stop must be re-allocated elsewhere."""
        if self.env.context.get("portion_batch"):
            return  # build in progress — validated at the end of the batch
        for alloc in self:
            alloc.pallet_id._validate_portion_sum()

"""Operational unification bridges for booking -> dispatch execution.

This file deliberately extends existing models instead of introducing a second
booking/dispatch authority.  The commercial Booking remains the shipment
identity; Dispatch Jobs are physical operation tasks; Dispatch Items are the
canonical physical load units shared by every operation that touches them.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError


class PremaDispatchItemBookingBridge(models.Model):
    _inherit = "prema.dispatch.item"

    logistics_booking_id = fields.Many2one(
        "logistics.booking", string="Shipment Booking", index=True,
        ondelete="set null", copy=False,
        help="Canonical commercial shipment that owns this physical load unit.",
    )
    operation_job_ids = fields.Many2many(
        "prema.dispatch.job", "dispatch_item_operation_job_rel",
        "item_id", "job_id", string="Operational Jobs", copy=False,
        help="Every physical dispatch operation that carries or handles this same item.",
    )


class LogisticsBookingOperationalStatus(models.Model):
    _inherit = "logistics.booking"

    operational_status = fields.Selection([
        ("booked", "Booked"),
        ("planned", "Planned"),
        ("dispatched", "Dispatched"),
        ("picked_up", "Picked Up"),
        ("in_transit", "In Transit"),
        ("out_for_delivery", "Out for Delivery"),
        ("partially_delivered", "Partially Delivered"),
        ("delivered", "Delivered"),
        ("delayed", "Delayed / Exception"),
        ("cancelled", "Cancelled"),
    ], compute="_compute_operational_status", string="Operational Status")

    @api.depends(
        "state",
        "dispatch_job_id.stage_id.stage_type",
        "leg_ids.status",
    )
    def _compute_operational_status(self):
        Job = self.env["prema.dispatch.job"]
        for booking in self:
            if booking.state == "cancelled":
                booking.operational_status = "cancelled"
                continue
            jobs = Job.search([
                ("logistics_booking_id", "=", booking.id),
                ("active", "=", True),
            ])
            if not jobs:
                booking.operational_status = "booked" if booking.state == "confirmed" else "planned"
                continue

            customer_dropoffs = jobs.mapped("stop_ids").filtered(
                lambda s: s.stop_type in ("dropoff", "return") and not s.planning_only and s.status != "cancelled"
            )
            completed_dropoffs = customer_dropoffs.filtered(lambda s: s.status == "completed")
            active_stops = jobs.mapped("stop_ids").filtered(lambda s: s.status != "cancelled" and not s.planning_only)

            if jobs.filtered(lambda j: j.stage_id and j.stage_id.stage_type in ("cancelled",)) and not active_stops:
                booking.operational_status = "cancelled"
            elif active_stops.filtered(lambda s: s.status == "issue"):
                booking.operational_status = "delayed"
            elif customer_dropoffs and len(completed_dropoffs) == len(customer_dropoffs):
                booking.operational_status = "delivered"
            elif completed_dropoffs:
                booking.operational_status = "partially_delivered"
            elif active_stops.filtered(lambda s: s.status in ("en_route", "arrived")):
                pending_dropoffs = customer_dropoffs.filtered(lambda s: s.status != "completed")
                booking.operational_status = "out_for_delivery" if pending_dropoffs else "in_transit"
            elif jobs.mapped("item_ids").filtered(lambda i: i.status in ("loaded", "in_transit", "partially_unloaded", "out_for_delivery")):
                booking.operational_status = "in_transit"
            elif jobs.mapped("item_ids").filtered(lambda i: i.status not in ("pending", "cancelled")):
                booking.operational_status = "picked_up"
            elif jobs.filtered(lambda j: j.sent_to_driver_at):
                booking.operational_status = "dispatched"
            else:
                booking.operational_status = "planned"

    def _create_draft_invoice(self):
        """Keep invoice identity aligned with the customer shipment number."""
        invoice = super()._create_draft_invoice()
        for booking in self:
            if invoice and booking.booking_number and invoice.ref != booking.booking_number:
                invoice.sudo().write({"ref": booking.booking_number})
        return invoice

    def _create_dispatch_operation(self, leg, role, operation_date, origin_stop=None,
                                   destination_stop=None, sequence=1):
        """Create one operation and then remove stops that do not belong to that leg.

        The historical implementation copied every customer delivery on every
        leg.  That made feeder and linehaul cards show destinations they never
        physically visit.  This bridge keeps only the leg endpoints and turns
        a transfer-hub endpoint into a real cross-dock operation.
        """
        job = super()._create_dispatch_operation(
            leg, role, operation_date,
            origin_stop=origin_stop, destination_stop=destination_stop,
            sequence=sequence,
        )
        if not leg or not job:
            return job

        hub = leg.transfer_hub_id
        hub_region = hub.canonical_region_id if hub else self.env["logistics.region"]
        origin_is_hub = bool(hub and leg.origin_region_id == hub_region)
        destination_is_hub = bool(hub and leg.destination_region_id == hub_region)

        stops = job.stop_ids.filtered(lambda s: not s.planning_only)
        pickup_stops = stops.filtered(lambda s: s.stop_type in ("pickup", "cross_dock_pickup")).sorted("sequence")
        delivery_stops = stops.filtered(lambda s: s.stop_type in ("dropoff", "cross_dock_drop")).sorted("sequence")

        # Keep a single operational pickup endpoint for this leg.
        if pickup_stops:
            keep_pickup = pickup_stops[:1]
            (pickup_stops - keep_pickup).unlink()
            if origin_is_hub:
                vals = {"stop_type": "cross_dock_pickup", "pod_required": False}
                if hub.saved_location_id:
                    vals.update({
                        "saved_location_id": hub.saved_location_id.id,
                        "address": hub.formatted_address or hub.saved_location_id.address or keep_pickup.address,
                        "latitude": hub.latitude or hub.saved_location_id.pin_lat or keep_pickup.latitude,
                        "longitude": hub.longitude or hub.saved_location_id.pin_lng or keep_pickup.longitude,
                    })
                keep_pickup.write(vals)

        if destination_stop:
            # Find the one dispatch stop corresponding to the leg destination.
            wanted = delivery_stops.filtered(
                lambda s: (
                    destination_stop.saved_location_id and s.saved_location_id == destination_stop.saved_location_id
                ) or (
                    destination_stop.formatted_address and s.address == destination_stop.formatted_address
                )
            )[:1]
            keep_delivery = wanted or delivery_stops[:1]
            if keep_delivery:
                (delivery_stops - keep_delivery).unlink()
                if destination_is_hub:
                    vals = {
                        "stop_type": "cross_dock_drop",
                        "pod_required": False,
                        "pallets_out": self.physical_pallets or self.pallets,
                    }
                    if hub.saved_location_id:
                        vals.update({
                            "saved_location_id": hub.saved_location_id.id,
                            "address": hub.formatted_address or hub.saved_location_id.address or keep_delivery.address,
                            "latitude": hub.latitude or hub.saved_location_id.pin_lat or keep_delivery.latitude,
                            "longitude": hub.longitude or hub.saved_location_id.pin_lng or keep_delivery.longitude,
                        })
                    keep_delivery.write(vals)
            elif destination_is_hub and hub.saved_location_id:
                self.env["prema.dispatch.stop"].create({
                    "job_id": job.id,
                    "stop_type": "cross_dock_drop",
                    "sequence": 20,
                    "saved_location_id": hub.saved_location_id.id,
                    "address": hub.formatted_address or hub.saved_location_id.address or "",
                    "latitude": hub.latitude or hub.saved_location_id.pin_lat,
                    "longitude": hub.longitude or hub.saved_location_id.pin_lng,
                    "pallets_out": self.physical_pallets or self.pallets,
                    "weight_out_lbs": self.weight_lbs,
                    "pod_required": False,
                })
        else:
            # Pickup-only operation must not carry copied customer dropoffs.
            delivery_stops.unlink()

        job.write({
            "planned_route_name": leg.departure_id.corridor_id.name if leg.departure_id else job.planned_route_name,
            "route_definition_mode": "exact_stops",
            "stops_confirmation_state": "confirmed",
        })
        return job

    def _create_dispatch_job(self):
        jobs = super()._create_dispatch_job()
        for booking in self:
            booking_jobs = jobs.filtered(lambda j: j.logistics_booking_id == booking)
            if not booking_jobs:
                continue
            all_items = booking_jobs.mapped("item_ids")
            canonical = all_items.filtered(lambda i: i.status != "cancelled")
            if not canonical:
                canonical = all_items[: booking.physical_pallets or booking.pallets or 1]
            canonical.write({
                "logistics_booking_id": booking.id,
                "operation_job_ids": [(6, 0, booking_jobs.ids)],
            })
            # The old dedup routine cancelled temporary per-leg copies.  Once
            # their allocations have been moved, remove those dead rows so
            # capacity/layout screens never count or display them again.
            dead = all_items.filtered(lambda i: i.status == "cancelled" and i not in canonical)
            if dead:
                dead.unlink()
        return jobs


class PremaDispatchLoadPlanBookingBridge(models.Model):
    _inherit = "prema.dispatch.load.plan"

    def assign_stops_to_pallet(self, item_id, stop_allocations, version=None):
        """Allow one canonical pallet to be allocated across its operation jobs.

        The original rule required stop.job_id == item.job_id, which is
        incompatible with a real multi-leg shipment.  The booking bridge uses
        item.operation_job_ids as the explicit allowed operation set.
        """
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        if not item.exists():
            raise UserError("Item not found.")
        if item.load_plan_id and item.load_plan_id != self:
            raise UserError("Item is currently assigned to another active load plan.")
        if item.pending_future_pickup:
            raise UserError("This pallet belongs to a future pickup and cannot be allocated yet.")

        stop_allocations = stop_allocations or []
        if len(stop_allocations) > 20:
            raise UserError("A pallet can be allocated to at most twenty delivery stops.")

        active_plan_jobs = self.load_plan_job_ids.filtered("active").mapped("job_id")
        allowed_jobs = item.operation_job_ids or item.job_id
        allowed_jobs &= active_plan_jobs or allowed_jobs
        Alloc = self.env["prema.dispatch.pallet.stop.allocation"]
        seen = set()
        active_stop_ids = set()

        for idx, data in enumerate(stop_allocations, start=1):
            stop = self.env["prema.dispatch.stop"].browse(data["stop_id"])
            if not stop.exists() or stop.status == "cancelled" or stop.planning_only:
                raise UserError("Only active operational stops can be allocated.")
            if stop.id in seen:
                raise UserError("The same stop cannot be allocated to one pallet twice.")
            if stop.job_id not in allowed_jobs:
                raise UserError("This stop is not part of an operation carrying this pallet.")
            seen.add(stop.id)
            active_stop_ids.add(stop.id)
            existing = Alloc.search([
                ("dispatch_item_id", "=", item.id),
                ("stop_id", "=", stop.id),
            ], limit=1)
            vals = {
                "dispatch_item_id": item.id,
                "stop_id": stop.id,
                "invoice_id": data.get("invoice_id") or False,
                "unload_sequence": data.get("unload_sequence", idx * 10),
                "notes": data.get("notes") or False,
                "active": True,
            }
            if existing:
                existing.write(vals)
            else:
                Alloc.create(vals)

        stale = item.stop_allocation_ids.filtered(
            lambda allocation: allocation.active and allocation.stop_id.id not in active_stop_ids
        )
        if stale:
            stale.write({"active": False})
        item.write({"shared_skid": len(item.stop_allocation_ids.filtered("active")) > 1})
        self._log_event(
            "stop_allocation_changed", item=item,
            new_value={"stop_allocations": stop_allocations},
        )
        self._bump_version()
        return self.get_load_plan()

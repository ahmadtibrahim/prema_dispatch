"""Connect scheduled LTL bookings to the canonical Dispatch Planner."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class PremaDispatchJob(models.Model):
    _inherit = "prema.dispatch.job"

    logistics_booking_id = fields.Many2one(
        "logistics.booking", string="LTL Booking", ondelete="cascade", index=True, copy=False,
    )
    booking_leg_id = fields.Many2one(
        "logistics.booking.leg", string="Booking Leg", ondelete="cascade", index=True, copy=False,
    )
    corridor_departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Scheduled Departure",
        ondelete="restrict", index=True, copy=False,
    )
    ltl_operation_key = fields.Char(readonly=True, copy=False, index=True)
    operation_date = fields.Date(index=True, copy=False)
    operation_role = fields.Selection([
        ("combined", "Pickup & Delivery"),
        ("pickup", "Pickup"),
        ("delivery", "Delivery"),
        ("feeder", "Feeder"),
        ("linehaul", "Linehaul"),
        ("final_delivery", "Final Delivery"),
        ("custom", "Custom / Expedited"),
    ], default="custom", required=True, copy=False)
    auto_scheduled_ltl = fields.Boolean(default=False, readonly=True, copy=False)

    _sql_constraints = [
        (
            "ltl_operation_key_uniq",
            "unique(ltl_operation_key)",
            "This booking operation is already present in the Dispatch Planner.",
        ),
    ]

    @api.model
    def _operation_date_from_pickup(self, value):
        pickup = fields.Datetime.to_datetime(value)
        if not pickup:
            return False
        return fields.Datetime.context_timestamp(self, pickup).date()

    def _lock_assigned_vehicle_rows(self):
        vehicle_ids = sorted(set(self.filtered("vehicle_id").mapped("vehicle_id").ids))
        if vehicle_ids:
            self.env.cr.execute(
                "SELECT id FROM fleet_vehicle WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(vehicle_ids)],
            )

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            vals = dict(incoming)
            if not vals.get("operation_date") and vals.get("scheduled_pickup"):
                vals["operation_date"] = self._operation_date_from_pickup(
                    vals["scheduled_pickup"]
                )
            normalized.append(vals)
        return super().create(normalized)

    def write(self, vals):
        vals = dict(vals)
        if "scheduled_pickup" in vals and "operation_date" not in vals:
            vals["operation_date"] = self._operation_date_from_pickup(
                vals.get("scheduled_pickup")
            )
        if "vehicle_id" in vals and not self.env.context.get("departure_vehicle_sync"):
            for job in self.filtered("corridor_departure_id"):
                departure_vehicle = job.corridor_departure_id.vehicle_id
                if departure_vehicle and vals.get("vehicle_id") != departure_vehicle.id:
                    raise ValidationError(_(
                        "This LTL job is controlled by departure %(departure)s. "
                        "Reassign the Truck on that departure so every job on it stays synchronized.",
                        departure=job.corridor_departure_id.display_name,
                    ))
        return super().write(vals)

    @api.constrains("vehicle_id", "operation_date", "corridor_departure_id", "stage_id")
    def _check_custom_job_against_departure(self):
        """A dedicated/custom job cannot occupy a truck already reserved by LTL."""
        if self.env.context.get("skip_planner_conflict_check"):
            return
        self._lock_assigned_vehicle_rows()
        for job in self.filtered(
            lambda record: record.vehicle_id
            and record.operation_date
            and not record.corridor_departure_id
            and (not record.stage_id or record.stage_id.stage_type not in ("cancelled", "completed"))
        ):
            departure = self.env["logistics.corridor.departure"].sudo().search([
                ("vehicle_id", "=", job.vehicle_id.id),
                ("departure_date", "=", job.operation_date),
                ("active", "=", True),
                ("status", "not in", ("cancelled", "completed")),
            ], limit=1)
            if departure:
                raise ValidationError(_(
                    "Truck %(truck)s is booked for %(route)s on %(date)s. "
                    "Add this freight to that LTL departure or choose another truck.",
                    truck=job.vehicle_id.display_name,
                    route=departure.corridor_id.display_name,
                    date=job.operation_date,
                ))
            ltl_operation = self.sudo().search([
                ("id", "!=", job.id),
                ("vehicle_id", "=", job.vehicle_id.id),
                ("operation_date", "=", job.operation_date),
                ("auto_scheduled_ltl", "=", True),
                ("stage_id.stage_type", "not in", ("cancelled", "completed")),
            ], limit=1)
            if ltl_operation:
                raise ValidationError(_(
                    "Truck %(truck)s is reserved for LTL operation %(job)s on %(date)s. "
                    "Choose another truck.",
                    truck=job.vehicle_id.display_name,
                    job=ltl_operation.display_name,
                    date=job.operation_date,
                ))

    @api.constrains("vehicle_id", "operation_date", "corridor_departure_id", "stage_id")
    def _check_ltl_operation_day(self):
        """A split next-day LTL card reserves that truck/day as real work."""
        if self.env.context.get("skip_planner_conflict_check"):
            return
        self._lock_assigned_vehicle_rows()
        for job in self.filtered(
            lambda record: record.vehicle_id
            and record.operation_date
            and record.corridor_departure_id
            and (not record.stage_id or record.stage_id.stage_type not in ("cancelled", "completed"))
        ):
            custom_job = self.sudo().search([
                ("id", "!=", job.id),
                ("vehicle_id", "=", job.vehicle_id.id),
                ("operation_date", "=", job.operation_date),
                ("corridor_departure_id", "=", False),
                ("stage_id.stage_type", "not in", ("cancelled", "completed")),
            ], limit=1)
            if custom_job:
                raise ValidationError(_(
                    "Truck %(truck)s already has custom job %(job)s on %(date)s. "
                    "Move that job before confirming this LTL booking.",
                    truck=job.vehicle_id.display_name,
                    job=custom_job.display_name,
                    date=job.operation_date,
                ))

            other_departures = self.env["logistics.corridor.departure"].sudo().search([
                ("id", "!=", job.corridor_departure_id.id),
                ("vehicle_id", "=", job.vehicle_id.id),
                ("departure_date", "=", job.operation_date),
                ("active", "=", True),
                ("status", "not in", ("cancelled", "completed")),
            ])
            if other_departures:
                raise ValidationError(_(
                    "Truck %(truck)s is already booked for %(route)s on %(date)s. "
                    "Choose another departure or truck.",
                    truck=job.vehicle_id.display_name,
                    route=other_departures[0].corridor_id.display_name,
                    date=job.operation_date,
                ))
            other_operations = self.sudo().search([
                ("id", "!=", job.id),
                ("vehicle_id", "=", job.vehicle_id.id),
                ("operation_date", "=", job.operation_date),
                ("auto_scheduled_ltl", "=", True),
                ("stage_id.stage_type", "not in", ("cancelled", "completed")),
            ])
            for operation in other_operations:
                if operation.corridor_departure_id == job.corridor_departure_id:
                    continue
                raise ValidationError(_(
                    "Truck %(truck)s is reserved for LTL operation %(job)s on %(date)s. "
                    "Choose another departure or truck.",
                    truck=job.vehicle_id.display_name,
                    job=operation.display_name,
                    date=job.operation_date,
                ))

    @api.model
    def assign_job_to_truck(self, job_id, truck_id, force=False):
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        if job.corridor_departure_id:
            if job.corridor_departure_id.vehicle_id.id != truck_id:
                return {
                    "success": False,
                    "departure_controlled": True,
                    "error": _(
                        "This LTL load belongs to %(departure)s. Reassign the Truck from Open: Departure.",
                        departure=job.corridor_departure_id.display_name,
                    ),
                }
            return super().assign_job_to_truck(job_id, truck_id, force=force)

        operation_date = job.operation_date or (
            fields.Date.to_date(job.scheduled_pickup) if job.scheduled_pickup else False
        )
        if operation_date:
            conflict = self.env["logistics.corridor.departure"].sudo().search([
                ("vehicle_id", "=", truck_id),
                ("departure_date", "=", operation_date),
                ("active", "=", True),
                ("status", "not in", ("cancelled", "completed")),
            ], limit=1)
            if conflict:
                return {
                    "success": False,
                    "truck_day_blocked": True,
                    "error": _(
                        "This truck is booked for %(route)s on %(date)s. "
                        "Add freight to that LTL departure or choose another truck.",
                        route=conflict.corridor_id.display_name,
                        date=operation_date,
                    ),
                }
            ltl_operation = self.sudo().search([
                ("id", "!=", job.id),
                ("vehicle_id", "=", truck_id),
                ("operation_date", "=", operation_date),
                ("auto_scheduled_ltl", "=", True),
                ("stage_id.stage_type", "not in", ("cancelled", "completed")),
            ], limit=1)
            if ltl_operation:
                return {
                    "success": False,
                    "truck_day_blocked": True,
                    "error": _(
                        "This truck is reserved for LTL operation %(job)s on %(date)s. "
                        "Choose another truck.",
                        job=ltl_operation.display_name,
                        date=operation_date,
                    ),
                }
        return super().assign_job_to_truck(job_id, truck_id, force=force)

    @api.model
    def unassign_truck(self, job_id):
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        if job.corridor_departure_id:
            return {
                "success": False,
                "departure_controlled": True,
                "error": _(
                    "This LTL load is assigned by %(departure)s. Change the Truck in Open: Departure.",
                    departure=job.corridor_departure_id.display_name,
                ),
            }
        return super().unassign_truck(job_id)

    @api.model
    def action_remove_from_booking_board(self, job_id):
        """Booking Board removal — reuses the canonical booking.action_cancel()
        workflow (the proven manual-delete path), then unlinks draft invoice
        and archives the dispatch job to remove it from the active board.

        Returns {success, skipped, error, invoice_deleted, booking_cancelled}.
        """
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}

        # Use sudo for booking access — the Booking Board is internal-staff only.
        # The record rule `rule_logistics_booking_customer_own` restricts bookings
        # to the customer's own company, which blocks dispatchers from cancelling
        # bookings that belong to other companies. sudo() is correct here because
        # this is an admin/dispatcher operation, not a customer self-service action.
        booking = job.sudo().logistics_booking_id
        invoice_deleted = False
        booking_cancelled = False

        try:
            with self.env.cr.savepoint():
                # ── 1. Guard: started jobs cannot be removed ──
                stops = job.stop_ids.filtered(lambda s: s.status != "cancelled")
                started = stops.filtered(
                    lambda s: s.status in ("completed", "arrived", "en_route")
                )
                has_pod = bool(stops.filtered(lambda s: s.pod_attachment_ids))
                if started or has_pod:
                    return {
                        "success": False, "skipped": True,
                        "error": "This shipment has operational activity and cannot be removed.",
                    }

                # ── 2. Guard: posted/paid invoices block removal ──
                invoice = job.invoice_id
                if booking and not invoice:
                    invoice = booking.sudo().invoice_id
                if invoice and invoice.state != "draft":
                    return {
                        "success": False, "skipped": True,
                        "error": "BLOCKED — Accounting document exists (state=%s). Cancel the invoice first." % invoice.state,
                    }

                # ── 3. Use the canonical cancel workflow (same as manual form delete) ──
                if booking and booking.state not in ("cancelled", "completed", "delivered"):
                    booking.sudo().action_cancel(
                        reason="Removed from Booking Board",
                        source="company",
                    )
                    booking_cancelled = True

                # ── 4. Unlink draft invoice (action_cancel only does button_cancel) ──
                invoice = invoice or (booking.sudo().invoice_id if booking else False)
                if invoice and invoice.exists() and invoice.state == "draft":
                    if not invoice.payment_state or invoice.payment_state == "not_paid":
                        invoice.sudo().unlink()
                        invoice_deleted = True

                # ── 5. Archive the dispatch job so it leaves the active board ──
                if job.exists():
                    job.write({"active": False})

        except Exception as exc:
            import traceback as tb
            self.env["prema.dispatch.error.log"].sudo().log_error(
                source="booking_board",
                action="bulk_remove",
                error_message=str(exc),
                severity="error",
                error_type=type(exc).__name__,
                traceback=tb.format_exc(),
                dispatch_job_id=job.id if job.exists() else False,
                booking_id=booking.id if booking else False,
                record_name=job.name if job.exists() else str(job_id),
            )
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "job_name": job.name,
            "booking_number": booking.booking_number if booking else None,
            "invoice_deleted": invoice_deleted,
            "booking_cancelled": booking_cancelled,
        }

    @api.model
    def optimize_truck_day_live(self, truck_id, date_string):
        """Apply one route across every pending job on the truck/day.

        The underlying optimizer preserves completed, arrived, en-route and
        manually locked stops, so a new pickup can be inserted while the
        driver is working without rewriting already-driven history.
        """
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService

        result = DispatchOptimizationService(self.env).apply_consolidated_route(
            truck_id, date_string,
        )
        if result.get("error"):
            return {"success": False, "error": result["error"]}
        return {
            "success": True,
            "stop_count": len(result.get("suggested_order") or []),
            "cross_dock_legs": result.get("cross_dock_legs", 0),
        }


class LogisticsCorridorDeparture(models.Model):
    _inherit = "logistics.corridor.departure"

    def write(self, vals):
        result = super().write(vals)
        if "vehicle_id" in vals:
            for departure in self:
                departure_jobs = self.env["prema.dispatch.job"].sudo().search([
                    ("corridor_departure_id", "=", departure.id),
                    ("auto_scheduled_ltl", "=", True),
                ])
                departure_jobs.with_context(departure_vehicle_sync=True).write({
                    "vehicle_id": departure.vehicle_id.id or False,
                    "assignment_locked": bool(departure.vehicle_id),
                })
        return result

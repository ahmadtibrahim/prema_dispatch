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
    required_temperature_c = fields.Float(
        string="Required Temperature °C", readonly=True, copy=False,
        help="Frozen numeric reefer setpoint from the logistics booking. "
             "Zero is a valid setpoint; this field is empty for dry loads.",
    )
    # ── Canonical requirement snapshot (18-section work order §3) ──────
    # Frozen at booking→job creation so the Driver App, customer tracking
    # and invoice evidence never have to chase the booking. Celsius only;
    # the supplied-flags are the sanctioned existence checks (0°C valid,
    # dry carries nothing).
    minimum_temperature_c = fields.Float(
        string="Minimum (°C)", readonly=True, copy=False)
    maximum_temperature_c = fields.Float(
        string="Maximum (°C)", readonly=True, copy=False)
    temperature_tolerance_c = fields.Float(
        string="Tolerance (°C)", readonly=True, copy=False)
    temperature_supplied = fields.Boolean(
        string="Temperature Set", readonly=True, copy=False)
    minimum_temperature_supplied = fields.Boolean(
        string="Minimum Set", readonly=True, copy=False)
    maximum_temperature_supplied = fields.Boolean(
        string="Maximum Set", readonly=True, copy=False)
    submitted_temperature_unit = fields.Selection(
        [("c", "°C"), ("f", "°F")], string="Submitted In", readonly=True,
        copy=False, default="c")
    temperature_requirement_source = fields.Selection(
        [("customer", "Customer"), ("dispatcher", "Dispatcher"),
         ("system", "System"), ("legacy", "Legacy (pre-canonical)")],
        string="Requirement Source", readonly=True, copy=False,
        default="customer")
    temperature_display_dual = fields.Char(
        string="Temperature", compute="_compute_temperature_display_dual",
        help="Both-units display for dispatcher panels (C-first).")
    temperature_range_display_dual = fields.Char(
        string="Safe Range", compute="_compute_temperature_display_dual")

    @api.depends("temperature_instruction_c", "temperature_supplied",
                 "temperature_range_min_c", "temperature_range_max_c")
    def _compute_temperature_display_dual(self):
        from ..services.temperature_service import format_dual, range_dual
        for job in self:
            job.temperature_display_dual = (
                format_dual(job.temperature_instruction_c)
                if job.temperature_supplied else "")
            if job.temperature_range_min_c is not None \
                    and job.temperature_range_max_c is not None:
                job.temperature_range_display_dual = range_dual(
                    job.temperature_range_min_c, job.temperature_range_max_c)
            else:
                job.temperature_range_display_dual = ""
    # ── Dynamic reefer state (18-section work order §5-§6) ─────────────
    # Written ONLY by TemperatureEngine.recalc() — never edited by hand.
    temperature_state = fields.Selection([
        ("none", "No Reefer Freight"),
        ("precool", "Pre-cool Required"),
        ("on", "Reefer On"),
        ("off", "Reefer Off"),
        ("conflict", "TEMPERATURE CONFLICT"),
    ], string="Reefer State", readonly=True, copy=False, index=True)
    temperature_instruction_c = fields.Float(
        string="Reefer Setpoint (°C)", readonly=True, copy=False,
        help="Canonical Celsius setpoint the driver must set. 0.0 valid.")
    temperature_range_min_c = fields.Float(
        string="Safe Range Min (°C)", readonly=True, copy=False)
    temperature_range_max_c = fields.Float(
        string="Safe Range Max (°C)", readonly=True, copy=False)
    temperature_conflict = fields.Boolean(
        string="Temperature Conflict", readonly=True, copy=False, index=True,
        help="Onboard reefer ranges are incompatible. DISPATCH REVIEW "
             "REQUIRED — automatic route release is blocked until an "
             "authorized override is applied.")
    temperature_message = fields.Char(
        string="Reefer Instruction", readonly=True, copy=False,
        help="Driver-facing instruction text, e.g. 'PRE-COOL REEFER TO "
             "2°C / 35.6°F'.")
    reefer_acknowledged = fields.Boolean(
        string="Setpoint Acknowledged", readonly=True, copy=False)
    reefer_ack_at = fields.Datetime(
        string="Acknowledged At", readonly=True, copy=False)
    reefer_ack_user_id = fields.Many2one(
        "res.users", string="Acknowledged By", readonly=True, copy=False)
    reefer_off_acknowledged = fields.Boolean(
        string="Switch-off Acknowledged", readonly=True, copy=False)
    reefer_off_ack_at = fields.Datetime(
        string="Switch-off Acknowledged At", readonly=True, copy=False)
    reefer_off_ack_user_id = fields.Many2one(
        "res.users", string="Switch-off Acknowledged By", readonly=True,
        copy=False)
    temperature_override_count = fields.Integer(
        string="Temperature Overrides",
        compute="_compute_temperature_override_count")
    # NOTE: the driver's display-unit preference lives on res.users
    # (res_users_temperature_preference). It is read directly in
    # _driver_temperature_payload — a `related="driver_id...."` field here
    # would fail setup because prema.dispatch.job is set up during the
    # prema_dispatch graph, before this module's res.users extension
    # exists.
    has_subcontracted_legs = fields.Boolean(
        related="logistics_booking_id.has_subcontracted_legs", readonly=True,
        string="Subcontracted Execution",
        help="The linked booking executes at least one leg through a "
             "subcontract carrier — PREMAFIRM truck/driver NOT required "
             "for those legs.")

    # ── §10 Progress page: compact physical route for the booking summary.
    # Facility names only — full detail (with evidence isolation) lives in
    # the stop and pallet progress trees.
    physical_route_text = fields.Char(
        string="Physical Route", compute="_compute_physical_route_text")

    def _compute_physical_route_text(self):
        for job in self:
            stops = job.stop_ids.filtered(
                lambda s: not s.planning_only).sorted(
                    key=lambda s: (s.sequence or 0, s.id))
            parts = []
            for s in stops[:4]:
                loc = s.saved_location_id
                label = (loc.business_name or loc.name
                         if loc else (s.address or "").split(",")[0] or "")
                parts.append(label or s.stop_type or f"Stop {s.id}")
            if len(stops) > 4:
                parts.append("…")
            job.physical_route_text = " → ".join(parts)

    _sql_constraints = [
        (
            "ltl_operation_key_uniq",
            "unique(ltl_operation_key)",
            "This booking operation is already present in the Dispatch Planner.",
        ),
    ]

    def _compute_temperature_override_count(self):
        for job in self:
            job.temperature_override_count = self.env[
                "prema.dispatch.temperature.override"].sudo().search_count([
                    ("job_id", "=", job.id)])

    def _recalc_temperature(self, force=False):
        """Engine entry point — safe on every recompute/refresh."""
        if not self:
            return self
        from odoo.addons.prema_logistics_booking.services.temperature_engine import (
            TemperatureEngine)
        for job in self:
            TemperatureEngine(self.env).recalc(job)
        return self

    def action_new_temperature_override(self):
        """Job-form button: open a draft override prefilled with this job
        (dispatcher authorizes from the job form, §5)."""
        self.ensure_one()
        override = self.env["prema.dispatch.temperature.override"].sudo().create({
            "job_id": self.id,
            "state": "draft",
        })
        return {
            "type": "ir.actions.act_window",
            "name": "Authorize Temperature Override",
            "res_model": "prema.dispatch.temperature.override",
            "res_id": override.id,
            "view_mode": "form",
            "target": "new",
        }

    def apply_temperature_override(self, setpoint_c, reason, item_ids=None):
        """Dispatcher-authorized override (Section 5). Guards: only staff
        groups may call; the driver is never asked to decide safety."""
        from odoo.addons.prema_logistics_booking.services.temperature_engine import (
            TemperatureEngine)
        override, state = TemperatureEngine(self.env).apply_override(
            self, setpoint_c, reason, item_ids=item_ids)
        return {
            "success": True,
            "override_id": override.id,
            "state": state["state"],
            "message": state["message"],
        }

    def _driver_temperature_payload(self):
        """Driver-app temperature block (§4-§6). Both units always present;
        the app renders per the driver's display preference. Recompute
        first — refresh is one of the Section 6 triggers (idempotent, no
        timeline noise when nothing changed)."""
        self.ensure_one()
        self._recalc_temperature()
        from odoo.addons.prema_logistics_booking.services.temperature_service import (
            format_dual, range_dual)
        # The display preference lives on res.users (drivers sign in as
        # internal users; the job's driver_id is their res.partner).
        driver_user = self.env["res.users"].search(
            [("partner_id", "=", self.driver_id.id)], limit=1)
        display_unit = driver_user.temperature_display_unit or "c"
        f_first = display_unit == "f"
        state = self.temperature_state or "none"
        if state == "none":
            return {
                "state": state,
                "required": False,
            }
        payload = {
            "state": state,
            "required": True,
            "conflict": bool(self.temperature_conflict),
            "instruction": self.temperature_message or "",
            "setpoint": (
                format_dual(self.temperature_instruction_c, f_first=f_first)
                if self.temperature_instruction_c is not None
                else ""),
            "setpoint_c": self.temperature_instruction_c,
            "range": range_dual(
                self.temperature_range_min_c, self.temperature_range_max_c,
                f_first=f_first),
            "safe_min_c": self.temperature_range_min_c,
            "safe_max_c": self.temperature_range_max_c,
            "setpoint_acknowledged": bool(self.reefer_acknowledged),
            "reefer_off_acknowledged": bool(self.reefer_off_acknowledged),
            "display_unit": display_unit,
        }
        if state == "conflict":
            override = self.env["prema.dispatch.temperature.override"].sudo().search([
                ("job_id", "=", self.id)], order="id desc", limit=1)
            payload["conflict_review"] = (
                "TEMPERATURE CONFLICT — DISPATCH REVIEW REQUIRED. "
                "Contact dispatch — never decide freight safety yourself.")
            payload["last_override"] = (
                f"{format_dual(override.selected_setpoint_c, f_first=f_first)} "
                f"({override.reason}) by {override.override_user_id.name}"
                if override else "")
        return payload

    def _live_map_truck_progress(self, job, stops, vehicle):
        """§8 Live Map truck popup — structured progress panel payload.

        Counts/statuses/timestamps/small metadata ONLY — never binaries
        (evidence stays lazy: counts here, files fetched from the stop
        drill-down on demand). `stops` are the day's serialized stop
        dicts from get_live_map_data (they carry the ETA fields).
        """
        from datetime import datetime, timezone
        from math import asin, cos, radians, sin, sqrt
        from odoo.addons.prema_logistics_booking.services.temperature_service import (
            format_dual, range_dual)

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        def _haversine_km(a, b):
            if not a or not b:
                return None
            dlat = radians(b[0] - a[0])
            dlng = radians(b[1] - a[1])
            h = (sin(dlat / 2) ** 2 + cos(radians(a[0]))
                 * cos(radians(b[0])) * sin(dlng / 2) ** 2)
            return round(6371.0 * 2 * asin(sqrt(h)), 1)

        # ── progress: actions + physical visits ─────────────────────
        done_statuses = ("completed", "skipped")
        done_actions = [s for s in stops if s["status"] in done_statuses]
        open_stops = [s for s in stops
                      if s["status"] not in done_statuses + ("cancelled",)]
        visits = {}
        for s in stops:
            visits.setdefault(s["address"] or s["job_name"], []).append(s)
        visits_total = len(visits)
        visits_done = sum(
            1 for group in visits.values()
            if all(x["status"] in done_statuses for x in group))

        # ── current work + next stop ────────────────────────────────
        current = next((s for s in open_stops if s["status"] in (
            "en_route", "arrived", "issue")), None)
        current_work = None
        if current:
            anchor = current.get("actual_service_start") or current.get(
                "actual_arrival_time") or ""
            elapsed = None
            if anchor:
                try:
                    elapsed = int((now_utc - datetime.fromisoformat(
                        anchor.replace("Z", "+00:00").split("+")[0])).total_seconds() // 60)
                except Exception:
                    elapsed = None
            current_work = {
                "id": current["id"],
                "type": current["type"],
                "status": current["status"],
                "address": current["address"] or "",
                "arrival_at": current.get("actual_arrival_time")
                    or current.get("facility_service_start_at")
                    or current.get("travel_arrival_at") or "",
                "service_elapsed_min": elapsed,
                "pallets": current.get("pallets_in") or current.get(
                    "pallets_out") or 0,
                "issue": current["status"] == "issue",
            }

        next_stop = next((s for s in open_stops if s["status"] in (
            "pending", "en_route")), None)
        next_block = None
        if next_stop:
            prev_coords = None
            for s in stops:
                if s["id"] == next_stop["id"]:
                    break
                if s.get("lat") and s.get("lng"):
                    prev_coords = (s["lat"], s["lng"])
            dist = _haversine_km(
                prev_coords,
                (next_stop.get("lat"), next_stop.get("lng"))
                if next_stop.get("lat") and next_stop.get("lng") else None,
            ) if prev_coords else None
            risk = "none"
            if next_stop.get("appointment_required"):
                risk = "risk" if (next_stop.get("eta_delay_minutes") or 0) > 0 \
                    else "ok"
            next_block = {
                "id": next_stop["id"],
                "type": next_stop["type"],
                "status": next_stop["status"],
                "address": next_stop["address"] or "",
                "eta": next_stop.get("customer_eta_at")
                    or next_stop.get("facility_service_start_at") or "",
                "opening": next_stop.get("facility_hours") or "",
                "distance_km": dist,
                "appointment_risk": risk,
            }

        finish_eta = ""
        if open_stops:
            last_open = open_stops[-1]
            finish_eta = last_open.get("customer_eta_at") or last_open.get(
                "facility_service_start_at") or ""

        # ── pallets + evidence counts (lazy — no binaries) ──────────
        items = job.item_ids
        pallets = {
            "onboard": job.onboard_pallet_count or 0,
            "delivered": len(items.filtered(lambda i: i.status == "delivered")),
            "remaining_pickup": len(
                items.filtered(lambda i: i.status == "pending")),
            "positioned": sum(1 for i in items if i.position_id),
        }
        Evidence = self.env["prema.dispatch.evidence"].sudo()
        evs = Evidence.search([("job_id", "=", job.id)])
        evidence = {"pop": 0, "popp": 0, "pod": 0,
                    "scans_pending": 0, "failed": 0}
        for ev in evs:
            t = ev.evidence_type
            if t in ("pop_general", "scanned_pop"):
                evidence["pop"] += 1
            elif t == "popp":
                evidence["popp"] += 1
            elif t in ("pod_general", "scanned_pod"):
                evidence["pod"] += 1
            elif t == "scan_page" and not ev.merged_into_id:
                evidence["scans_pending"] += 1
            if ev.superseded_by_id:
                evidence["failed"] += 1
        pallets["popp"] = evidence["popp"]

        # ── reefer instruction (dispatcher-facing, C-first) ─────────
        reefer = None
        if job.requires_reefer and hasattr(job, "temperature_state"):
            state = job.temperature_state or "none"
            if state != "none":
                setpoint_c = job.temperature_instruction_c
                reefer = {
                    "state": state,
                    "conflict": bool(job.temperature_conflict),
                    "instruction": job.temperature_message or "",
                    "setpoint": format_dual(setpoint_c)
                        if setpoint_c is not None else "",
                    "range": range_dual(job.temperature_range_min_c,
                                        job.temperature_range_max_c) or "",
                    # On a reefer job every onboard pallet is a refrigerated
                    # pallet; the driver's unit counts are authoritative for
                    # loaded freight on reefer stops.
                    "onboard_reefer_pallets": job.onboard_pallet_count or 0,
                }

        # ── GPS freshness → moving / parked / offline ───────────────
        gps_at = vehicle.x_last_location_at
        gps_age = None
        if gps_at:
            try:
                gps_age = int((now_utc - gps_at.replace(tzinfo=None))
                             .total_seconds() // 60)
            except Exception:
                pass
        moving_state = "offline"
        if gps_age is not None and gps_age <= 15:
            job_state = getattr(job, "vehicle_moving_state", "") or ""
            moving_state = "moving" if job_state == "moving" else (
                "moving" if (current and current["status"] == "en_route")
                else "parked")

        return {
            "completed_actions": len(done_actions),
            "total_actions": len(stops),
            "completed_visits": visits_done,
            "total_visits": visits_total,
            "finish_eta": finish_eta,
            "delay_minutes": next_stop.get("eta_delay_minutes")
                if next_stop else None,
            "moving_state": moving_state,
            "gps_at": self._dt_iso_utc(gps_at),
            "app_last_sync": self._dt_iso_utc(vehicle.x_driver_app_last_sync),
            "current": current_work,
            "next": next_block,
            "pallets": pallets,
            "evidence": evidence,
            "reefer": reefer,
        }

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
        pre = {
            d.id: (d.vehicle_id.id, d.driver_id.id)
            for d in self
        }
        result = super().write(vals)
        if "vehicle_id" in vals or "driver_id" in vals:
            for departure in self:
                old_vehicle_id, _old_driver_id = pre.get(
                    departure.id, (None, None))
                # Phase 3: propagate the new truck/driver to the departure's
                # UNFINISHED work only. Completed and cancelled jobs keep
                # their historical assignment — never rewrite captured
                # history (completed stops, POD/POPP, timestamps).
                unfinished_jobs = self.env["prema.dispatch.job"].sudo().search([
                    ("corridor_departure_id", "=", departure.id),
                    ("stage_id.stage_type", "not in", ("cancelled", "completed")),
                ])
                job_vals = {}
                if "vehicle_id" in vals:
                    job_vals["vehicle_id"] = departure.vehicle_id.id or False
                    job_vals["assignment_locked"] = bool(departure.vehicle_id)
                if "driver_id" in vals:
                    job_vals["driver_id"] = departure.driver_id.id or False
                if job_vals:
                    unfinished_jobs.with_context(
                        departure_vehicle_sync=True).write(job_vals)
                # Phase 3: the departure's load plan rides on the truck —
                # move any unfinished plan for (old truck, date) to the
                # replacement truck. Never touch completed/cancelled plans.
                if "vehicle_id" in vals and departure.vehicle_id.id != old_vehicle_id:
                    plans = self.env["prema.dispatch.load.plan"].sudo().search([
                        ("vehicle_id", "=", old_vehicle_id or 0),
                        ("operating_date", "=", departure.departure_date),
                        ("state", "not in", ("completed", "cancelled")),
                    ])
                    if plans:
                        plan_vals = {
                            "vehicle_id": departure.vehicle_id.id or False,
                        }
                        if "driver_id" in vals:
                            plan_vals["driver_id"] = departure.driver_id.id or False
                        try:
                            plans.write(plan_vals)
                        except ValidationError:
                            raise ValidationError(_(
                                "Truck reassigned, but the load plan for "
                                "%(date)s could not move to %(truck)s: "
                                "another plan already exists for that truck "
                                "and date. Move or complete it first.",
                                date=departure.departure_date,
                                truck=departure.vehicle_id.display_name,
                            ))
        return result

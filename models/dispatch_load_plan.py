import json
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

LOAD_PLAN_STATES = [
    ("draft", "Draft"), ("planning", "Planning"), ("ready", "Ready"),
    ("loading", "Loading"), ("loaded", "Loaded"), ("in_transit", "In Transit"),
    ("completed", "Completed"), ("exception", "Exception"), ("cancelled", "Cancelled"),
]


class PremaDispatchLoadPlan(models.Model):
    _name = "prema.dispatch.load.plan"
    _description = "Load Plan — one physical vehicle-loading execution"
    _order = "operating_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    operating_date = fields.Date(required=True, index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", required=True, index=True)
    driver_id = fields.Many2one("res.partner", index=True)
    origin_stop_id = fields.Many2one("prema.dispatch.stop", help="Set only when this plan was created by a transfer/cross-dock handoff.")
    layout_template_id = fields.Many2one("prema.dispatch.vehicle.layout.template", required=True)
    layout_template_revision = fields.Integer()
    state = fields.Selection(LOAD_PLAN_STATES, default="draft", required=True)
    version = fields.Integer(default=1)
    is_locked = fields.Boolean(default=False)
    locked_at = fields.Datetime()
    locked_by = fields.Many2one("res.users")
    lock_reason = fields.Text()
    is_stale = fields.Boolean(default=False)
    stale_reason = fields.Text()
    stale_since = fields.Datetime()
    stale_triggered_by = fields.Many2one("res.users")
    unverified_layout_acknowledged = fields.Boolean(default=False)
    unverified_layout_acknowledged_by = fields.Many2one("res.users")
    unverified_layout_acknowledged_at = fields.Datetime()
    expected_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    actual_received_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    confirmed_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    assigned_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    loaded_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    onboard_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    vacant_position_count = fields.Integer(compute="_compute_counts", store=True)
    reserved_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    committed_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    available_position_count = fields.Integer(compute="_compute_counts", store=True)
    future_pickup_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    payload_used = fields.Float(compute="_compute_counts", store=True)
    payload_capacity = fields.Float(compute="_compute_counts", store=True)
    utilization_percentage = fields.Float(compute="_compute_counts", store=True)
    final_snapshot_json = fields.Text()
    active = fields.Boolean(default=True)
    load_plan_job_ids = fields.One2many("prema.dispatch.load.plan.job", "load_plan_id")
    pallet_ids = fields.One2many("prema.dispatch.item", "load_plan_id")
    event_ids = fields.One2many("prema.dispatch.load.plan.event", "load_plan_id")
    document_ids = fields.One2many("prema.dispatch.document", "load_plan_id")

    @api.depends("vehicle_id", "operating_date")
    def _compute_name(self):
        for plan in self:
            plan.name = f"LP-{plan.vehicle_id.name or '?'}-{plan.operating_date or ''}"

    @api.depends(
        "load_plan_job_ids.job_id.approximate_skids", "load_plan_job_ids.job_id.actual_received_pallet_count", "load_plan_job_ids.reserved_floor_positions", "load_plan_job_ids.reserve_capacity",
        "pallet_ids", "pallet_ids.consumes_floor_position", "pallet_ids.position_id",
        "pallet_ids.status", "pallet_ids.weight_lbs", "pallet_ids.pending_future_pickup",
        "layout_template_id.max_positions", "vehicle_id.x_max_payload_lbs",
    )
    def _compute_counts(self):
        for plan in self:
            floor_items = plan.pallet_ids.filtered(
                lambda i: i.status != "cancelled" and i.consumes_floor_position and not i.pending_future_pickup
            )
            all_floor_items = plan.pallet_ids.filtered(
                lambda i: i.status != "cancelled" and i.consumes_floor_position
            )
            plan.expected_pallet_count = sum(plan.load_plan_job_ids.mapped("job_id.approximate_skids"))
            plan.actual_received_pallet_count = len(floor_items)
            plan.confirmed_pallet_count = len(floor_items.filtered(lambda i: i.status != "cancelled"))
            plan.reserved_pallet_count = sum(plan.load_plan_job_ids.filtered("active").mapped("reserved_floor_positions"))
            commitment = 0
            for link in plan.load_plan_job_ids.filtered("active"):
                job_items = floor_items.filtered(lambda i, job=link.job_id: i.job_id.id == job.id)
                commitment += max(link.reserved_floor_positions or 0, len(job_items))
            plan.committed_pallet_count = commitment
            plan.assigned_pallet_count = len(floor_items.filtered("position_id"))
            plan.loaded_pallet_count = len(floor_items.filtered(lambda i: i.status in ("loaded", "in_transit", "partially_unloaded", "delivered")))
            plan.onboard_pallet_count = len(floor_items.filtered(lambda i: i.status in ("loaded", "in_transit", "partially_unloaded")))
            plan.future_pickup_pallet_count = len(all_floor_items.filtered("pending_future_pickup"))
            max_pos = plan.layout_template_id.max_positions
            plan.vacant_position_count = max(0, max_pos - plan.assigned_pallet_count)
            plan.available_position_count = max(0, max_pos - plan.committed_pallet_count)
            plan.payload_used = sum(plan.pallet_ids.filtered(lambda i: i.status != "cancelled").mapped("weight_lbs"))
            plan.payload_capacity = plan.vehicle_id.x_max_payload_lbs or 0.0
            plan.utilization_percentage = (plan.assigned_pallet_count / max_pos * 100.0) if max_pos else 0.0

    @api.constrains("vehicle_id", "operating_date", "origin_stop_id", "active")
    def _check_unique_plan(self):
        for plan in self:
            if not plan.active:
                continue
            domain = [
                ("id", "!=", plan.id), ("active", "=", True),
                ("vehicle_id", "=", plan.vehicle_id.id),
                ("operating_date", "=", plan.operating_date),
                ("origin_stop_id", "=", plan.origin_stop_id.id if plan.origin_stop_id else False),
            ]
            if self.search_count(domain):
                raise ValidationError("A Load Plan already exists for this vehicle/date/origin combination.")

    def create(self, vals_list):
        records = super().create(vals_list)
        for plan in records:
            plan._log_event("created")
        return records

    # ── Internal helpers ─────────────────────────────────────────────

    def _log_event(self, event_type, item=None, from_position=None, to_position=None,
                    from_plan=None, to_plan=None, reason=None, old_value=None, new_value=None, snapshot=None):
        self.ensure_one()
        self.env["prema.dispatch.load.plan.event"].create({
            "load_plan_id": self.id,
            "event_type": event_type,
            "item_id": item.id if item else False,
            "from_position_id": from_position.id if from_position else False,
            "to_position_id": to_position.id if to_position else False,
            "from_load_plan_id": from_plan.id if from_plan else False,
            "to_load_plan_id": to_plan.id if to_plan else False,
            "reason": reason,
            "old_value_json": json.dumps(old_value) if old_value is not None else False,
            "new_value_json": json.dumps(new_value) if new_value is not None else False,
            "snapshot_json": json.dumps(snapshot) if snapshot is not None else False,
        })

    def _check_version(self, version):
        self.ensure_one()
        if version is not None and int(version) != self.version:
            raise UserError(
                f"This load plan was updated by someone else (now at version {self.version}). "
                "Refresh before saving your changes."
            )

    def _bump_version(self):
        self.write({"version": self.version + 1})

    def _mark_stale(self, reason):
        self.ensure_one()
        if self.is_locked:
            return
        self.write({
            "is_stale": True, "stale_reason": reason,
            "stale_since": fields.Datetime.now(), "stale_triggered_by": self.env.user.id,
        })
        self._log_event("marked_stale", reason=reason)

    def _check_dispatch_staff_or_raise(self, require_manager=False):
        from odoo.addons.prema_dispatch.services.dispatch_auth import is_dispatch_staff
        user = self.env.user
        if require_manager:
            if not (user.has_group("prema_dispatch.group_dispatch_manager") or user.has_group("base.group_system")):
                raise AccessError("Only a Dispatch Manager can perform this action.")
        elif not is_dispatch_staff(self.env):
            raise AccessError("Not authorized.")

    def _check_access(self, require_not_locked=False):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_load_plan_access
        for plan in self:
            check_load_plan_access(self.env, plan, require_not_locked=require_not_locked)

    def _vehicle_layout_capacity(self, layout_type=None):
        self.ensure_one()
        return self.vehicle_id.get_layout_capacity(layout_type or self.layout_template_id.layout_type)

    def _layout_is_vehicle_verified(self):
        self.ensure_one()
        vehicle = self.vehicle_id
        if not vehicle or not vehicle.layout_configuration_verified:
            return False
        return self.layout_template_id.max_positions == self._vehicle_layout_capacity()

    # ── Read ─────────────────────────────────────────────────────────

    @api.model
    def get_layout_templates(self, vehicle_id=None):
        domain = [("active", "=", True)]
        if vehicle_id:
            domain = ["|", ("applicable_vehicle_ids", "in", [vehicle_id]), ("applicable_vehicle_ids", "=", False)] + domain
        templates = self.env["prema.dispatch.vehicle.layout.template"].search(domain)
        return [{
            "id": t.id, "name": t.name, "layout_type": t.layout_type, "max_positions": t.max_positions,
            "is_verified": t.is_verified, "revision": t.revision,
            "positions": [{
                "id": p.id, "position_code": p.position_code, "display_name": p.display_name or p.position_code,
                "side": p.side, "sequence": p.sequence, "x": p.x_coordinate, "y": p.y_coordinate,
                "width": p.display_width, "height": p.display_height, "orientation": p.orientation,
                "max_weight_lbs": p.max_weight_lbs, "four_way_required": p.four_way_required,
                "blocked": p.blocked, "blocked_reason": p.blocked_reason,
            } for p in t.position_ids.filtered("active")],
        } for t in templates]

    def get_load_plan(self):
        """Single batched read — header, jobs, pallets/positions, warnings.
        Never one RPC per position."""
        self.ensure_one()
        self._check_access()
        tpl = self.layout_template_id
        positions_by_code = {p.position_code: p for p in tpl.position_ids.filtered("active")}
        items = self.pallet_ids.filtered(lambda i: i.status != "cancelled" and not i.pending_future_pickup)
        items_by_position = {i.position_id.position_code: i for i in items if i.position_id}
        future_items = self.pallet_ids.filtered(
            lambda i: i.status != "cancelled" and i.pending_future_pickup)

        # Position-level reservations for future pickups (pending
        # reserve_position operations) — the planning commitment that lets a
        # confirmed-but-not-yet-picked-up pallet occupy a slot on the diagram.
        def _loc_label(stop):
            if not stop:
                return ""
            return (stop.saved_location_id.name
                    or (stop.saved_location_id.business_name if stop.saved_location_id else False)
                    or stop.job_id.partner_id.name
                    or stop.address
                    or f"Stop {stop.sequence}")

        def _reservation_payload(op):
            job = op.related_pickup_stop_id.job_id if op.related_pickup_stop_id else False
            job_future = future_items.filtered(lambda i, j=job: (i.job_id.id == j.id) if j else False)
            delivery = False
            if job_future:
                delivery = job_future[0].delivery_stop_id
            elif job:
                delivery = job.stop_ids.filtered(
                    lambda s: s.stop_type == "dropoff" and not s.planning_only)[:1]
            return {
                "operation_id": op.id,
                "job_id": job.id if job else False,
                "job_name": job.name if job else "Future pickup",
                "item_ids": job_future.ids,
                "item_names": job_future.mapped("name"),
                "pickup_label": _loc_label(op.related_pickup_stop_id),
                "delivery_label": _loc_label(delivery),
            }

        reservations = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True),
        ])
        reservation_by_code = {
            r.to_position_id.position_code: _reservation_payload(r) for r in reservations
        }
        reserved_code_by_job = {
            r.related_pickup_stop_id.job_id.id: r.to_position_id.position_code
            for r in reservations if r.related_pickup_stop_id
        }

        # Delivery destinations travel WITH each item (delivery_choices),
        # not only in the plan-level available_stops groups keyed by the
        # load_plan_job link. A job whose pallets reached the plan without
        # a job-link row (or a plan without the job) used to render Step 2
        # with zero choices — "No delivery stop assigned by Dispatch" —
        # even though item.delivery_stop_id was set (the Step-2 defect).
        choices_by_job = {
            job.id: [{
                "stop_id": stop.id,
                "sequence": stop.sequence,
                "customer": stop.saved_location_id.business_name or stop.address,
                "status": stop.status,
                "city": stop.saved_location_id.city or "",
                "state": stop.saved_location_id.province_code or "",
            } for stop in job.stop_ids.filtered(
                lambda s: s.stop_type == "dropoff" and not s.planning_only
                and s.status != "cancelled").sorted("sequence")]
            for job in self.load_plan_job_ids.filtered("active").mapped("job_id")
        }

        def _choices_for(item):
            """Item-level destination choices — the plan group when the
            job is on the plan, else the item's OWN job's dropoff stops
            (the authoritative fallback that never leaves a pallet with a
            delivery stop but no renderable choice)."""
            if item.job_id and item.job_id.id in choices_by_job:
                return choices_by_job[item.job_id.id]
            if not item.job_id:
                return []
            return [{
                "stop_id": stop.id,
                "sequence": stop.sequence,
                "customer": stop.saved_location_id.business_name or stop.address,
                "status": stop.status,
                "city": stop.saved_location_id.city or "",
                "state": stop.saved_location_id.province_code or "",
            } for stop in item.job_id.stop_ids.filtered(
                lambda s: s.stop_type == "dropoff" and not s.planning_only
                and s.status != "cancelled").sorted("sequence")]

        def item_payload(item):
            allocs = self.env["prema.dispatch.pallet.stop.allocation"].search([
                ("dispatch_item_id", "=", item.id), ("active", "=", True),
            ])
            popp = [{
                "id": a.id, "name": a.name,
                "url": f"/web/content/{a.id}",
            } for a in item.popp_attachment_ids]
            return {
                "id": item.id, "name": item.name, "load_unit_type": item.load_unit_type,
                "job_id": item.job_id.id,
                "job_name": item.job_id.name,
                "shared_skid": item.shared_skid, "status": item.status,
                "weight_lbs": item.weight_lbs, "position_id": item.position_id.id,
                "position_code": item.position_id.position_code if item.position_id else False,
                "exception_state": item.exception_state,
                "popp_photos": popp,
                "popp_count": len(popp),
                "popp_complete": bool(popp),
                "delivery_stop_id": item.delivery_stop_id.id if item.delivery_stop_id else False,
                "delivery_stop_name": _loc_label(item.delivery_stop_id),
                "delivery_choices": _choices_for(item),
                "stops": [{
                    "stop_id": a.stop_id.id, "sequence": a.stop_id.sequence,
                    "customer": a.stop_id.job_id.partner_id.name,
                    "invoice_id": a.invoice_id.id, "delivered": a.delivered,
                } for a in allocs],
            }

        return {
            "id": self.id, "name": self.name, "version": self.version, "state": self.state,
            "vehicle": {"id": self.vehicle_id.id, "name": self.vehicle_id.name},
            "driver": {"id": self.driver_id.id, "name": self.driver_id.name} if self.driver_id else False,
            "operating_date": self.operating_date.isoformat() if self.operating_date else False,
            "layout_template": {"id": tpl.id, "name": tpl.name, "layout_type": tpl.layout_type,
                                 "revision": tpl.revision, "is_verified": tpl.is_verified,
                                 "vehicle_verified": self._layout_is_vehicle_verified(),
                                 "capacity": self._vehicle_layout_capacity(tpl.layout_type)},
            "is_locked": self.is_locked, "lock_reason": self.lock_reason,
            "is_stale": self.is_stale, "stale_reason": self.stale_reason,
            "unverified_layout_acknowledged": self.unverified_layout_acknowledged,
            "unverified_layout_acknowledged_by": (
                self.unverified_layout_acknowledged_by.name
                if self.unverified_layout_acknowledged_by else False),
            "unverified_layout_acknowledged_at": (
                self.unverified_layout_acknowledged_at.isoformat()
                if self.unverified_layout_acknowledged_at else False),
            "counts": {
                "expected": self.expected_pallet_count, "actual_received": self.actual_received_pallet_count,
                "confirmed": self.confirmed_pallet_count,
                "assigned": self.assigned_pallet_count, "loaded": self.loaded_pallet_count,
                "onboard": self.onboard_pallet_count,
                "reserved": self.reserved_pallet_count, "committed": self.committed_pallet_count,
                "vacant": self.vacant_position_count, "available": self.available_position_count, "max_positions": tpl.max_positions,
                "future_pickup": self.future_pickup_pallet_count,
                "utilization_percentage": round(self.utilization_percentage, 1),
            },
            "payload": {"used": self.payload_used, "capacity": self.payload_capacity},
            "jobs": [{
                "load_plan_job_id": j.id, "job_id": j.job_id.id, "job_name": j.job_id.name,
                "customer": j.job_id.partner_id.name, "state": j.state,
                "pickup_step_state": j.job_id._pickup_completion_step_state(),
            } for j in self.load_plan_job_ids.filtered("active")],
            "positions": [{
                "id": p.id, "position_code": code, "display_name": p.display_name or code, "side": p.side,
                "sequence": p.sequence, "x": p.x_coordinate, "y": p.y_coordinate,
                "width": p.display_width, "height": p.display_height, "blocked": p.blocked,
                "item": item_payload(items_by_position[code]) if code in items_by_position else False,
                "reservation": reservation_by_code.get(code, False),
            } for code, p in positions_by_code.items()],
            "unassigned_items": [item_payload(i) for i in items if not i.position_id and i.consumes_floor_position],
            "non_floor_items": [item_payload(i) for i in items if not i.consumes_floor_position],
            "future_pickup_items": [{
                **item_payload(i),
                "reserved_position_code": reserved_code_by_job.get(i.job_id.id, False),
                "pickup_label": _loc_label(i.pickup_stop_id or i.available_after_stop_id),
                "delivery_label": _loc_label(i.delivery_stop_id),
            } for i in future_items],
            "available_stops": [{
                "job_id": job.id,
                "job_name": job.name,
                "stops": [{
                    "stop_id": stop.id,
                    "sequence": stop.sequence,
                    "customer": stop.saved_location_id.business_name or stop.address,
                    "status": stop.status,
                    "city": stop.saved_location_id.city or "",
                    "state": stop.saved_location_id.province_code or "",
                } for stop in job.stop_ids.filtered(lambda stop: stop.stop_type == "dropoff" and not stop.planning_only and stop.status != "cancelled").sorted("sequence")],
            } for job in self.load_plan_job_ids.filtered("active").mapped("job_id")],
            "warnings": self.validate_load_plan()["warnings"],
        }

    def get_load_plan_for_warehouse(self):
        """Same data as get_load_plan(), with customer names, invoice
        references, and per-job customer info stripped — warehouse staff
        may see truck/position/pallet/stop-number/exception state, never
        rates, revenue, or customer/invoice detail (see Decision 3/Phase 5)."""
        self.ensure_one()
        payload = self.get_load_plan()

        def strip_item(item):
            if not item:
                return item
            item["stops"] = [{"stop_id": s["stop_id"], "sequence": s["sequence"]} for s in item.get("stops", [])]
            item["delivery_choices"] = [
                {"stop_id": c["stop_id"], "sequence": c["sequence"]}
                for c in item.get("delivery_choices", [])
            ]
            return item

        for pos in payload["positions"]:
            pos["item"] = strip_item(pos["item"])
        payload["unassigned_items"] = [strip_item(i) for i in payload["unassigned_items"]]
        payload["non_floor_items"] = [strip_item(i) for i in payload["non_floor_items"]]
        payload["jobs"] = [{"job_id": j["job_id"], "state": j["state"]} for j in payload["jobs"]]
        return payload

    # ── Create / job membership ──────────────────────────────────────

    def _find_or_create_plan_record(self, vehicle_id, operating_date, driver_id=None):
        existing = self.search([
            ("vehicle_id", "=", vehicle_id), ("operating_date", "=", operating_date),
            ("origin_stop_id", "=", False), ("active", "=", True),
        ], limit=1)
        if existing:
            existing._check_access()
            return existing
        if not self.get_layout_templates(vehicle_id):
            raise UserError("No layout template available for this vehicle. Create one first.")
        # Authorize auto-creation for non-staff users.
        # Dispatch staff always pass; drivers may only auto-create their
        # own plan (must be the vehicle's assigned driver); warehouse users
        # may auto-create for any truck (scoped by operational state).
        from odoo.addons.prema_dispatch.services.dispatch_auth import is_dispatch_staff, get_driver_partner
        skip_check = False
        resolved_driver_id = driver_id
        if is_dispatch_staff(self.env):
            skip_check = True
        elif self.env.user.has_group("prema_dispatch.group_dispatch_warehouse"):
            skip_check = True
        else:
            driver_partner = get_driver_partner(self.env)
            if driver_partner:
                # The driver app sends driver_id=null — resolve from the vehicle instead.
                vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
                vehicle_driver = vehicle.driver_id or vehicle.x_current_driver_contact_id
                if driver_id and driver_partner.id == driver_id:
                    skip_check = True
                elif vehicle_driver and driver_partner.id == vehicle_driver.id:
                    skip_check = True
                    resolved_driver_id = vehicle_driver.id
        if not skip_check:
            raise AccessError("Not authorized to create a load plan for this vehicle.")
        self.create_load_plan(vehicle_id, operating_date, driver_id=resolved_driver_id, _skip_staff_check=True)
        return self.search([
            ("vehicle_id", "=", vehicle_id), ("operating_date", "=", operating_date),
            ("origin_stop_id", "=", False), ("active", "=", True),
        ], limit=1)

    @api.model
    def get_or_create_for_vehicle_date(self, vehicle_id, operating_date, driver_id=None):
        """Idempotent lookup used by the UI (Planner panel / Driver App) —
        opening the same truck/date repeatedly must not hit the
        one-plan-per-vehicle-per-date uniqueness constraint."""
        return self._find_or_create_plan_record(vehicle_id, operating_date, driver_id).get_load_plan()

    @api.model
    def get_or_create_for_vehicle_date_warehouse(self, vehicle_id, operating_date):
        """Same as get_or_create_for_vehicle_date, but returns the
        customer/invoice-stripped warehouse payload (see
        get_load_plan_for_warehouse)."""
        return self._find_or_create_plan_record(vehicle_id, operating_date).get_load_plan_for_warehouse()

    @api.model
    def create_load_plan(self, vehicle_id, operating_date, layout_template_id=None, driver_id=None, origin_stop_id=None, _skip_staff_check=False):
        if not _skip_staff_check:
            self._check_dispatch_staff_or_raise()
        if not layout_template_id:
            templates = self.get_layout_templates(vehicle_id)
            if not templates:
                raise UserError("No layout template available for this vehicle. Create one first.")
            vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
            default_layout = vehicle.default_pallet_layout or "straight"
            preferred = [t for t in templates if t["layout_type"] == default_layout]
            straight = [t for t in templates if t["layout_type"] == "straight"]
            layout_template_id = (preferred or straight or templates)[0]["id"]
        tpl = self.env["prema.dispatch.vehicle.layout.template"].browse(layout_template_id)
        plan = self.create({
            "vehicle_id": vehicle_id, "operating_date": operating_date,
            "driver_id": driver_id, "origin_stop_id": origin_stop_id,
            "layout_template_id": layout_template_id, "layout_template_revision": tpl.revision,
        })
        return plan.get_load_plan()

    def add_job(self, job_id):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            raise UserError("Job not found.")
        # Idempotent: don't create a duplicate link if this job is already
        # on this plan (unique constraint prema_dispatch_load_plan_job_job_unique_per_plan).
        existing = self.env["prema.dispatch.load.plan.job"].search([
            ("load_plan_id", "=", self.id), ("job_id", "=", job.id), ("active", "=", True),
        ], limit=1)
        if existing:
            return self.get_load_plan()
        self.env["prema.dispatch.load.plan.job"].create({"load_plan_id": self.id, "job_id": job.id})
        # LOAD-PLAN POPULATION: adding the job attaches its floor items
        # (one item per physical pallet) to this plan — a plan with a job
        # link but zero pallets is the zero-item bug. Shared skids count
        # one item; future-pickup pallets (second pickup, milk-run) are
        # planned on the truck that will carry them.
        floor_items = job.item_ids.filtered(
            lambda i: i.status != "cancelled"
            and i.load_unit_type in ("pallet", "shared_pallet", "container"))
        missing = floor_items.filtered(lambda i: not i.load_plan_id)
        if missing:
            missing.write({"load_plan_id": self.id})
        self._mark_stale("Job added to load plan")
        self._log_event("job_added", new_value={
            "job_id": job.id, "items_attached": len(missing)})
        self._bump_version()
        return self.get_load_plan()

    def remove_job(self, job_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        link = self.load_plan_job_ids.filtered(lambda j: j.job_id.id == job_id)
        link.write({"active": False})
        self._mark_stale("Job removed from load plan")
        self._log_event("job_removed", old_value={"job_id": job_id})
        self._bump_version()
        return self.get_load_plan()

    # ── Pallet position assignment ───────────────────────────────────

    def _get_position(self, position_id):
        pos = self.env["prema.dispatch.vehicle.layout.position"].browse(position_id)
        if not pos.exists() or pos.layout_template_id.id != self.layout_template_id.id:
            raise UserError("Invalid position for this load plan's layout.")
        if pos.blocked:
            raise UserError(f"Position {pos.position_code} is blocked: {pos.blocked_reason or 'not available'}.")
        return pos

    def assign_pallet_to_position(self, item_id, position_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        if not item.exists() or item.load_plan_id.id != self.id:
            raise UserError("Item not found on this load plan.")
        if item.pending_future_pickup:
            raise UserError("This pallet belongs to a future pickup and cannot be positioned yet.")
        pos = self._get_position(position_id)
        occupant = self.pallet_ids.filtered(lambda i: i.position_id.id == pos.id and i.id != item.id and i.status != "cancelled")
        if occupant:
            raise UserError(f"Position {pos.position_code} is already occupied by {occupant[0].name}.")
        reserved = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True), ("to_position_id", "=", pos.id),
        ], limit=1)
        if reserved:
            raise UserError(f"Position {pos.position_code} is reserved for a future pickup — un-reserve it before assigning freight there.")
        item.write({"position_id": pos.id})
        self._log_event("pallet_assigned", item=item, to_position=pos)
        self._bump_version()
        return self.get_load_plan()

    def unassign_pallet(self, item_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        old_pos = item.position_id
        item.write({"position_id": False})
        self._log_event("pallet_unassigned", item=item, from_position=old_pos)
        self._bump_version()
        return self.get_load_plan()

    def move_pallet(self, item_id, to_position_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        old_pos = item.position_id
        pos = self._get_position(to_position_id)
        occupant = self.pallet_ids.filtered(lambda i: i.position_id.id == pos.id and i.id != item.id and i.status != "cancelled")
        if occupant:
            raise UserError(f"Position {pos.position_code} is already occupied by {occupant[0].name}.")
        reserved = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True), ("to_position_id", "=", pos.id),
        ], limit=1)
        if reserved:
            raise UserError(f"Position {pos.position_code} is reserved for a future pickup — un-reserve it before moving freight there.")
        item.write({"position_id": pos.id})
        self._log_event("pallet_moved", item=item, from_position=old_pos, to_position=pos)
        self._bump_version()
        return self.get_load_plan()

    def swap_pallets(self, item_id_a, item_id_b, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item_a = self.env["prema.dispatch.item"].browse(item_id_a)
        item_b = self.env["prema.dispatch.item"].browse(item_id_b)
        pos_a, pos_b = item_a.position_id, item_b.position_id
        item_a.write({"position_id": pos_b.id if pos_b else False})
        item_b.write({"position_id": pos_a.id if pos_a else False})
        self._log_event("pallets_swapped", item=item_a, from_position=pos_a, to_position=pos_b)
        self._bump_version()
        return self.get_load_plan()

    def assign_stops_to_pallet(self, item_id, stop_allocations, version=None):
        """stop_allocations: list of {stop_id, invoice_id, unload_sequence, notes}"""
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        if not item.exists() or item.load_plan_id.id != self.id:
            raise UserError("Item not found on this load plan.")
        if item.pending_future_pickup:
            raise UserError("This pallet belongs to a future pickup and cannot be allocated yet.")
        stop_allocations = stop_allocations or []
        if not stop_allocations:
            # A physical pallet always needs at least one delivery destination.
            # UAT 2026-08-25: the Driver App Step 2 toggle could send [] and
            # silently unassign the pallet, stranding the driver on a step
            # with nothing selected. The UI no longer offers that click, and
            # the endpoint refuses the empty payload outright.
            raise UserError("Select a delivery destination for this pallet.")
        if len(stop_allocations) > 5:
            raise UserError("A pallet can be allocated to at most five stops.")
        Alloc = self.env["prema.dispatch.pallet.stop.allocation"]
        old_allocs = [{
            "stop_id": a.stop_id.id,
            "unload_sequence": a.unload_sequence,
        } for a in item.stop_allocation_ids.filtered("active")]
        job_ids = set(self.load_plan_job_ids.filtered("active").mapped("job_id.id"))
        seen_stop_ids = set()
        active_stop_ids = set()
        for idx, a in enumerate(stop_allocations, start=1):
            stop = self.env["prema.dispatch.stop"].browse(a["stop_id"])
            if not stop.exists() or stop.status == "cancelled" or stop.planning_only:
                raise UserError("Only active operational stops can be allocated.")
            if stop.id in seen_stop_ids:
                raise UserError("The same stop cannot be allocated to one pallet twice.")
            if stop.job_id.id != item.job_id.id or stop.job_id.id not in job_ids:
                raise UserError("Pallet allocations must stay within the same physical run and job.")
            seen_stop_ids.add(stop.id)
            active_stop_ids.add(stop.id)
            # active_test=False: re-adding a stop that was previously
            # deactivated (remove_stop_from_pallet / stale cleanup) must
            # REACTIVATE that row — the plain search skips inactive records
            # and would crash on the item_stop_unique constraint instead.
            existing = Alloc.with_context(active_test=False).search(
                [("dispatch_item_id", "=", item.id), ("stop_id", "=", stop.id)])
            vals = {
                "dispatch_item_id": item.id, "stop_id": stop.id,
                "invoice_id": a.get("invoice_id"), "unload_sequence": a.get("unload_sequence", idx * 10),
                "notes": a.get("notes"),
                "active": True,
            }
            if existing:
                existing.write(vals)
            else:
                Alloc.create(vals)
        stale_allocs = item.stop_allocation_ids.filtered(lambda alloc: alloc.active and alloc.stop_id.id not in active_stop_ids)
        if stale_allocs:
            stale_allocs.write({"active": False})
        item.write({"shared_skid": len(item.stop_allocation_ids.filtered("active")) > 1})
        self._log_event("stop_allocation_changed", item=item,
                        old_value={"stop_allocations": old_allocs},
                        new_value={"stop_allocations": stop_allocations})
        self._bump_version()
        return self.get_load_plan()

    def remove_stop_from_pallet(self, item_id, stop_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        alloc = self.env["prema.dispatch.pallet.stop.allocation"].search([
            ("dispatch_item_id", "=", item_id), ("stop_id", "=", stop_id),
        ])
        alloc.write({"active": False})
        item.write({"shared_skid": len(item.stop_allocation_ids.filtered("active")) > 1})
        self._log_event("stop_allocation_changed", item=item, old_value={"removed_stop_id": stop_id})
        self._bump_version()
        return self.get_load_plan()

    # ── Layout ────────────────────────────────────────────────────────

    def change_layout(self, layout_template_id, version=None, confirm_remap=False):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        new_tpl = self.env["prema.dispatch.vehicle.layout.template"].browse(layout_template_id)
        if not new_tpl.exists():
            raise UserError("Layout template not found.")
        assigned = self.pallet_ids.filtered(lambda i: i.status != "cancelled" and i.position_id)
        if assigned and not confirm_remap:
            return {
                "requires_confirmation": True,
                "message": f"{len(assigned)} pallet(s) are currently positioned. Changing layout will unassign any that don't fit the new template. Confirm to proceed.",
            }
        new_codes = set(new_tpl.position_ids.filtered("active").mapped("position_code"))
        preserved, unmatched = 0, 0
        for item in assigned:
            if item.position_id.position_code in new_codes:
                new_pos = new_tpl.position_ids.filtered(lambda p: p.position_code == item.position_id.position_code)
                item.write({"position_id": new_pos.id})
                preserved += 1
            else:
                item.write({"position_id": False})
                unmatched += 1
        old_tpl = self.layout_template_id
        # A different template — verified or not — needs its own fresh
        # acknowledgement; carrying one template's ack over to another
        # would defeat the point of the check.
        self.write({
            "layout_template_id": new_tpl.id, "layout_template_revision": new_tpl.revision,
            "unverified_layout_acknowledged": False, "unverified_layout_acknowledged_by": False,
            "unverified_layout_acknowledged_at": False,
        })
        self._log_event("layout_changed", old_value={"template_id": old_tpl.id}, new_value={
            "template_id": new_tpl.id, "preserved": preserved, "unmatched": unmatched,
        })
        self._bump_version()
        result = self.get_load_plan()
        result["layout_change_summary"] = f"Preserved {preserved} assignment(s), {unmatched} now unassigned."
        return result

    def evaluate_layout_for_capacity(self):
        """Called when the confirmed pallet count changes (see
        dispatch_item.py's create() override). Never applies a layout
        change silently — reuses change_layout()'s existing
        confirm-required flow so assignments are only remapped after an
        explicit confirmation, exactly as for a manual layout change."""
        self.ensure_one()
        tpl = self.layout_template_id
        if self.is_locked:
            return None
        target_layout = self.vehicle_id.get_recommended_pallet_layout(self.confirmed_pallet_count)
        if not target_layout:
            self._mark_stale(f"Confirmed pallet count ({self.confirmed_pallet_count}) exceeds every available layout's capacity")
            return {
                "no_valid_layout": True,
                "message": f"No configured layout supports {self.confirmed_pallet_count} pallets on {self.vehicle_id.name}.",
            }
        target_capacity = self.vehicle_id.get_layout_capacity(target_layout)
        if self.confirmed_pallet_count <= tpl.max_positions and tpl.layout_type == target_layout:
            return None
        candidate = self.env["prema.dispatch.vehicle.layout.template"].search([
            ("id", "!=", tpl.id), ("active", "=", True),
            ("layout_type", "=", target_layout),
            ("max_positions", "=", target_capacity),
            "|", ("applicable_vehicle_ids", "in", [self.vehicle_id.id]), ("applicable_vehicle_ids", "=", False),
        ], order="max_positions asc", limit=1)
        if not candidate:
            self._mark_stale(f"Configured {target_layout} layout template is missing for {self.vehicle_id.name}")
            return {
                "no_valid_layout": True,
                "message": f"No {target_layout.replace('_', ' ')} layout template with capacity {target_capacity} is configured for {self.vehicle_id.name}.",
            }
        proposal = self.change_layout(candidate.id, version=self.version, confirm_remap=False)
        proposal["notification"] = (
            f"Suggested layout: {candidate.layout_type} ({target_capacity} positions) for "
            f"{self.confirmed_pallet_count} confirmed pallets."
        )
        return proposal

    # ── Validation / stale ────────────────────────────────────────────

    def validate_load_plan(self):
        self.ensure_one()
        blocking, warnings = [], []
        tpl = self.layout_template_id
        floor_items = self.pallet_ids.filtered(lambda i: i.status != "cancelled" and i.consumes_floor_position)
        if self.confirmed_pallet_count > tpl.max_positions:
            blocking.append(f"Confirmed pallet count ({self.confirmed_pallet_count}) exceeds layout capacity ({tpl.max_positions}).")
        # Future-pickup freight is not physically on the truck yet, so it
        # has no position binding — but a job with a PENDING reservation has
        # a planned slot and is fully planned. Only unreserved future
        # pallets (or physical pallets with no position) block validation.
        reserved_job_ids = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True),
            ("related_pickup_stop_id", "!=", False),
        ]).mapped("related_pickup_stop_id.job_id.id")
        unassigned = floor_items.filtered(
            lambda i: not i.position_id and (
                not i.pending_future_pickup or i.job_id.id not in reserved_job_ids))
        if unassigned:
            blocking.append(f"{len(unassigned)} pallet(s) have no assigned position.")
        if tpl.max_payload_lbs and self.payload_used > tpl.max_payload_lbs:
            warnings.append(f"Payload used ({self.payload_used:.0f} lbs) exceeds template max ({tpl.max_payload_lbs:.0f} lbs).")
        for item in floor_items.filtered("position_id"):
            pos = item.position_id
            if item.four_way_entry and not pos.four_way_required:
                pass  # a four-way pallet in a non-four-way slot is fine; the reverse is the real risk, covered below
            if pos.four_way_required and not item.four_way_entry:
                warnings.append(f"{item.name} is in position {pos.position_code}, which requires four-way entry.")
            if pos.max_weight_lbs and item.weight_lbs and item.weight_lbs > pos.max_weight_lbs:
                warnings.append(f"{item.name} ({item.weight_lbs:.0f} lbs) exceeds position {pos.position_code}'s max ({pos.max_weight_lbs:.0f} lbs).")
        # Accessibility: an early-stop item positioned "behind" (farther from
        # rear) a later-stop item on the same side is blocked by it.
        by_side = {}
        for item in floor_items.filtered("position_id"):
            allocs = item.stop_allocation_ids.filtered("active") or self.env["prema.dispatch.pallet.stop.allocation"]
            min_seq = min(allocs.mapped("stop_id.sequence")) if allocs else (item.delivery_stop_id.sequence or 0)
            by_side.setdefault(item.position_id.side, []).append((item, min_seq, item.position_id.distance_from_rear_in))
        for side, entries in by_side.items():
            entries.sort(key=lambda e: e[1])  # by earliest stop sequence
            for i, (item, seq, dist) in enumerate(entries):
                for other_item, other_seq, other_dist in entries[i + 1:]:
                    if other_seq >= seq and other_dist < dist:
                        warnings.append(
                            f"{item.name} (earlier stop) is behind {other_item.name} (later stop) on the {side} side — review accessibility before departure."
                        )
        if self.is_stale:
            warnings.append(f"Load plan is stale: {self.stale_reason or 'inputs changed since planning'}.")
        if not self._layout_is_vehicle_verified():
            warnings.append(
                "UNVERIFIED VEHICLE LAYOUT — Dimensions and capacity have not yet been physically "
                f"verified for '{tpl.name}'. Positions shown are a planning aid, not a guarantee that "
                "these pallets will physically fit. Legal payload/axle-weight limits are separate from "
                "this position count and are not verified by this feature."
                + ("" if self.unverified_layout_acknowledged else " Dispatcher acknowledgement is required before confirming loading.")
            )
        required_docs_missing = self.load_plan_job_ids.filtered("active") and not self.document_ids.filtered(lambda d: d.document_type == "route_sheet")
        if required_docs_missing:
            warnings.append("No route sheet uploaded yet.")
        return {"blocking": blocking, "warnings": warnings, "valid": not blocking}

    def clear_stale(self, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        validation = self.validate_load_plan()
        if validation["blocking"]:
            raise UserError("Cannot clear stale status while blocking errors remain: " + "; ".join(validation["blocking"]))
        self.write({"is_stale": False, "stale_reason": False, "stale_since": False, "stale_triggered_by": False})
        self._log_event("stale_cleared")
        self._bump_version()
        return self.get_load_plan()

    def acknowledge_unverified_layout(self, reason=None):
        """Dispatcher-only (not driver/warehouse): explicit sign-off that
        they understand this template's dimensions/capacity are unverified.
        Required before confirm_loading() when the current template is
        unverified — see validate_load_plan()'s warning text.

        The gate is the LAYOUT TEMPLATE's own verification flag — the exact
        context the "UNVERIFIED VEHICLE LAYOUT" banner keys off. It must NOT
        use _layout_is_vehicle_verified(): that vehicle-level check can be
        True (vehicle config verified + capacity match) while the template
        is still unverified, which made this button silently no-op. And
        acknowledging NEVER flips the template's is_verified or the
        vehicle's layout_configuration_verified — physical verification is
        a separate, manager-only action."""
        self.ensure_one()
        self._check_dispatch_staff_or_raise()
        if self.layout_template_id.is_verified:
            return self.get_load_plan()  # nothing to acknowledge
        self.write({
            "unverified_layout_acknowledged": True,
            "unverified_layout_acknowledged_by": self.env.user.id,
            "unverified_layout_acknowledged_at": fields.Datetime.now(),
        })
        self._log_event("unverified_layout_acknowledged", reason=reason)
        self._bump_version()
        return self.get_load_plan()

    def recommend_updated_layout(self):
        self.ensure_one()
        self._check_access()
        from odoo.addons.prema_dispatch.services.dispatch_recommendation_service import DispatchRecommendationService
        recommendation = DispatchRecommendationService(self.env).recommend(self)
        self._log_event("recommendation_generated", new_value=recommendation)
        return recommendation

    def recommend_layout(self):
        return self.recommend_updated_layout()

    def _set_job_reserved_count(self, job_id):
        """Mirror the pending reserve-position operations for one job onto
        the load-plan-job link's reserved_floor_positions — the canonical
        knob feeding the Reserved / Committed / Available header counts.
        Operations are the position-level truth; the link count is the
        aggregate the counts read."""
        self.ensure_one()
        ops = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True),
            ("related_pickup_stop_id.job_id", "=", job_id),
        ])
        link = self.load_plan_job_ids.filtered(lambda l: l.active and l.job_id.id == job_id)[:1]
        if link:
            count = len(ops)
            vals = {"reserved_floor_positions": count}
            if count and not link.reservation_source:
                vals["reservation_source"] = "dispatcher_override"
            if link.reserved_floor_positions != count or not link.reservation_source:
                link.write(vals)
        return len(ops)

    def _reserve_future_position(self, placement):
        """Persist ONE pending reserve_position operation for freight that
        is planned but not physically picked up yet. Idempotent per
        position: a second accept of the same proposal reuses the existing
        operation instead of duplicating it. The operation (not the item)
        is the authority until confirm_future_pickup_operation binds the
        real pallet at pickup time."""
        self.ensure_one()
        pos = self.env["prema.dispatch.vehicle.layout.position"].browse(placement["position_id"])
        item = self.env["prema.dispatch.item"].browse(placement.get("item_id"))
        job = self.env["prema.dispatch.job"].browse(placement.get("job_id")) if placement.get("job_id") else (item.job_id if item else False)
        if not pos.exists() or pos.layout_template_id.id != self.layout_template_id.id:
            raise UserError("Invalid position for this load plan's layout.")
        if pos.blocked:
            raise UserError(f"Position {pos.position_code} is blocked: {pos.blocked_reason or 'not available'}.")
        occupant = self.pallet_ids.filtered(
            lambda i: i.status != "cancelled" and not i.pending_future_pickup
            and i.position_id.id == pos.id)
        if occupant:
            raise UserError(f"Position {pos.position_code} is already occupied by {occupant[0].name}.")
        existing = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True), ("to_position_id", "=", pos.id),
        ], limit=1)
        if existing:
            other_job = existing.related_pickup_stop_id.job_id if existing.related_pickup_stop_id else False
            if job and other_job and other_job.id != job.id:
                raise UserError(f"Position {pos.position_code} is already reserved for another job.")
            if not existing.related_pickup_stop_id and job:
                pickup_stop = job.stop_ids.filtered(
                    lambda s: s.stop_type == "pickup" and not s.planning_only)[:1]
                if pickup_stop:
                    existing.write({"related_pickup_stop_id": pickup_stop.id})
            return existing
        pickup_stop = False
        if job:
            pickup_stop = job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup" and not s.planning_only)[:1]
        op = self.env["prema.dispatch.load.plan.operation"].create({
            "load_plan_id": self.id, "operation_type": "reserve_position",
            "to_position_id": pos.id,
            "related_pickup_stop_id": pickup_stop.id if pickup_stop else False,
            "reason": f"Reserved for future pickup: {job.name if job else 'unknown job'}",
        })
        if job:
            self._set_job_reserved_count(job.id)
        return op

    def _clear_stale_if_no_blocking(self):
        """Drop the stale flag after a planner action WHEN the plan now
        validates — never blindly (a remaining blocking error keeps the
        stale state so the planner still sees the plan needs attention)."""
        self.ensure_one()
        if not self.is_stale:
            return
        validation = self.validate_load_plan()
        if validation["blocking"]:
            return
        self.write({
            "is_stale": False, "stale_reason": False,
            "stale_since": False, "stale_triggered_by": False,
        })
        self._log_event("stale_cleared")

    def accept_recommendation(self, recommendation, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        for placement in recommendation.get("positions", []):
            if placement.get("future"):
                # Planning commitment for freight not yet picked up: reserve
                # the proposed position (operation + link count). The item
                # keeps position_id empty until the pickup actually happens.
                self._reserve_future_position(placement)
                continue
            item = self.env["prema.dispatch.item"].browse(placement["item_id"])
            if item.exists() and item.load_plan_id.id == self.id:
                item.write({"position_id": placement["position_id"]})
        self._log_event("recommendation_accepted", new_value=recommendation)
        self._clear_stale_if_no_blocking()
        self._bump_version()
        return self.get_load_plan()

    def reject_recommendation(self, recommendation):
        self.ensure_one()
        self._check_access()
        self._log_event("recommendation_rejected", old_value=recommendation)
        return {"success": True}

    # ── Loading / locking ─────────────────────────────────────────────

    def mark_pallet_loaded(self, item_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        item.write({"status": "loaded", "loaded_at": fields.Datetime.now(), "loaded_by": self.env.user.id})
        self._log_event("pallet_loaded", item=item)
        self._bump_version()
        return self.get_load_plan()

    def mark_pallet_unloaded(self, item_id, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        item = self.env["prema.dispatch.item"].browse(item_id)
        item.write({"status": "delivered", "unloaded_at": fields.Datetime.now(), "unloaded_by": self.env.user.id})
        item._release_position_on_delivery()
        self._bump_version()
        return self.get_load_plan()

    def confirm_loading(self, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        if not self._layout_is_vehicle_verified() and not self.unverified_layout_acknowledged:
            raise UserError(
                "UNVERIFIED VEHICLE LAYOUT — Dimensions and capacity have not yet been physically "
                "verified. A dispatcher must acknowledge this before loading can be confirmed."
            )
        # A reserved position is a PLAN, not cargo on the truck: loading can
        # only be confirmed after the freight was physically received at its
        # pickup (available_after_stop gets actual_departure_time, which
        # flips pending_future_pickup off). The planning UI must never skip
        # the driver pickup.
        future_items = self.pallet_ids.filtered(
            lambda i: i.status != "cancelled" and i.pending_future_pickup)
        if future_items:
            names = ", ".join(future_items[:3].mapped("name")) + (
                "…" if len(future_items) > 3 else "")
            raise UserError(
                f"Cannot confirm loading: {names} {'has' if len(future_items) == 1 else 'have'} not "
                "been physically picked up yet. Complete the pickup/receiving workflow first — a "
                "reserved position is a plan, not cargo on the truck."
            )
        validation = self.validate_load_plan()
        if validation["blocking"]:
            raise UserError("Cannot confirm loading: " + "; ".join(validation["blocking"]))
        self.write({"state": "loaded"})
        self._bump_version()
        return self.get_load_plan()

    def _build_snapshot(self):
        self.ensure_one()
        payload = self.get_load_plan()
        payload["snapshot_at"] = fields.Datetime.now().isoformat()
        return payload

    def lock_load_plan(self, reason=None, auto=False):
        self.ensure_one()
        if self.is_locked:
            return self.get_load_plan()
        validation = self.validate_load_plan()
        if validation["blocking"]:
            if auto:
                self._log_event("locked", reason=f"Auto-lock deferred: {'; '.join(validation['blocking'])}")
                return {"success": False, "deferred": True, "blocking": validation["blocking"]}
            raise UserError("Cannot lock: " + "; ".join(validation["blocking"]))
        if not auto:
            self._check_access(require_not_locked=False)
        snapshot = self._build_snapshot()
        self.write({
            "is_locked": True, "locked_at": fields.Datetime.now(),
            "locked_by": self.env.user.id, "lock_reason": reason or ("Auto-locked at pickup departure" if auto else "Manual lock"),
            "final_snapshot_json": json.dumps(snapshot),
        })
        self._log_event("locked", reason=reason, snapshot=snapshot)
        self._bump_version()
        return self.get_load_plan()

    def unlock_load_plan(self, reason):
        self.ensure_one()
        self._check_dispatch_staff_or_raise(require_manager=True)
        if not reason:
            raise UserError("A reason is required to unlock a load plan.")
        self.write({"is_locked": False, "lock_reason": False})
        self._log_event("unlocked", reason=reason)
        self._bump_version()
        return self.get_load_plan()

    # ── Transfer handoff ───────────────────────────────────────────────

    def execute_handoff(self, item_id, to_vehicle_id, to_operating_date=None, to_driver_id=None, evidence_attachment_ids=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        item = self.env["prema.dispatch.item"].browse(item_id)
        if not item.exists() or item.load_plan_id.id != self.id:
            raise UserError("Item not found on this load plan.")
        to_date = to_operating_date or self.operating_date
        receiving = self.search([
            ("vehicle_id", "=", to_vehicle_id), ("operating_date", "=", to_date),
            ("origin_stop_id", "=", False), ("active", "=", True),
        ], limit=1)
        if not receiving:
            templates = self.get_layout_templates(to_vehicle_id)
            if not templates:
                raise UserError("No layout template available for the receiving vehicle.")
            receiving = self.create({
                "vehicle_id": to_vehicle_id, "operating_date": to_date, "driver_id": to_driver_id,
                "layout_template_id": templates[0]["id"], "layout_template_revision": templates[0]["revision"],
            })
        old_position = item.position_id
        item.write({"load_plan_id": receiving.id, "position_id": False})
        if evidence_attachment_ids:
            item.write({"evidence_attachment_ids": [(4, aid) for aid in evidence_attachment_ids]})
        self._log_event("pallet_handed_off", item=item, from_position=old_position, to_plan=receiving)
        receiving._log_event("pallet_received", item=item, from_plan=self, to_position=False)
        self._bump_version()
        return {"from_load_plan": self.get_load_plan(), "to_load_plan": receiving.get_load_plan()}

    def report_exception(self, item_id, exception_type, notes, photo_attachment_ids=None):
        self.ensure_one()
        self._check_access()
        item = self.env["prema.dispatch.item"].browse(item_id)
        item.write({
            "exception_state": exception_type, "damage_notes": notes,
            "damage_reported_by": self.env.user.id, "damage_reported_at": fields.Datetime.now(),
        })
        if photo_attachment_ids:
            for aid in photo_attachment_ids:
                self.env["prema.dispatch.document"].create({
                    "attachment_id": aid, "document_type": "damage_photo",
                    "load_plan_id": self.id, "item_id": item.id,
                })
        self._log_event("damage_reported", item=item, reason=notes)
        return self.get_load_plan()

    # ── Physical route visits: combine shared delivery addresses ──────

    def find_shared_visit_candidates(self):
        """Stops across the jobs on this Load Plan that share a saved_location_id
        and aren't already linked into the same route visit. Returns a list of
        {saved_location_id, address, stop_ids: [...]} groups with 2+ stops."""
        self.ensure_one()
        jobs = self.load_plan_job_ids.filtered("active").mapped("job_id")
        stops = jobs.mapped("stop_ids").filtered(
            lambda s: s.saved_location_id and s.stop_type in ("dropoff", "delivery")
        )
        groups = {}
        for stop in stops:
            groups.setdefault(stop.saved_location_id.id, []).append(stop)
        candidates = []
        for loc_id, group_stops in groups.items():
            job_ids = {s.job_id.id for s in group_stops}
            if len(group_stops) < 2 or len(job_ids) < 2:
                continue
            already_linked = self.env["prema.dispatch.route.visit.stop"].search([
                ("stop_id", "in", [s.id for s in group_stops]), ("active", "=", True),
            ])
            if len(already_linked) >= len(group_stops):
                continue
            candidates.append({
                "saved_location_id": loc_id,
                "address": group_stops[0].saved_location_id.address,
                "stop_ids": [s.id for s in group_stops],
            })
        return candidates

    def combine_physical_visit(self, stop_ids):
        """Link 2+ delivery stops (from different, financially separate jobs)
        that are physically the same address into ONE route visit — one map
        marker / navigation destination / arrival event, while every stop
        keeps its own job, invoice, pallet allocation, POD and completion
        status. Never merges the underlying financial jobs."""
        self.ensure_one()
        self._check_access()
        stops = self.env["prema.dispatch.stop"].browse(stop_ids)
        if len(stops) < 2:
            raise UserError("At least two stops are required to combine a physical visit.")
        locations = stops.mapped("saved_location_id")
        if len(locations) != 1:
            raise UserError("All stops must share the same Saved Location to be combined into one physical visit.")
        job_ids = set(stops.mapped("job_id.id"))
        plan_job_ids = set(self.load_plan_job_ids.filtered("active").mapped("job_id.id"))
        if not job_ids.issubset(plan_job_ids):
            raise UserError("All stops must belong to jobs linked to this Load Plan.")
        visit = self.env["prema.dispatch.route.visit"].create({
            "load_plan_id": self.id, "operating_date": self.operating_date,
            "vehicle_id": self.vehicle_id.id, "driver_id": self.driver_id.id,
            "visit_type": "delivery", "saved_location_id": locations.id,
            "address": locations.address, "effective_lat": locations.pin_lat,
            "effective_lng": locations.pin_lng,
        })
        for stop in stops:
            self.env["prema.dispatch.route.visit.stop"].create({
                "route_visit_id": visit.id, "stop_id": stop.id,
            })
        self._log_event("stop_allocation_changed", reason=f"Combined {len(stops)} stops into one physical visit at {locations.address}")
        return {"success": True, "route_visit_id": visit.id, "stop_ids": stop_ids}

    # ── Future pickup: reserve positions, compute exact rehandle plan ──

    def reserve_future_positions(self, job_id, count, version=None):
        """Reserve `count` currently-vacant positions for freight that will
        physically be picked up later (e.g. a second pickup on the same
        route/truck). Prefers positions nearest the door (lowest
        distance_from_rear_in) so the future pickup needs the fewest
        rehandles. Creates prema.dispatch.load.plan.operation rows of type
        reserve_position with item_id left empty until the pickup happens."""
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            raise UserError("Job not found.")
        occupied_ids = set(self.pallet_ids.filtered(lambda i: i.status != "cancelled").mapped("position_id.id"))
        already_reserved_ids = set(self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True),
        ]).mapped("to_position_id.id"))
        blocked_ids = occupied_ids | already_reserved_ids
        candidates = self.layout_template_id.position_ids.filtered(
            lambda p: p.id not in blocked_ids and not p.blocked
        ).sorted(key=lambda p: p.distance_from_rear_in or 0)
        if len(candidates) < count:
            raise UserError(f"Only {len(candidates)} accessible position(s) available; cannot reserve {count}.")
        chosen = candidates[:count]
        pickup_stop = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        ops = self.env["prema.dispatch.load.plan.operation"]
        for pos in chosen:
            ops |= ops.create({
                "load_plan_id": self.id, "operation_type": "reserve_position",
                "to_position_id": pos.id, "related_pickup_stop_id": pickup_stop.id if pickup_stop else False,
                "reason": f"Reserved for future pickup: {job.name}",
            })
        self._set_job_reserved_count(job.id)
        self._log_event("stop_allocation_changed", reason=f"Reserved {count} position(s) for future pickup on {job.name}")
        self._bump_version()
        return {"success": True, "reserved_position_ids": chosen.ids, "operation_ids": ops.ids}

    def get_future_pickup_plan(self, job_id):
        """Return the exact, position-level loading instructions for a
        reserved future pickup: if the reserved positions are still vacant,
        say so explicitly (no rehandle needed); if another item now occupies
        a reserved position, generate exact temporary_unload + reload
        operations naming the specific blocking item and position, in the
        correct sequence."""
        self.ensure_one()
        job = self.env["prema.dispatch.job"].browse(job_id)
        reservations = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True),
            ("related_pickup_stop_id.job_id", "=", job_id),
        ])
        if not reservations:
            return {"success": True, "reserved_positions": [], "steps": [], "rehandle_required": False}

        steps = []
        rehandle_required = False
        sequence = 10
        for res in reservations:
            pos = res.to_position_id
            blocker = self.pallet_ids.filtered(
                lambda i, p=pos: i.status != "cancelled" and i.position_id.id == p.id
            )
            if blocker:
                rehandle_required = True
                steps.append({
                    "sequence": sequence, "action": "temporary_unload",
                    "item_id": blocker[0].id, "item_name": blocker[0].name,
                    "position_code": pos.position_code,
                })
                sequence += 10
        for res in reservations:
            steps.append({
                "sequence": sequence, "action": "load_future_pickup",
                "position_code": res.to_position_id.position_code,
                "operation_id": res.id,
            })
            sequence += 10
        for res in reservations:
            pos = res.to_position_id
            blocker = self.pallet_ids.filtered(
                lambda i, p=pos: i.status != "cancelled" and i.position_id.id == p.id
            )
            if blocker:
                steps.append({
                    "sequence": sequence, "action": "reload",
                    "item_id": blocker[0].id, "item_name": blocker[0].name,
                    "position_code": pos.position_code,
                })
                sequence += 10
        return {
            "success": True,
            "reserved_positions": [{"position_code": r.to_position_id.position_code, "operation_id": r.id} for r in reservations],
            "steps": steps, "rehandle_required": rehandle_required,
            "message": "NO TEMPORARY UNLOADING REQUIRED" if not rehandle_required else "Rehandle required — follow steps in exact sequence.",
        }

    def confirm_future_pickup_operation(self, operation_id, item_id, version=None):
        """Load the actual physical item into its reserved position and mark
        the reservation completed. Any temporary_unload/reload steps for
        blocking items must already have been performed by the driver."""
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        op = self.env["prema.dispatch.load.plan.operation"].browse(operation_id)
        if not op.exists() or op.load_plan_id.id != self.id:
            raise UserError("Operation not found on this load plan.")
        pos = op.to_position_id
        occupant = self.pallet_ids.filtered(lambda i: i.position_id.id == pos.id and i.status != "cancelled")
        if occupant:
            raise UserError(f"Position {pos.position_code} is still occupied by {occupant[0].name}; complete the rehandle steps first.")
        item = self.env["prema.dispatch.item"].browse(item_id)
        item.write({"position_id": pos.id, "status": "loaded", "loaded_at": fields.Datetime.now(), "loaded_by": self.env.user.id})
        op.write({"item_id": item.id, "state": "completed", "completed_by": self.env.user.id, "completed_at": fields.Datetime.now()})
        self._set_job_reserved_count(item.job_id.id)
        self._log_event("pallet_loaded", item=item, to_position=pos, reason="Future pickup confirmed")
        self._bump_version()
        return self.get_load_plan()

    def release_future_reservation(self, position_id, version=None):
        """Cancel a pending future-pickup reservation: the position returns
        to vacant and the job link's reserved count is recomputed from the
        remaining pending operations. No-op on unreserved positions."""
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        pos = self.env["prema.dispatch.vehicle.layout.position"].browse(position_id)
        ops = self.env["prema.dispatch.load.plan.operation"].search([
            ("load_plan_id", "=", self.id), ("operation_type", "=", "reserve_position"),
            ("state", "=", "pending"), ("active", "=", True), ("to_position_id", "=", pos.id),
        ])
        if not ops:
            raise UserError("No pending reservation on this position.")
        job_ids = {o.related_pickup_stop_id.job_id.id for o in ops if o.related_pickup_stop_id}
        ops.write({"state": "cancelled", "active": False})
        for job_id in job_ids:
            self._set_job_reserved_count(job_id)
        self._log_event("stop_allocation_changed",
                        reason=f"Released future-pickup reservation on {pos.position_code}")
        self._clear_stale_if_no_blocking()
        self._bump_version()
        return self.get_load_plan()

    # ── Documents (reuses the Phase 1C validator, no second one) ──────

    def upload_document(self, document_type, filename, data_b64, stop_id=None, item_id=None):
        self.ensure_one()
        self._check_access()
        from odoo.addons.prema_dispatch.services.dispatch_upload import decode_and_validate, find_duplicate, UploadError
        try:
            validated = decode_and_validate(data_b64, filename, category=document_type)
        except UploadError as e:
            return {"success": False, "code": e.code, "message": e.message}
        existing_atts = self.document_ids.filtered(lambda d: d.document_type == document_type).mapped("attachment_id")
        dup = find_duplicate(self.env, existing_atts, validated["checksum_sha256"])
        if dup:
            return {"success": True, "duplicate": True, "message": "This file was already uploaded."}
        att = self.env["ir.attachment"].create({
            "name": validated["filename"], "type": "binary",
            "datas": __import__("base64").b64encode(validated["data"]),
            "res_model": "prema.dispatch.load.plan", "res_id": self.id,
            "mimetype": validated["mimetype"],
        })
        doc = self.env["prema.dispatch.document"].create({
            "attachment_id": att.id, "document_type": document_type, "load_plan_id": self.id,
            "stop_id": stop_id, "item_id": item_id, "checksum": validated["checksum_sha256"],
        })
        self._log_event("document_uploaded", item=self.env["prema.dispatch.item"].browse(item_id) if item_id else None,
                         new_value={"document_type": document_type, "attachment_id": att.id})
        return {"success": True, "id": doc.id, "attachment_id": att.id, "url": f"/web/content/{att.id}"}

    def get_documents(self):
        self.ensure_one()
        self._check_access()
        return [{
            "id": d.id, "document_type": d.document_type, "name": d.attachment_id.name,
            "url": f"/web/content/{d.attachment_id.id}", "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else False,
        } for d in self.document_ids.filtered("active")]


class PremaDispatchLoadPlanJob(models.Model):
    _name = "prema.dispatch.load.plan.job"
    _description = "Load Plan <-> Job membership (physical aggregate; financials stay separate)"

    load_plan_id = fields.Many2one("prema.dispatch.load.plan", required=True, ondelete="cascade", index=True)
    job_id = fields.Many2one("prema.dispatch.job", required=True, ondelete="cascade", index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", related="load_plan_id.vehicle_id", store=True)
    driver_id = fields.Many2one("res.partner", related="load_plan_id.driver_id", store=True)
    origin_stop_id = fields.Many2one("prema.dispatch.stop")
    planned_pickup_sequence = fields.Integer()
    planned_delivery_sequence = fields.Integer()
    state = fields.Selection([
        ("included", "Included"), ("staged", "Staged"), ("active", "Active"),
        ("completed", "Completed"), ("removed", "Removed"),
    ], default="included")
    reserved_floor_positions = fields.Integer()
    reserve_capacity = fields.Boolean()
    reservation_source = fields.Selection([("booking_estimate", "Booking Estimate"), ("dispatcher_override", "Dispatcher Override"), ("confirmed_items", "Confirmed Items")])
    reservation_notes = fields.Text()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("job_unique_per_plan", "unique(load_plan_id, job_id)", "This job is already on this load plan."),
    ]


class PremaDispatchJobLoadPlanAutolock(models.Model):
    """Extends prema.dispatch.job (existing model, untouched otherwise) with
    the auto-lock trigger only — kept in this file/class rather than editing
    dispatch_job.py's own write() to avoid touching a large, working method."""
    _inherit = "prema.dispatch.job"

    def write(self, vals):
        watch = "stage_id" in vals
        old_codes = {j.id: j.stage_id.code for j in self} if watch else {}
        res = super().write(vals)
        if watch:
            for job in self:
                if old_codes.get(job.id) != "picked_up" and job.stage_id.code == "picked_up":
                    job._try_autolock_load_plans()
        return res

    def _try_autolock_load_plans(self):
        links = self.env["prema.dispatch.load.plan.job"].search([("job_id", "=", self.id), ("active", "=", True)])
        for plan in links.mapped("load_plan_id").filtered(lambda p: not p.is_locked):
            try:
                plan.lock_load_plan(auto=True)
            except Exception:
                _logger.exception("Auto-lock attempt failed for load plan %s", plan.id)

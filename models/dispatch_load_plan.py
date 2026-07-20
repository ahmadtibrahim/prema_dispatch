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
    confirmed_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    assigned_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    loaded_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    vacant_position_count = fields.Integer(compute="_compute_counts", store=True)
    reserved_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    committed_pallet_count = fields.Integer(compute="_compute_counts", store=True)
    available_position_count = fields.Integer(compute="_compute_counts", store=True)
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
        "load_plan_job_ids.job_id.approximate_skids", "load_plan_job_ids.reserved_floor_positions", "load_plan_job_ids.reserve_capacity",
        "pallet_ids", "pallet_ids.consumes_floor_position", "pallet_ids.position_id",
        "pallet_ids.status", "pallet_ids.weight_lbs",
        "layout_template_id.max_positions", "vehicle_id.x_max_payload_lbs",
    )
    def _compute_counts(self):
        for plan in self:
            floor_items = plan.pallet_ids.filtered(lambda i: i.status != "cancelled" and i.consumes_floor_position)
            plan.expected_pallet_count = sum(plan.load_plan_job_ids.mapped("job_id.approximate_skids"))
            plan.confirmed_pallet_count = len(floor_items.filtered(lambda i: i.status != "cancelled"))
            plan.reserved_pallet_count = sum(plan.load_plan_job_ids.filtered("active").mapped("reserved_floor_positions"))
            commitment = 0
            for link in plan.load_plan_job_ids.filtered("active"):
                job_items = floor_items.filtered(lambda i, job=link.job_id: i.job_id.id == job.id)
                commitment += max(link.reserved_floor_positions or 0, len(job_items))
            plan.committed_pallet_count = commitment
            plan.assigned_pallet_count = len(floor_items.filtered("position_id"))
            plan.loaded_pallet_count = len(floor_items.filtered(lambda i: i.status in ("loaded", "in_transit", "delivered")))
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
        items = self.pallet_ids.filtered(lambda i: i.status != "cancelled")
        items_by_position = {i.position_id.position_code: i for i in items if i.position_id}

        def item_payload(item):
            allocs = self.env["prema.dispatch.pallet.stop.allocation"].search([
                ("dispatch_item_id", "=", item.id), ("active", "=", True),
            ])
            return {
                "id": item.id, "name": item.name, "load_unit_type": item.load_unit_type,
                "shared_skid": item.shared_skid, "status": item.status,
                "weight_lbs": item.weight_lbs, "position_id": item.position_id.id,
                "position_code": item.position_id.position_code if item.position_id else False,
                "exception_state": item.exception_state,
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
                                 "revision": tpl.revision, "is_verified": tpl.is_verified},
            "is_locked": self.is_locked, "lock_reason": self.lock_reason,
            "is_stale": self.is_stale, "stale_reason": self.stale_reason,
            "unverified_layout_acknowledged": self.unverified_layout_acknowledged,
            "counts": {
                "expected": self.expected_pallet_count, "confirmed": self.confirmed_pallet_count,
                "assigned": self.assigned_pallet_count, "loaded": self.loaded_pallet_count,
                "reserved": self.reserved_pallet_count, "committed": self.committed_pallet_count,
                "vacant": self.vacant_position_count, "available": self.available_position_count, "max_positions": tpl.max_positions,
                "utilization_percentage": round(self.utilization_percentage, 1),
            },
            "payload": {"used": self.payload_used, "capacity": self.payload_capacity},
            "jobs": [{
                "load_plan_job_id": j.id, "job_id": j.job_id.id, "job_name": j.job_id.name,
                "customer": j.job_id.partner_id.name, "state": j.state,
            } for j in self.load_plan_job_ids.filtered("active")],
            "positions": [{
                "id": p.id, "position_code": code, "display_name": p.display_name or code, "side": p.side,
                "sequence": p.sequence, "x": p.x_coordinate, "y": p.y_coordinate,
                "width": p.display_width, "height": p.display_height, "blocked": p.blocked,
                "item": item_payload(items_by_position[code]) if code in items_by_position else False,
            } for code, p in positions_by_code.items()],
            "unassigned_items": [item_payload(i) for i in items if not i.position_id and i.consumes_floor_position],
            "non_floor_items": [item_payload(i) for i in items if not i.consumes_floor_position],
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
        self.create_load_plan(vehicle_id, operating_date, driver_id=driver_id)
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
    def create_load_plan(self, vehicle_id, operating_date, layout_template_id=None, driver_id=None, origin_stop_id=None):
        self._check_dispatch_staff_or_raise()
        if not layout_template_id:
            templates = self.get_layout_templates(vehicle_id)
            if not templates:
                raise UserError("No layout template available for this vehicle. Create one first.")
            # Straight is the documented default (0-12 pallets); don't fall
            # back to whichever template happens to sort first alphabetically.
            straight = [t for t in templates if t["layout_type"] == "straight"]
            layout_template_id = (straight or templates)[0]["id"]
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
        self.env["prema.dispatch.load.plan.job"].create({"load_plan_id": self.id, "job_id": job.id})
        self._mark_stale("Job added to load plan")
        self._log_event("job_added", new_value={"job_id": job.id})
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
        pos = self._get_position(position_id)
        occupant = self.pallet_ids.filtered(lambda i: i.position_id.id == pos.id and i.id != item.id and i.status != "cancelled")
        if occupant:
            raise UserError(f"Position {pos.position_code} is already occupied by {occupant[0].name}.")
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
        Alloc = self.env["prema.dispatch.pallet.stop.allocation"]
        for a in stop_allocations:
            existing = Alloc.search([("dispatch_item_id", "=", item.id), ("stop_id", "=", a["stop_id"])])
            vals = {
                "dispatch_item_id": item.id, "stop_id": a["stop_id"],
                "invoice_id": a.get("invoice_id"), "unload_sequence": a.get("unload_sequence", 10),
                "notes": a.get("notes"),
            }
            if existing:
                existing.write(vals)
            else:
                Alloc.create(vals)
        item.write({"shared_skid": len(item.stop_allocation_ids.filtered("active")) > 1})
        self._log_event("stop_allocation_changed", item=item, new_value={"stop_allocations": stop_allocations})
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
        if self.is_locked or self.confirmed_pallet_count <= tpl.max_positions:
            return None
        candidate = self.env["prema.dispatch.vehicle.layout.template"].search([
            ("id", "!=", tpl.id), ("active", "=", True),
            ("layout_type", "!=", "turned"),  # Turned requires manual selection + verified handling requirements — never auto-proposed
            ("max_positions", ">=", self.confirmed_pallet_count),
            "|", ("applicable_vehicle_ids", "in", [self.vehicle_id.id]), ("applicable_vehicle_ids", "=", False),
        ], order="max_positions asc", limit=1)
        if not candidate:
            self._mark_stale(f"Confirmed pallet count ({self.confirmed_pallet_count}) exceeds every available layout's capacity")
            return {
                "no_valid_layout": True,
                "message": f"No layout supports {self.confirmed_pallet_count} pallets without using Turned, which requires manual selection and verified four-way/forklift handling. Consider another truck, splitting the route, removing the additional booking, a manual Turned selection, or an authorized manual plan.",
            }
        proposal = self.change_layout(candidate.id, version=self.version, confirm_remap=False)
        proposal["notification"] = (
            f"Layout changed from {tpl.layout_type} to {candidate.layout_type} because the "
            f"confirmed physical skid count increased to {self.confirmed_pallet_count}."
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
        unassigned = floor_items.filtered(lambda i: not i.position_id)
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
        if not tpl.is_verified:
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
        unverified — see validate_load_plan()'s warning text."""
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

    def accept_recommendation(self, recommendation, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        for placement in recommendation.get("positions", []):
            item = self.env["prema.dispatch.item"].browse(placement["item_id"])
            if item.exists() and item.load_plan_id.id == self.id:
                item.write({"position_id": placement["position_id"]})
        self._log_event("recommendation_accepted", new_value=recommendation)
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
        self._log_event("pallet_unloaded", item=item)
        self._bump_version()
        return self.get_load_plan()

    def confirm_loading(self, version=None):
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)
        if not self.layout_template_id.is_verified and not self.unverified_layout_acknowledged:
            raise UserError(
                "UNVERIFIED VEHICLE LAYOUT — Dimensions and capacity have not yet been physically "
                "verified. A dispatcher must acknowledge this before loading can be confirmed."
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

import uuid

from odoo import api, fields, models


class PremaDispatchItem(models.Model):
    _name = "prema.dispatch.item"
    _description = "Dispatch Freight Item / Skid"
    _order = "job_id, sequence asc"

    qr_token = fields.Char(default=lambda self: str(uuid.uuid4()), copy=False, index=True,
                            help="Opaque public token for the QR code — never the raw database id.")

    job_id = fields.Many2one(
        "prema.dispatch.job", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Label", required=True, default="Skid")
    item_ref = fields.Char(string="Reference / SKU")
    description = fields.Char()

    # Route
    pickup_stop_id = fields.Many2one(
        "prema.dispatch.stop",
        string="Pickup Stop",
        domain="[('job_id', '=', job_id), ('stop_type', '=', 'pickup')]",
    )
    delivery_stop_id = fields.Many2one(
        "prema.dispatch.stop",
        string="Delivery Stop",
        domain="[('job_id', '=', job_id), ('stop_type', '=', 'dropoff')]",
    )
    available_after_stop_id = fields.Many2one(
        "prema.dispatch.stop",
        string="Available After Stop",
        domain="[('job_id', '=', job_id), ('stop_type', 'in', ('pickup', 'cross_dock_pickup'))]",
        help="Freight is physically available only after this pickup/operation stop.",
    )

    # Evidence (canonical prema.dispatch.evidence records for this pallet)
    evidence_count = fields.Integer(
        string="Evidence Records", compute="_compute_evidence_count",
        help="Canonical evidence records for this physical pallet "
             "(POPP photos, seal photos…).",
    )

    def _compute_evidence_count(self):
        Evidence = self.env["prema.dispatch.evidence"]
        for item in self:
            item.evidence_count = Evidence.search_count([("pallet_id", "=", item.id)])

    # ── §10 Progress page: pallet-level POPP and delivery-POD status.
    # POPP belongs to THIS pallet (popp_attachment_ids); the delivery POD
    # is the pallet's delivery stop's POD — read directly, never merged
    # across a shared visit's other jobs.
    popp_status_text = fields.Char(
        string="POPP Status", compute="_compute_popp_status_text")
    pod_status_text = fields.Char(
        string="POD Status", compute="_compute_popp_status_text")

    @api.depends("popp_attachment_ids", "delivery_stop_id.pod_attachment_ids")
    def _compute_popp_status_text(self):
        for item in self:
            popp_n = len(item.popp_attachment_ids)
            item.popp_status_text = (
                f"{popp_n} photo{'' if popp_n == 1 else 's'}" if popp_n else "None")
            pod_stop = item.delivery_stop_id
            pod_n = len(pod_stop.pod_attachment_ids) if pod_stop else 0
            if not pod_stop:
                item.pod_status_text = "—"
            elif pod_n:
                item.pod_status_text = "POD ✓"
            elif pod_stop.status == "completed":
                item.pod_status_text = "POD ⚠ MISSING"
            else:
                item.pod_status_text = "Pending"

    def action_open_evidence(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pallet Evidence",
            "res_model": "prema.dispatch.evidence",
            "view_mode": "list,form",
            "domain": [("pallet_id", "=", self.id)],
            "context": {"default_pallet_id": self.id},
        }

    # Dimensions
    pallet_count = fields.Integer(string="Pallets / Skids", default=1)
    weight_lbs = fields.Float(string="Weight (lbs)", digits=(10, 1))
    length_in = fields.Float(string="Length (in)", digits=(6, 1))
    width_in = fields.Float(string="Width (in)", digits=(6, 1))
    height_in = fields.Float(string="Height (in)", digits=(6, 1))

    # Custody
    current_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Current Truck", ondelete="set null"
    )
    current_driver_id = fields.Many2one(
        "res.partner", string="Current Driver", ondelete="set null"
    )
    current_location_id = fields.Many2one(
        "prema.dispatch.location", string="Current Location", ondelete="set null"
    )
    current_custody_type = fields.Selection([
        ("pending", "Pending Pickup"),
        ("truck", "On Truck"),
        ("cross_dock", "At Cross-Dock"),
        ("location", "At Saved Location / Meet Point"),
        ("delivered", "Delivered"),
    ], default="pending", required=True, string="Current Custody")

    status = fields.Selection([
        ("pending",         "Pending"),
        ("loaded",          "Loaded"),
        ("partially_unloaded", "Partially Unloaded"),
        ("in_transit",      "In Transit"),
        ("cross_docked",    "Cross-Docked / Stored"),
        ("staged",          "Staged / Stored"),
        ("reloaded",        "Reloaded"),
        ("out_for_delivery", "Out for Delivery"),
        ("delivered",       "Delivered"),
        ("held",            "On Hold"),
        ("failed",          "Failed"),
        ("transferred",     "Transferred"),
        ("cancelled",       "Cancelled"),
    ], default="pending", required=True,
        help="Auto-advances on pickup/delivery stop completion (see "
             "dispatch_stop.py's completion actions) — Staged/Reloaded are "
             "manual, for cross-dock handoffs.",
    )

    requires_transfer = fields.Boolean(
        string="Requires Transfer",
        help="This item will be transferred to another truck at a cross-dock point.",
    )
    custody_event_ids = fields.One2many(
        "prema.dispatch.custody.event", "item_id",
        string="Custody History", readonly=True,
    )
    evidence_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_item_evidence_att_rel",
        "item_id",
        "attachment_id",
        string="Transit Evidence",
        help="Pictures and documents that should follow this pallet/skid across "
             "cross-dock storage, handoffs, and final delivery.",
    )
    # POPP — Proof of Pickup Pallet (spec §20): 1-4 photos taken by the
    # driver at the pickup dock, belonging to THIS pallet (not the general
    # pickup stop). The canonical evidence rows (evidence_type "popp") link
    # back through pallet_id; this m2m is the pallet analog of
    # stop.pop_attachment_ids. The cap is enforced in
    # driver_add_evidence (dispatch_job.py).
    popp_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_item_popp_att_rel",
        "item_id",
        "attachment_id",
        string="POPP Evidence",
        help="Proof of Pickup Pallet photos — max 4 per physical pallet.",
    )
    notes = fields.Text()

    # ── Load Plan / pallet-position extension (Phase 2) ───────────────
    load_unit_type = fields.Selection([
        ("pallet", "Pallet"), ("shared_pallet", "Shared Pallet"), ("loose", "Loose Freight"),
        ("carton", "Carton"), ("tote", "Tote"), ("container", "Container"),
        ("equipment", "Equipment"), ("other", "Other"),
    ], default="pallet", required=True)
    consumes_floor_position = fields.Boolean(
        compute="_compute_consumes_floor_position", store=True, readonly=False,
        help="Only items with this set count toward truck layout capacity.",
    )
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="set null", index=True)
    position_id = fields.Many2one("prema.dispatch.vehicle.layout.position", ondelete="set null", index=True)
    stop_allocation_ids = fields.One2many("prema.dispatch.pallet.stop.allocation", "dispatch_item_id")
    shared_skid = fields.Boolean(default=False)
    four_way_entry = fields.Boolean(default=False)
    stackable = fields.Boolean(default=True)
    temperature_zone = fields.Selection([
        ("ambient", "Ambient"), ("chilled", "Chilled"), ("frozen", "Frozen"), ("multi", "Multi-Zone"),
    ])
    # ── Canonical temperature snapshot (18-section §3) ─────────────────
    # Frozen at item creation from the booking pallet / job snapshot.
    # Celsius only; supplied-flags are the existence checks (0°C valid,
    # dry items carry nothing).
    target_temperature_c = fields.Float(string="Target Temperature (°C)")
    minimum_temperature_c = fields.Float(string="Minimum (°C)")
    maximum_temperature_c = fields.Float(string="Maximum (°C)")
    temperature_tolerance_c = fields.Float(string="Tolerance (°C)")
    temperature_supplied = fields.Boolean(string="Temperature Set")
    minimum_temperature_supplied = fields.Boolean(string="Minimum Set")
    maximum_temperature_supplied = fields.Boolean(string="Maximum Set")
    submitted_temperature_unit = fields.Selection(
        [("c", "°C"), ("f", "°F")], string="Submitted In", default="c")
    temperature_requirement_source = fields.Selection(
        [("customer", "Customer"), ("dispatcher", "Dispatcher"),
         ("system", "System"), ("legacy", "Legacy (pre-canonical)")],
        string="Requirement Source", default="customer")
    temperature_display = fields.Char(
        string="Temperature", compute="_compute_temperature_display")
    temperature_range_display = fields.Char(
        string="Range", compute="_compute_temperature_display")

    @api.depends("target_temperature_c", "temperature_supplied",
                 "minimum_temperature_c", "maximum_temperature_c",
                 "minimum_temperature_supplied",
                 "maximum_temperature_supplied", "temperature_tolerance_c")
    def _compute_temperature_display(self):
        try:
            from odoo.addons.prema_logistics_booking.services.temperature_service import (
                format_dual, range_dual, validate_range)
        except ImportError:  # module-load window only
            for item in self:
                item.temperature_display = ""
                item.temperature_range_display = ""
            return
        for item in self:
            item.temperature_display = (
                format_dual(item.target_temperature_c)
                if item.temperature_supplied else "")
            _errors, effective = validate_range(
                item.target_temperature_c, item.minimum_temperature_c,
                item.maximum_temperature_c, item.temperature_tolerance_c,
                target_supplied=item.temperature_supplied,
                minimum_supplied=item.minimum_temperature_supplied,
                maximum_supplied=item.maximum_temperature_supplied)
            item.temperature_range_display = (
                range_dual(effective[0], effective[1]) if effective else "")
    exception_state = fields.Selection([
        ("none", "None"), ("damaged", "Damaged"), ("shortage", "Shortage"),
        ("overage", "Overage"), ("other", "Other"),
    ], default="none")
    pending_future_pickup = fields.Boolean(
        compute="_compute_pending_future_pickup", store=True,
        help="True when this item is tied to a future pickup stop that has not "
             "physically happened yet — excluded from onboard/confirmed/committed "
             "counts until that pickup stop's actual_departure_time is recorded.",
    )
    damage_notes = fields.Text()
    damage_reported_by = fields.Many2one("res.users")
    damage_reported_at = fields.Datetime()
    loaded_at = fields.Datetime()
    unloaded_at = fields.Datetime()
    loaded_by = fields.Many2one("res.users")
    unloaded_by = fields.Many2one("res.users")

    @api.depends("load_unit_type")
    def _compute_consumes_floor_position(self):
        for item in self:
            if not item.id or item._origin.load_unit_type != item.load_unit_type or not item.position_id:
                item.consumes_floor_position = item.load_unit_type in ("pallet", "shared_pallet", "container")

    @api.depends("available_after_stop_id", "available_after_stop_id.actual_departure_time")
    def _compute_pending_future_pickup(self):
        for item in self:
            item.pending_future_pickup = bool(
                item.available_after_stop_id and not item.available_after_stop_id.actual_departure_time
            )

    def write(self, vals):
        dimension_fields = {"weight_lbs", "length_in", "width_in", "height_in"}
        touched = dimension_fields & set(vals.keys())
        affected_plans = self.filtered("position_id").mapped("load_plan_id") if touched else self.env["prema.dispatch.load.plan"]
        res = super().write(vals)
        for plan in affected_plans:
            plan._mark_stale("Pallet dimensions or weight changed after positioning")
        # §15 trigger: pallet/freight change → service durations on the
        # affected route shift (per-pallet service, pallet counts). Same
        # failure envelope as every other trigger: ETA is advisory.
        if any(k in vals for k in ("pallet_count", "status")):
            jobs = self.mapped("job_id").filtered(
                lambda j: not j.stage_id.is_cancelled)
            for job in jobs:
                try:
                    from odoo.addons.prema_logistics_booking.services.eta_engine import (
                        EtaEngine)
                    EtaEngine(self.env).recompute_job(job)
                except Exception:
                    pass
        return res

    @api.model_create_multi
    def create(self, vals_list):
        # ── Canonical temperature snapshot (§3) ───────────────────────
        # Copy the requirement from the linked booking pallet (fallback:
        # the job's frozen snapshot) so the physical item row carries the
        # canonical C-only requirement. Lazy lookups: prema_logistics_
        # booking's models may not be registered yet during module-load
        # test runs — skip quietly in that window (no requirement to
        # copy; the job snapshot covers production paths).
        for vals in vals_list:
            if any(k in vals for k in ("target_temperature_c",
                                       "required_temperature_c")):
                continue  # explicit intake wins
            if "logistics_booking_pallet_id" not in self._fields \
                    or "logistics.booking.pallet" not in self.env.registry.models:
                continue
            pallet = self.env["logistics.booking.pallet"].browse(
                vals.get("logistics_booking_pallet_id"))
            source = pallet if pallet and pallet.temperature_supplied else False
            if not source:
                job = self.env["prema.dispatch.job"].browse(vals.get("job_id"))
                if job and job.temperature_supplied:
                    source = job
            if not source:
                continue
            # Pallet carries target_temperature_c; the job's canonical
            # target IS its legacy required_temperature_c (no mirror).
            target = getattr(source, "target_temperature_c", None)
            if target is None:
                target = source.required_temperature_c
            vals["target_temperature_c"] = target
            vals["minimum_temperature_c"] = source.minimum_temperature_c
            vals["maximum_temperature_c"] = source.maximum_temperature_c
            vals["temperature_tolerance_c"] = source.temperature_tolerance_c
            vals["temperature_supplied"] = source.temperature_supplied
            vals["minimum_temperature_supplied"] = (
                source.minimum_temperature_supplied)
            vals["maximum_temperature_supplied"] = (
                source.maximum_temperature_supplied)
            vals["submitted_temperature_unit"] = (
                source.submitted_temperature_unit or "c")
            vals["temperature_requirement_source"] = (
                source.temperature_requirement_source or "customer")
        items = super().create(vals_list)
        for plan in items.mapped("load_plan_id"):
            plan._mark_stale("Pallet added")
            plan.evaluate_layout_for_capacity()
        for job in items.mapped("job_id").filtered(
                lambda j: not j.stage_id.is_cancelled):
            try:
                from odoo.addons.prema_logistics_booking.services.eta_engine import (
                    EtaEngine)
                EtaEngine(self.env).recompute_job(job)
            except Exception:
                pass
        return items

    def display_label(self):
        self.ensure_one()
        parts = [self.name or "Freight Item"]
        if self.item_ref:
            parts.append(self.item_ref)
        label = " / ".join(parts)
        if self.pallet_count:
            label = f"{label} ({self.pallet_count} pallet{'s' if self.pallet_count != 1 else ''})"
        return label

    def _release_position_on_delivery(self):
        """A fully delivered pallet no longer occupies the truck: release
        its position on the ACTIVE (unlocked) load plan so the diagram shows
        the slot vacant again. Locked/completed plans are frozen execution
        records — their final_snapshot_json is the audit authority, so their
        diagram is left as-planned."""
        for item in self:
            if item.status != "delivered" or not item.position_id or not item.load_plan_id:
                continue
            plan = item.load_plan_id
            if plan.is_locked:
                continue
            old_pos = item.position_id
            item.write({"position_id": False})
            plan._log_event("pallet_unloaded", item=item, from_position=old_pos)

    def _sync_booking_pallet_custody(self):
        """Mirror custody onto the canonical booking pallet (movement_v1).
        Shared pallets go to partially_delivered until their FINAL active
        delivery allocation — never fully delivered at the first stop."""
        if "logistics_booking_pallet_id" not in self._fields:
            return
        for item in self:
            pallet = item.logistics_booking_pallet_id
            if pallet:
                # Runs from the driver app too (stop completion): the
                # booking-pallet movement record is gated by record rules
                # (no group grants the driver access), so mirror custody
                # under sudo — same pattern as _log_custody_event below and
                # the sudo'd booking sync in sync_state_from_dispatch. The
                # driver's authorization was checked upstream
                # (check_stop_access on the job).
                pallet.sudo().sync_custody_from_dispatch_item(item)

    def _log_custody_event(
        self, event_type, stop=None, saved_location=None,
        vehicle=None, driver=None, notes=None,
    ):
        """Append chain-of-custody records without forcing callers to know
        the event model's full schema."""
        Event = self.env["prema.dispatch.custody.event"].sudo()
        for item in self:
            Event.create({
                "item_id": item.id,
                "event_type": event_type,
                "stop_id": stop.id if stop else False,
                "saved_location_id": saved_location.id if saved_location else False,
                "vehicle_id": vehicle.id if vehicle else False,
                "driver_id": driver.id if driver else False,
                "notes": notes or False,
            })

    _sql_constraints = [
        ("qr_token_unique", "unique(qr_token)", "QR token must be unique."),
    ]

    @api.model
    def get_public_pallet_summary(self, token):
        """Public/unauthenticated QR-scan endpoint. Returns ONLY the
        explicitly whitelisted, non-sensitive fields — never the record
        itself, never rates/revenue/customer/invoice data. sudo() is safe
        here specifically because the token is an unguessable UUID and the
        response is a hand-built dict of the safe subset only."""
        item = self.sudo().search([("qr_token", "=", token)], limit=1)
        if not item:
            return {"success": False, "error": "Not found"}
        return {
            "success": True,
            "reference": item.name,
            "truck": item.load_plan_id.vehicle_id.name if item.load_plan_id else False,
            "position": item.position_id.position_code if item.position_id else False,
            "stop_numbers": item.stop_allocation_ids.filtered(lambda a: a.active).mapped("stop_id.sequence"),
            "shared_skid": item.shared_skid,
            "exception_state": item.exception_state,
            "status": item.status,
        }

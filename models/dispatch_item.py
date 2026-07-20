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
    exception_state = fields.Selection([
        ("none", "None"), ("damaged", "Damaged"), ("shortage", "Shortage"),
        ("overage", "Overage"), ("other", "Other"),
    ], default="none")
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

    def write(self, vals):
        dimension_fields = {"weight_lbs", "length_in", "width_in", "height_in"}
        touched = dimension_fields & set(vals.keys())
        affected_plans = self.filtered("position_id").mapped("load_plan_id") if touched else self.env["prema.dispatch.load.plan"]
        res = super().write(vals)
        for plan in affected_plans:
            plan._mark_stale("Pallet dimensions or weight changed after positioning")
        return res

    def create(self, vals_list):
        items = super().create(vals_list)
        for plan in items.mapped("load_plan_id"):
            plan._mark_stale("Pallet added")
            plan.evaluate_layout_for_capacity()
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

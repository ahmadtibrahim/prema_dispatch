from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _


class LogisticsEquipmentProfile(models.Model):
    _name = "logistics.equipment.profile"
    _description = "Equipment — actual fleet vehicle link OR requirement class"
    _order = "is_requirement_class, name"

    name = fields.Char(
        required=True,
        compute="_compute_name", store=True, readonly=False,
    )
    active = fields.Boolean(default=True)

    # ── Mode ─────────────────────────────────────────────────────────
    is_requirement_class = fields.Boolean(
        string="Requirement Class",
        default=False,
        help="ON = defines what a lane/load REQUIRES. OFF = links to actual fleet.vehicle."
    )

    # ── Mode 1 — Link to fleet.vehicle (SINGLE SOURCE OF TRUTH) ─────
    fleet_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Fleet Vehicle", index=True,
        help="Actual physical truck. All capabilities are read-only from fleet.vehicle.",
    )

    @api.onchange("is_requirement_class")
    def _onchange_mode(self):
        """When switching to Actual Fleet, restrict dropdown to available vehicles."""
        if not self.is_requirement_class:
            already_linked = self.search([
                ("is_requirement_class", "=", False),
                ("fleet_vehicle_id", "!=", False),
                ("id", "!=", self.id or 0),
            ]).mapped("fleet_vehicle_id").ids
            domain = [("active", "=", True), ("x_operational_logistics", "=", True)]
            if already_linked:
                domain.append(("id", "not in", already_linked))
            return {"domain": {"fleet_vehicle_id": domain}}

    # ── Computed from fleet.vehicle — READ-ONLY, auto-synced ────────
    license_plate = fields.Char(related="fleet_vehicle_id.license_plate", string="License Plate")
    vehicle_model_name = fields.Char(related="fleet_vehicle_id.model_id.name", string="Vehicle Model")

    body_length_ft = fields.Float(
        string="Cargo Box Length (ft)",
        compute="_compute_from_vehicle", store=True,
    )
    straight_pallet_capacity = fields.Integer(
        string="Straight Pallet Capacity",
        compute="_compute_from_vehicle", store=True,
    )
    pin_wheel_pallet_capacity = fields.Integer(
        string="Pin-Wheel Pallet Capacity",
        compute="_compute_from_vehicle", store=True,
    )
    turned_pallet_capacity = fields.Integer(
        string="Tuned Pallet Capacity",
        compute="_compute_from_vehicle", store=True,
    )
    max_pallets = fields.Integer(
        string="Auto-Booking Max Pallets",  # straight=12, pinwheel available up to 13
        compute="_compute_from_vehicle", store=True,
    )
    max_payload_lbs = fields.Float(
        string="Max Payload (lbs)",
        compute="_compute_from_vehicle", store=True,
    )
    gvwr_lbs = fields.Float(
        compute="_compute_from_vehicle", store=True, string="GVWR (lbs)",
    )
    reefer = fields.Boolean(
        string="Reefer Capable",
        compute="_compute_from_vehicle", store=True,
    )
    liftgate_capable = fields.Boolean(
        compute="_compute_from_vehicle", store=True,
    )
    air_ride = fields.Boolean(
        compute="_compute_from_vehicle", store=True,
    )
    dock_high = fields.Boolean(
        compute="_compute_from_vehicle", store=True,
    )
    stacking_allowed = fields.Boolean(
        default=False,
        help="Whether pallets may be stacked on this equipment.",
    )
    geotab_active = fields.Boolean(
        compute="_compute_from_vehicle", store=True, string="GeoTab/ELD Active",
    )
    fuel_efficiency_km_l = fields.Float(
        compute="_compute_from_vehicle", store=True, string="Fuel Efficiency (km/L)",
    )
    operational = fields.Boolean(
        compute="_compute_from_vehicle", store=True, string="Operational",
    )
    default_pallet_layout = fields.Selection(
        compute="_compute_from_vehicle", store=True,
        selection=[("straight","Straight"),("pin_wheel","Pin-Wheel"),("turned","Tuned")],
    )
    layout_verified = fields.Boolean(
        compute="_compute_from_vehicle", store=True,
    )

    # ── Mode 2 — Requirement Class fields ───────────────────────────
    min_body_length_ft = fields.Float(string="Min Body Length (ft)")
    min_pallets = fields.Integer(string="Minimum Pallets")
    min_payload_lbs = fields.Float(string="Minimum Payload (lbs)")
    reefer_required = fields.Boolean(string="Reefer Required")
    liftgate_required = fields.Boolean(string="Liftgate Required")
    air_ride_preferred = fields.Boolean(string="Air Ride Preferred")
    dock_high_required = fields.Boolean(string="Dock High Required")

    @api.constrains("fleet_vehicle_id", "is_requirement_class")
    def _check_unique_fleet_vehicle(self):
        for rec in self:
            if rec.fleet_vehicle_id and not rec.is_requirement_class:
                dup = self.search([
                    ("fleet_vehicle_id", "=", rec.fleet_vehicle_id.id),
                    ("is_requirement_class", "=", False),
                    ("id", "!=", rec.id),
                ], limit=1)
                if dup:
                    raise ValidationError(_(
                        "This fleet vehicle '%s' is already linked to Equipment Profile '%s'."
                    ) % (rec.fleet_vehicle_id.name, dup.name))

    # ── Computed fields source: fleet.vehicle ────────────────────────
    @api.depends("fleet_vehicle_id",
                 "fleet_vehicle_id.x_cargo_box_length_ft",
                 "fleet_vehicle_id.straight_pallet_capacity",
                 "fleet_vehicle_id.pin_wheel_pallet_capacity",
                 "fleet_vehicle_id.turned_pallet_capacity",
                 "fleet_vehicle_id.x_max_payload_lbs",
                 "fleet_vehicle_id.x_gvwr_lbs",
                 "fleet_vehicle_id.x_reefer",
                 "fleet_vehicle_id.x_liftgate",
                 "fleet_vehicle_id.x_air_ride",
                 "fleet_vehicle_id.x_dock_height_in",
                 "fleet_vehicle_id.x_geotab_device_id",
                 "fleet_vehicle_id.x_avg_km_per_l_last_week",
                 "fleet_vehicle_id.x_operational_logistics",
                 "fleet_vehicle_id.default_pallet_layout",
                 "fleet_vehicle_id.layout_configuration_verified",
    )
    def _compute_from_vehicle(self):
        for rec in self:
            v = rec.fleet_vehicle_id
            if v and not rec.is_requirement_class:
                rec.body_length_ft = v.x_cargo_box_length_ft or 26.0
                rec.straight_pallet_capacity = int(v.straight_pallet_capacity or 12)
                rec.pin_wheel_pallet_capacity = int(v.pin_wheel_pallet_capacity or 13)
                rec.turned_pallet_capacity = int(v.turned_pallet_capacity or 14)
                # Auto-booking: straight up to 12, pin-wheel available for 13
                rec.max_pallets = rec.pin_wheel_pallet_capacity
                rec.max_payload_lbs = v.x_max_payload_lbs or 11000.0
                rec.gvwr_lbs = v.x_gvwr_lbs or 0.0
                rec.reefer = bool(v.x_reefer)
                rec.liftgate_capable = bool(v.x_liftgate)
                rec.air_ride = bool(v.x_air_ride)
                rec.dock_high = (v.x_dock_height_in or 0) > 0
                rec.geotab_active = bool(v.x_geotab_device_id)
                rec.fuel_efficiency_km_l = v.x_avg_km_per_l_last_week or 0.0
                rec.operational = bool(v.x_operational_logistics)
                rec.default_pallet_layout = v.default_pallet_layout or "straight"
                rec.layout_verified = bool(v.layout_configuration_verified)
            elif not v and not rec.is_requirement_class:
                # No fleet vehicle linked yet — keep current values
                pass

    @api.depends("fleet_vehicle_id", "is_requirement_class")
    def _compute_name(self):
        for rec in self:
            if rec.fleet_vehicle_id and not rec.is_requirement_class:
                v = rec.fleet_vehicle_id
                length = int(v.x_cargo_box_length_ft or 0)
                reefer_str = " Reefer" if v.x_reefer else ""
                rec.name = f"{v.license_plate or v.name} — {length} ft{reefer_str}"
            elif rec.is_requirement_class and rec.name:
                pass  # keep manually set name
            elif not rec.name:
                rec.name = "New Equipment Profile"

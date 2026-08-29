from odoo import fields, models
from odoo.exceptions import UserError


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    default_pallet_layout = fields.Selection([
        ("straight", "Straight"),
        ("pin_wheel", "Pin-Wheel"),
        ("turned", "Turned"),
    ], default="straight")

    # ── Driver start profile (unified ETA engine, Section C) ─────────
    # Where the truck starts its operational day. "driver_home" uses the
    # driver's private home coordinates (res.partner.home_latitude/longitude,
    # staff-only) and computes recommended_driver_leave_home_at =
    # route start − pretrip − home→hub.
    driver_start_mode = fields.Selection([
        ("depot", "Depot / Truck Yard"),
        ("hub", "Hub"),
        ("driver_home", "Driver Home"),
        ("prior_day_end", "Prior-Day End"),
    ], string="Driver Start Mode", default="depot",
        help="Where the truck starts its operational day. Prior-Day End = "
             "last completed stop of the previous run (cross-day "
             "positioning).")
    driver_pretrip_minutes = fields.Integer(
        string="Pre-Trip Inspection (min)", default=10,
        help="Inspection time at the origin before the first leg — used to "
             "back-compute the driver's leave-home time.")
    driver_home_to_hub_minutes = fields.Integer(
        string="Home → Hub Drive (min)", default=30,
        help="Estimated drive from the driver's home to the hub/yard — "
             "only relevant when Driver Start Mode = Driver Home.")
    straight_pallet_capacity = fields.Integer(default=12)
    pin_wheel_pallet_capacity = fields.Integer(default=13)
    turned_pallet_capacity = fields.Integer(default=14)
    layout_configuration_verified = fields.Boolean(default=False)
    layout_verified_by = fields.Many2one("res.users")
    layout_verified_at = fields.Datetime()
    layout_verification_notes = fields.Text()

    def get_layout_capacity(self, layout_type=None):
        self.ensure_one()
        layout = layout_type or self.default_pallet_layout or "straight"
        return {
            "straight": self.straight_pallet_capacity or 0,
            "pin_wheel": self.pin_wheel_pallet_capacity or 0,
            "turned": self.turned_pallet_capacity or 0,
        }.get(layout, self.straight_pallet_capacity or 0)

    def get_recommended_pallet_layout(self, pallet_count):
        self.ensure_one()
        pallet_count = int(pallet_count or 0)
        if pallet_count <= (self.straight_pallet_capacity or 0):
            return "straight"
        if pallet_count <= (self.pin_wheel_pallet_capacity or 0):
            return "pin_wheel"
        if pallet_count <= (self.turned_pallet_capacity or 0):
            return "turned"
        return False

    def action_verify_pallet_layout_configuration(self):
        self.ensure_one()
        if not (
            self.env.user.has_group("prema_dispatch.group_dispatch_manager")
            or self.env.user.has_group("base.group_system")
        ):
            raise UserError("Only a Dispatch Manager can verify pallet layout configuration.")
        if self.straight_pallet_capacity <= 0:
            raise UserError("Straight capacity must be greater than zero.")
        if self.pin_wheel_pallet_capacity < self.straight_pallet_capacity:
            raise UserError("Pin-Wheel capacity must be greater than or equal to Straight capacity.")
        if self.turned_pallet_capacity < self.pin_wheel_pallet_capacity:
            raise UserError("Turned capacity must be greater than or equal to Pin-Wheel capacity.")

        # The gate above is the whole access check: only a Dispatch
        # Manager (or admin) reaches this point. The writes themselves
        # target fleet.vehicle, which fleet's own security rules restrict
        # to Fleet/Officer — sudo here so a dispatch manager who is not a
        # fleet officer can still verify the layout configuration.
        self = self.sudo()
        templates = self.env["prema.dispatch.vehicle.layout.template"].search([
            "|", ("applicable_vehicle_ids", "in", [self.id]), ("applicable_vehicle_ids", "=", False),
            ("active", "=", True),
        ])
        required = {
            "straight": self.straight_pallet_capacity,
            "pin_wheel": self.pin_wheel_pallet_capacity,
            "turned": self.turned_pallet_capacity,
        }
        for layout_type, capacity in required.items():
            tpl = templates.filtered(lambda template: template.layout_type == layout_type and template.max_positions == capacity)[:1]
            if not tpl:
                raise UserError(
                    f"No active {layout_type.replace('_', ' ')} layout template with {capacity} positions is available for {self.name}."
                )
            tpl.write({
                "is_verified": True,
                "verified_by": self.env.user.id,
                "verified_at": fields.Datetime.now(),
            })
            if self.id not in tpl.applicable_vehicle_ids.ids:
                tpl.write({"applicable_vehicle_ids": [(4, self.id)]})

        self.write({
            "layout_configuration_verified": True,
            "layout_verified_by": self.env.user.id,
            "layout_verified_at": fields.Datetime.now(),
            "x_max_pallets": max(
                self.straight_pallet_capacity or 0,
                self.pin_wheel_pallet_capacity or 0,
                self.turned_pallet_capacity or 0,
            ),
        })
        self.message_post(body=(
            f"Pallet layout configuration verified by {self.env.user.name}: "
            f"Straight {self.straight_pallet_capacity}, Pin-Wheel {self.pin_wheel_pallet_capacity}, "
            f"Turned {self.turned_pallet_capacity}."
        ))
        return True

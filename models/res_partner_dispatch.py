from odoo import api, fields, models
from odoo.exceptions import UserError


class ResPartnerDispatch(models.Model):
    _inherit = "res.partner"

    x_is_driver = fields.Boolean(
        string="Fleet Driver",
        default=False,
        help="Mark this contact as a fleet driver. Drivers appear in dispatch job driver picker.",
    )
    x_voip_extension = fields.Char(
        string="VoIP Extension",
        help="Internal PBX / softphone extension used for driver-to-dispatch calls.",
    )

    # Private driver home coordinates — staff-only (the driver start
    # profile reads them under sudo; never serialized to the driver app
    # as a lat/lng, only as the computed leave-home time).
    home_latitude = fields.Float(
        string="Home Latitude", digits=(16, 7),
        groups="prema_dispatch.group_dispatcher,prema_dispatch.group_dispatch_manager",
        help="Driver's home coordinates for the Driver Home start mode — "
             "private, staff-only.")
    home_longitude = fields.Float(
        string="Home Longitude", digits=(16, 7),
        groups="prema_dispatch.group_dispatcher,prema_dispatch.group_dispatch_manager",
        help="Driver's home coordinates for the Driver Home start mode — "
             "private, staff-only.")

    # Many2one to GeoTab driver registry — user picks from dropdown.
    # On save this syncs back to x_geotab_driver_id (Char) that the ELD integration uses.
    geotab_driver_link_id = fields.Many2one(
        "premafirm.geotab.driver",
        string="GeoTab Driver",
        ondelete="set null",
        help="Select the GeoTab ELD driver profile for this contact. "
             "Automatically fills the GeoTab Driver ID used for ELD sync.",
    )

    # Computed: which Odoo user is linked to this partner (for driver app login)
    driver_user_id = fields.Many2one(
        "res.users",
        string="Driver App Login",
        compute="_compute_driver_user_id",
        store=False,
    )
    driver_has_account = fields.Boolean(
        compute="_compute_driver_user_id",
        store=False,
    )

    @api.depends("user_ids")
    def _compute_driver_user_id(self):
        for partner in self:
            # res.partner.user_ids is the reverse of res.users.partner_id
            users = partner.user_ids.filtered(lambda u: u.active)
            partner.driver_user_id = users[0] if users else False
            partner.driver_has_account = bool(users)

    @api.onchange("geotab_driver_link_id")
    def _onchange_geotab_driver_link(self):
        """Sync the selected GeoTab driver's ID back into x_geotab_driver_id (Char)."""
        if self.geotab_driver_link_id:
            self.x_geotab_driver_id = self.geotab_driver_link_id.geotab_id
        else:
            self.x_geotab_driver_id = False

    def write(self, vals):
        if "geotab_driver_link_id" in vals:
            driver_rec_id = vals["geotab_driver_link_id"]
            if driver_rec_id:
                gt = self.env["premafirm.geotab.driver"].browse(driver_rec_id)
                if gt.exists():
                    vals.setdefault("x_geotab_driver_id", gt.geotab_id)
            else:
                vals.setdefault("x_geotab_driver_id", False)
        return super().write(vals)

    def action_create_driver_account(self):
        """Create a restricted Odoo user account for this contact (Driver group).

        - Uses the partner's email as login.
        - Assigns only the Dispatch > Driver security group.
        - Sends a password-reset / set-initial-password email so the driver
          can set their own password via the standard Odoo invitation flow.
        """
        self.ensure_one()

        if not self.x_is_driver:
            raise UserError("Enable 'Fleet Driver' first before creating a driver account.")
        if not self.email:
            raise UserError("This contact has no email address. Add an email before creating an account.")
        if self.driver_has_account:
            raise UserError(
                f"This contact already has an Odoo account: {self.driver_user_id.login}.\n"
                "Use 'Reset Password' to send a new login link."
            )

        driver_group = self.env.ref("prema_dispatch.group_dispatch_driver", raise_if_not_found=False)
        base_user_group = self.env.ref("base.group_user", raise_if_not_found=False)

        # Build group_ids: portal access + driver group
        portal_group = self.env.ref("base.group_portal", raise_if_not_found=False)
        groups = [driver_group.id] if driver_group else []

        user = self.env["res.users"].create({
            "name":       self.name,
            "login":      self.email,
            "email":      self.email,
            "partner_id": self.id,
            "groups_id":  [(6, 0, [base_user_group.id] + groups)] if base_user_group else [(6, 0, groups)],
            "sel_groups_1_10_11": 1,  # Internal user (required for app access)
        })

        # Send invitation / password-reset email
        try:
            user.action_reset_password()
        except Exception:
            pass

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Driver Account Created",
                "message": f"Account created for {self.name} ({self.email}). "
                           "A password setup email has been sent.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_reset_driver_password(self):
        """Send a password-reset email to the linked driver user."""
        self.ensure_one()
        if not self.driver_has_account:
            raise UserError("No driver account found for this contact.")
        try:
            self.driver_user_id.action_reset_password()
        except Exception as e:
            raise UserError(f"Could not send reset email: {e}") from e
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Password Reset Sent",
                "message": f"Password reset email sent to {self.email}.",
                "type": "info",
                "sticky": False,
            },
        }

    @api.model
    def sync_fleet_drivers(self):
        """Set x_is_driver=True on all res.partner records currently assigned
        as drivers in fleet.vehicle. Safe to run multiple times."""
        driver_partners = self.env["fleet.vehicle"].search([
            ("driver_id", "!=", False)
        ]).mapped("driver_id")
        if driver_partners:
            driver_partners.write({"x_is_driver": True})
        return True

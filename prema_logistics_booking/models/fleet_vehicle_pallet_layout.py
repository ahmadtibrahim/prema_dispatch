"""Per-vehicle pallet layout modes — the single data source for pallet
capacity and layout selection.

A vehicle may define any number of active layouts (STANDARD, PINWHEEL,
TURNED, future modes). Capacity and layout selection are ALWAYS derived
from these rows (falling back to the legacy straight/pin-wheel/turned
fields when a vehicle has no rows yet).
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    pallet_layout_ids = fields.One2many(
        "fleet.vehicle.pallet.layout", "vehicle_id", string="Pallet Layouts",
    )


class FleetVehiclePalletLayout(models.Model):
    _name = "fleet.vehicle.pallet.layout"
    _description = "Vehicle Pallet Layout Mode"
    _order = "vehicle_id, sequence, max_pallets, id"

    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Vehicle", required=True,
        ondelete="cascade", index=True,
    )
    name = fields.Char(required=True)
    code = fields.Char(required=True)
    layout_type = fields.Selection([
        ("standard", "Standard"),
        ("pinwheel", "Pinwheel"),
        ("turned", "Turned"),
        ("other", "Other"),
    ], string="Layout Type", required=True, default="standard")
    max_pallets = fields.Integer(string="Max Pallets", required=True, default=0)
    sequence = fields.Integer(default=10)
    is_default = fields.Boolean(string="Default Layout")
    active = fields.Boolean(default=True)

    @api.constrains("max_pallets")
    def _check_max_pallets_positive(self):
        for layout in self:
            if layout.max_pallets <= 0:
                raise ValidationError(_("Max Pallets must be greater than zero."))

    @api.constrains("vehicle_id", "is_default", "active")
    def _check_single_default(self):
        for layout in self:
            if not layout.is_default or not layout.active:
                continue
            other_default = self.search([
                ("vehicle_id", "=", layout.vehicle_id.id),
                ("is_default", "=", True),
                ("active", "=", True),
                ("id", "!=", layout.id),
            ], limit=1)
            if other_default:
                raise ValidationError(_(
                    "Vehicle %(vehicle)s already has an active default layout "
                    "(%(layout)s).",
                    vehicle=layout.vehicle_id.display_name,
                    layout=other_default.display_name,
                ))

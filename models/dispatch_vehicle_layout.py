from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PremaDispatchVehicleLayoutTemplate(models.Model):
    _name = "prema.dispatch.vehicle.layout.template"
    _description = "Vehicle Load Layout Template"
    _order = "name"

    name = fields.Char(required=True)
    applicable_vehicle_ids = fields.Many2many("fleet.vehicle", string="Applicable Vehicles")
    layout_type = fields.Selection([
        ("straight", "Straight"),
        ("pin_wheel", "Pin-Wheel"),
        ("turned", "Turned"),
        ("distributed", "Distributed"),
    ], required=True, default="straight")
    max_positions = fields.Integer(compute="_compute_max_positions", store=True)
    usable_length_in = fields.Float()
    usable_width_in = fields.Float()
    interior_height_in = fields.Float()
    door_opening_width_in = fields.Float()
    liftgate_clearance_in = fields.Float()
    reefer_bulkhead_clearance_in = fields.Float()
    wheel_well_notes = fields.Text()
    standard_pallet_length_in = fields.Float(default=48.0)
    standard_pallet_width_in = fields.Float(default=40.0)
    max_payload_lbs = fields.Float()
    is_verified = fields.Boolean(default=False, help="Real measurements confirmed. Unverified templates must not be presented as guaranteed capacity.")
    verified_by = fields.Many2one("res.users")
    verified_at = fields.Datetime()
    revision = fields.Integer(default=1)
    active = fields.Boolean(default=True)
    position_ids = fields.One2many("prema.dispatch.vehicle.layout.position", "layout_template_id")

    @api.depends("position_ids", "position_ids.active")
    def _compute_max_positions(self):
        for tpl in self:
            tpl.max_positions = len(tpl.position_ids.filtered("active"))


class PremaDispatchVehicleLayoutPosition(models.Model):
    _name = "prema.dispatch.vehicle.layout.position"
    _description = "Vehicle Load Layout Position"
    _order = "sequence, position_code"

    layout_template_id = fields.Many2one("prema.dispatch.vehicle.layout.template", required=True, ondelete="cascade", index=True)
    position_code = fields.Char(required=True)
    display_name = fields.Char()
    side = fields.Selection([("driver", "Driver Side"), ("passenger", "Passenger Side"), ("center", "Center")], default="center")
    sequence = fields.Integer(default=10)
    distance_from_rear_in = fields.Float()
    x_coordinate = fields.Float()
    y_coordinate = fields.Float()
    display_width = fields.Float(default=40.0)
    display_height = fields.Float(default=48.0)
    orientation = fields.Selection([
        ("lengthwise", "Lengthwise"), ("crosswise", "Crosswise"), ("angled", "Angled"),
    ], default="lengthwise")
    max_weight_lbs = fields.Float()
    max_length_in = fields.Float()
    max_width_in = fields.Float()
    four_way_required = fields.Boolean(default=False)
    blocked = fields.Boolean(default=False)
    blocked_reason = fields.Char()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("position_code_unique_per_template", "unique(layout_template_id, position_code)",
         "Position code must be unique within a layout template."),
    ]

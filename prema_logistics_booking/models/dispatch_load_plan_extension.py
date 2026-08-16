"""Load Plan bridge — expose the canonical selected pallet layout to the
Load Planning UI and Driver App, from the same VehicleCapacityService
used by booking, dispatch and the portal."""
from odoo import api, fields, models


class DispatchLoadPlan(models.Model):
    _inherit = "prema.dispatch.load.plan"

    capacity_layout_code = fields.Char(compute="_compute_capacity_layout")
    capacity_layout_name = fields.Char(compute="_compute_capacity_layout")

    @api.depends("vehicle_id", "confirmed_pallet_count")
    def _compute_capacity_layout(self):
        from ..services.vehicle_capacity_service import VehicleCapacityService
        service = VehicleCapacityService(self.env)
        for plan in self:
            required = plan.confirmed_pallet_count or 0
            valid, layout = service.select_layout(plan.vehicle_id, required)
            plan.capacity_layout_code = layout["code"] if valid else ""
            plan.capacity_layout_name = layout["name"] if valid else ""

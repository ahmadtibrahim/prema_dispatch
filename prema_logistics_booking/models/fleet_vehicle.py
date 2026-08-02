from odoo import fields, models


class FleetVehicle(models.Model):
    """Additive extension only — no change to prema_dispatch's fleet_vehicle.py."""

    _inherit = "fleet.vehicle"

    equipment_profile_id = fields.Many2one(
        "logistics.equipment.profile",
        string="Logistics Equipment Profile",
        help="Links this real vehicle to an abstract capacity profile used by "
             "the logistics pricing/availability engine.",
    )

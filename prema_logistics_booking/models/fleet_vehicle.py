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

    # Revenue analytic for the freight invoice line when this truck moves
    # the booking (spec Q "analytic truck if available"). Configured per
    # vehicle — e.g. analytic "Truck #1 – Freightliner M2" on the real
    # Freightliner — and read at draft-invoice creation so the product
    # line carries {"<analytic_id>": 100.0}; empty = line has no analytic.
    analytic_account_id = fields.Many2one(
        "account.analytic.account",
        string="Revenue Analytic (Truck)",
        help="Cost/revenue analytic account charged 100%% on the freight "
             "invoice line when this truck moves a booking. Leave blank to "
             "create invoice lines without an analytic.",
    )

from odoo import fields, models


class PremaDispatchAssignmentLog(models.Model):
    _name = "prema.dispatch.assignment.log"
    _description = "Dispatch Assignment / Reassignment Log"
    _order = "changed_at desc"

    job_id = fields.Many2one(
        "prema.dispatch.job", required=True, ondelete="cascade", index=True
    )
    old_vehicle_id = fields.Many2one("fleet.vehicle", string="Previous Truck", ondelete="set null")
    new_vehicle_id = fields.Many2one("fleet.vehicle", string="New Truck", ondelete="set null")
    old_driver_id = fields.Many2one("res.partner", string="Previous Driver", ondelete="set null")
    new_driver_id = fields.Many2one("res.partner", string="New Driver", ondelete="set null")
    changed_by = fields.Many2one(
        "res.users", string="Changed By",
        default=lambda self: self.env.user, readonly=True,
    )
    changed_at = fields.Datetime(
        string="Changed At", default=fields.Datetime.now, readonly=True
    )
    reason = fields.Text()
    gps_lat_at_assignment = fields.Float(digits=(10, 6))
    gps_lng_at_assignment = fields.Float(digits=(10, 6))

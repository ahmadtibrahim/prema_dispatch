from odoo import fields, models

class PremaDispatchLoadPlanOperation(models.Model):
    _name = "prema.dispatch.load.plan.operation"
    _description = "Load Plan Operation"
    _order = "load_plan_id, operation_sequence, id"
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", required=True, ondelete="cascade", index=True)
    route_visit_id = fields.Many2one("prema.dispatch.route.visit", ondelete="set null")
    operation_sequence = fields.Integer(default=10)
    operation_type = fields.Selection([("reserve_position", "Reserve Position"), ("temporary_unload", "Temporary Unload"), ("load_future_pickup", "Load Future Pickup"), ("reposition", "Reposition"), ("reload", "Reload"), ("completed", "Completed"), ("exception", "Exception")], required=True)
    item_id = fields.Many2one("prema.dispatch.item", ondelete="set null")
    from_position_id = fields.Many2one("prema.dispatch.vehicle.layout.position", ondelete="set null"); to_position_id = fields.Many2one("prema.dispatch.vehicle.layout.position", ondelete="set null")
    related_pickup_stop_id = fields.Many2one("prema.dispatch.stop", ondelete="set null")
    reason = fields.Text(); state = fields.Selection([("pending", "Pending"), ("completed", "Completed"), ("exception", "Exception"), ("cancelled", "Cancelled")], default="pending")
    completed_by = fields.Many2one("res.users"); completed_at = fields.Datetime(); active = fields.Boolean(default=True)

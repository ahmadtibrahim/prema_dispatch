from odoo import fields, models

class PremaDispatchRouteVisit(models.Model):
    _name = "prema.dispatch.route.visit"
    _description = "Physical Route Visit"
    _order = "operating_date, sequence, id"
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="cascade", index=True)
    operating_date = fields.Date(index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", index=True); driver_id = fields.Many2one("res.partner", index=True)
    sequence = fields.Integer(default=10); visit_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery"), ("mixed", "Mixed"), ("other", "Other")], default="delivery")
    mixed_action_order = fields.Selection([
        ("unload_then_load", "Unload First, Then Load"),
        ("load_then_unload", "Load First, Then Unload"),
    ], string="Mixed Action Order", default="unload_then_load",
        help="Default action order for mixed visits (logical pickup + "
             "delivery at the same physical facility): UNLOAD first, "
             "then LOAD — unless constraints require otherwise.")
    saved_location_id = fields.Many2one("prema.dispatch.location", index=True); address = fields.Char(); effective_lat = fields.Float(digits=(10,6)); effective_lng = fields.Float(digits=(10,6))
    planned_arrival = fields.Datetime(); service_window = fields.Char(); status = fields.Selection([("pending", "Pending"), ("arrived", "Arrived"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="pending")
    active = fields.Boolean(default=True); stop_link_ids = fields.One2many("prema.dispatch.route.visit.stop", "route_visit_id")

class PremaDispatchRouteVisitStop(models.Model):
    _name = "prema.dispatch.route.visit.stop"
    _description = "Physical Route Visit Stop Link"
    route_visit_id = fields.Many2one("prema.dispatch.route.visit", required=True, ondelete="cascade")
    stop_id = fields.Many2one("prema.dispatch.stop", required=True, ondelete="cascade")
    job_id = fields.Many2one("prema.dispatch.job", related="stop_id.job_id", store=True, index=True)
    completion_state = fields.Selection([("pending", "Pending"), ("completed", "Completed"), ("exception", "Exception")], default="pending")
    pod_required = fields.Boolean(related="stop_id.pod_required", store=True); active = fields.Boolean(default=True)

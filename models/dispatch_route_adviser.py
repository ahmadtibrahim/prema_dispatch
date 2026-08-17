"""Route Adviser wizard — current vs recommended milk-run route.

Opened from the dispatch job; shows side-by-side metrics, the
recommended per-stop plan (ETA, hours, appointment, service window,
pallet delta, onboard after), and warnings. Apply writes the new stop
sequence; Keep leaves the manual route untouched.
"""
from odoo import fields, models


class PremaDispatchRouteAdviser(models.TransientModel):
    _name = "prema.dispatch.route.adviser"
    _description = "Route Adviser"

    job_id = fields.Many2one(
        "prema.dispatch.job", string="Route Job", required=True,
        ondelete="cascade")

    # Current route metrics
    current_distance_km = fields.Float(digits=(10, 1))
    current_drive_minutes = fields.Float(digits=(10, 0))
    current_waiting_minutes = fields.Float(digits=(10, 0))
    current_finish_eta = fields.Char()
    current_peak = fields.Integer()
    current_feasible = fields.Boolean()

    # Recommended route metrics
    recommended_distance_km = fields.Float(digits=(10, 1))
    recommended_drive_minutes = fields.Float(digits=(10, 0))
    recommended_waiting_minutes = fields.Float(digits=(10, 0))
    recommended_finish_eta = fields.Char()
    recommended_peak = fields.Integer()
    feasible = fields.Boolean(string="Recommended Feasible")

    warnings_text = fields.Text(string="Warnings")

    line_ids = fields.One2many(
        "prema.dispatch.route.adviser.line", "adviser_id",
        string="Recommended Stops")

    def apply_recommended(self):
        from odoo.addons.prema_dispatch.services.route_adviser_service import (
            RouteAdviserService,
        )
        result = RouteAdviserService(self.env).apply_recommended_route(self.job_id)
        return {
            "type": "ir.actions.act_window_close",
            "context": result,
        }


class PremaDispatchRouteAdviserLine(models.TransientModel):
    _name = "prema.dispatch.route.adviser.line"
    _description = "Route Adviser Recommended Stop"
    _order = "sequence, id"

    adviser_id = fields.Many2one(
        "prema.dispatch.route.adviser", required=True, ondelete="cascade")
    sequence = fields.Integer()
    stop_name = fields.Char(string="Stop")
    stop_type = fields.Char(string="Type")
    eta = fields.Char(string="ETA")
    facility_hours = fields.Char(string="Facility Hours")
    appointment = fields.Char(string="Appointment / Window")
    service_start = fields.Char(string="Service Start")
    service_end = fields.Char(string="Departure")
    pallet_delta = fields.Char(string="Pallet Δ")
    onboard_after = fields.Integer(string="Onboard After")
    reason = fields.Char(string="Reason")

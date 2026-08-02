"""Daily Local Operation — many local jobs on one truck, feeding a corridor.

Monday Local → feeds Tuesday Quebec corridor.
Thursday Local → feeds Friday Ottawa corridor.

Local operations are NOT corridors — they aggregate many small local jobs
(pickups and deliveries within GTA) into a single daily operation.
"""
from odoo import api, fields, models


class LogisticsDailyLocalOperation(models.Model):
    _name = "logistics.daily.local.operation"
    _description = "Daily Local Operation (GTA pickup/delivery day)"
    _order = "date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(compute="_compute_name", store=True)
    date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    driver_id = fields.Many2one("res.partner", string="Driver",
                                 domain=[("x_is_driver_profile", "=", True)])

    # Links to the corridor this local ops day feeds
    corridor_id = fields.Many2one(
        "logistics.corridor", string="Local Route",
        domain=[("direction", "=", "local")],
        help="The local operations corridor template."
    )
    feeds_corridor_id = fields.Many2one(
        "logistics.corridor", string="Feeds Corridor",
        help="The long-haul corridor that freight collected today feeds into "
             "(e.g., Tuesday Quebec corridor)."
    )

    # Revenue
    revenue_target = fields.Float(string="Revenue Target")
    booked_revenue = fields.Float(compute="_compute_booked", store=True)

    # Jobs
    job_ids = fields.One2many("prema.dispatch.job", "local_operation_id", string="Jobs")
    job_count = fields.Integer(compute="_compute_booked", store=True)

    # Stops & Pallets
    total_stops = fields.Integer(compute="_compute_booked", store=True)
    total_pallets = fields.Integer(compute="_compute_booked", store=True)

    # Status
    state = fields.Selection([
        ("planned", "Planned"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
    ], default="planned", tracking=True)

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.depends("date", "corridor_id")
    def _compute_name(self):
        for rec in self:
            day_name = rec.date.strftime("%A") if rec.date else ""
            corridor = rec.corridor_id.name or "Local"
            rec.name = f"{day_name} {corridor} — {rec.date}"

    @api.depends("job_ids", "job_ids.approximate_skids", "job_ids.total_skids")
    def _compute_booked(self):
        for rec in self:
            jobs = rec.job_ids.filtered(lambda j: j.active)
            rec.job_count = len(jobs)
            rec.total_stops = sum(len(j.stop_ids) for j in jobs)
            rec.total_pallets = sum(j.total_skids or j.approximate_skids or 0 for j in jobs)
            # Revenue from linked bookings — sudo() because viewer/manager groups
            # may not have direct ACL on logistics.booking
            bookings = self.env["logistics.booking"].sudo().search([
                ("dispatch_job_id", "in", jobs.ids),
                ("state", "=", "confirmed"),
            ])
            rec.booked_revenue = sum(b.calculated_price for b in bookings)

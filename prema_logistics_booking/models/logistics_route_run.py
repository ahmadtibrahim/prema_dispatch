"""Route Run — DEPRECATED. Use logistics.corridor.departure instead.

This model is kept for backwards compatibility only. Its operational fields
(vehicle, driver, booked_pallets, revenue, net_profit, state) all exist on
logistics.corridor.departure with the same or better fidelity.

Existing route_run_id foreign keys (on logistics.booking and
logistics.recurring.agreement) should be migrated to departure_id pointing
to logistics.corridor.departure.
"""
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

WEEKDAY_SELECTION = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
    ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
]

ROUTING_STRATEGY = [
    ("direct", "Direct"),
    ("en_route", "En-Route"),
    ("hub_transfer", "Hub Transfer"),
    ("scheduled_connection", "Scheduled Connection"),
    ("multi_leg", "Multi-Leg"),
    ("custom_quote", "Custom Quote"),
]


class LogisticsRouteRun(models.Model):
    """[DEPRECATED] Operational instance of a recurring schedule.

    Use logistics.corridor.departure instead. This model is kept for backwards
    compatibility. New code should create corridor.departure records directly.
    """
    _name = "logistics.route.run"
    _description = "Route Run [DEPRECATED — use logistics.corridor.departure]"
    _order = "run_date desc, name"
    _inherit = ["mail.thread"]

    @api.model
    def create(self, vals):
        _logger.warning(
            "logistics.route.run is DEPRECATED — use logistics.corridor.departure instead. "
            "Creating run for date=%s", vals.get('run_date', 'unknown')
        )
        return super().create(vals)

    def write(self, vals):
        _logger.warning(
            "logistics.route.run is DEPRECATED — use logistics.corridor.departure instead. "
            "Updating run ids=%s", self.ids
        )
        return super().write(vals)

    name = fields.Char(compute="_compute_name", store=True)
    active = fields.Boolean(default=True)

    # Scheduling
    run_date = fields.Date(required=True, index=True, string="Run Date")
    recurring_day = fields.Selection(WEEKDAY_SELECTION, string="Recurring Day")
    corridor_name = fields.Char(string="Corridor", help="e.g. Eastbound Quebec, Southwest Feeder")
    routing_strategy = fields.Selection(ROUTING_STRATEGY, default="direct")

    # Regions
    origin_region_id = fields.Many2one("logistics.region", string="Origin Region", index=True)
    destination_region_id = fields.Many2one("logistics.region", string="Destination Region")
    via_hub_id = fields.Many2one("logistics.region", string="Via Hub")

    # Truck & Driver
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck", index=True)
    driver_id = fields.Many2one("res.partner", string="Driver", domain=[("x_is_driver_profile", "=", True)])
    equipment_profile_id = fields.Many2one("logistics.equipment.profile")

    # Capacity
    max_pallets = fields.Integer(default=12)
    max_weight_lbs = fields.Float(default=11000.0)
    booked_pallets = fields.Integer(compute="_compute_capacity", store=True)
    booked_weight_lbs = fields.Float(compute="_compute_capacity", store=True)
    available_pallets = fields.Integer(compute="_compute_capacity", store=True)
    available_weight_lbs = fields.Float(compute="_compute_capacity", store=True)

    # Temperature
    temperature_mode = fields.Selection([("dry","Dry"),("chilled","Chilled"),("frozen","Frozen"),("all","All — Reefer Capable")], default="dry")

    # Revenue targets
    revenue_target = fields.Float(string="Revenue Target", help="Minimum target revenue for this run.")
    booked_revenue = fields.Float(compute="_compute_revenue", store=True)
    remaining_target = fields.Float(compute="_compute_revenue", store=True)

    # Cost / Profit
    route_cost = fields.Float(string="Estimated Route Cost")
    net_profit = fields.Float(compute="_compute_profit", store=True)

    # Status
    state = fields.Selection([
        ("scheduled", "Scheduled"),
        ("confirmed", "Confirmed"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ], default="scheduled", tracking=True)

    # Bookings
    booking_ids = fields.One2many("logistics.booking", "route_run_id", string="Bookings")
    booking_count = fields.Integer(compute="_compute_booking_count", store=True)

    # Route stops (for sequencing)
    stop_city_ids = fields.Text(string="Stop Cities", help="Ordered list of cities/stops on this route.")

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    _sql_constraints = [
        ("vehicle_date_uniq", "unique(vehicle_id, run_date)",
         "This truck is already assigned to a route run on this date."),
    ]

    @api.depends("run_date", "corridor_name", "origin_region_id", "destination_region_id")
    def _compute_name(self):
        for rec in self:
            parts = [rec.corridor_name or "Route Run", str(rec.run_date or "")]
            rec.name = " — ".join(parts)

    @api.depends("booking_ids", "booking_ids.pallets", "booking_ids.weight_lbs", "booking_ids.state")
    def _compute_capacity(self):
        for rec in self:
            active_bookings = rec.booking_ids.filtered(lambda b: b.state == "confirmed")
            rec.booked_pallets = sum(b.pallets for b in active_bookings)
            rec.booked_weight_lbs = sum(b.weight_lbs for b in active_bookings)
            rec.available_pallets = max(0, rec.max_pallets - rec.booked_pallets)
            rec.available_weight_lbs = max(0, rec.max_weight_lbs - rec.booked_weight_lbs)

    @api.depends("booking_ids", "booking_ids.calculated_price", "booking_ids.state")
    def _compute_revenue(self):
        for rec in self:
            active_bookings = rec.booking_ids.filtered(lambda b: b.state == "confirmed")
            rec.booked_revenue = sum(b.calculated_price for b in active_bookings)
            rec.remaining_target = max(0, (rec.revenue_target or 0) - rec.booked_revenue)

    @api.depends("booked_revenue", "route_cost")
    def _compute_profit(self):
        for rec in self:
            rec.net_profit = rec.booked_revenue - (rec.route_cost or 0.0)

    @api.depends("booking_ids")
    def _compute_booking_count(self):
        for rec in self:
            rec.booking_count = len(rec.booking_ids)

    def action_estimate_cost(self):
        """Call Prema AI Estimator for this route run."""
        self.ensure_one()
        try:
            from odoo.addons.premafirm_ai_engine.services.pricing_engine import PricingEngine
            if not self.vehicle_id:
                return {"type": "ir.actions.client", "tag": "display_notification",
                        "params": {"title": "No Truck", "message": "Assign a truck first.", "type": "warning"}}
            distance = 500.0
            if self.origin_region_id and self.destination_region_id:
                lane = self.env["logistics.lane"].search([
                    ("origin_region_id", "=", self.origin_region_id.id),
                    ("destination_region_id", "=", self.destination_region_id.id),
                ], limit=1)
                if lane and lane.road_km:
                    distance = lane.road_km
            duration = distance / 80.0
            engine = PricingEngine(self.env)
            costs = engine.calculate(self.vehicle_id.id, distance, duration,
                                     load_weight_lbs=self.booked_weight_lbs or 8000.0)
            self.route_cost = costs["total_cost"]
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Cost Estimated",
                               "message": f"Route cost: ${costs['total_cost']:.2f} | NET: ${self.net_profit:.2f}",
                               "type": "success"}}
        except Exception as e:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Error", "message": str(e), "type": "danger"}}

    def action_open_dispatch(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Board",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("id", "in", self.booking_ids.mapped("dispatch_job_id").ids)],
            "target": "current",
        }

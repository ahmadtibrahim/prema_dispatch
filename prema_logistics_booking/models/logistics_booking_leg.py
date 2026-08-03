"""Multi-Leg Booking — one booking may span multiple operational legs.

Example: St Catharines → Transit Mississauga → Montreal
    Leg 1: St Catharines → Mississauga (pickup Mon)
    Leg 2: Mississauga → Montreal (delivery Tue)

ONE booking, ONE invoice, multiple operational legs.
Each leg rides on a specific corridor departure.
"""
from odoo import api, fields, models


class LogisticsBookingLeg(models.Model):
    _name = "logistics.booking.leg"
    _description = "Booking Leg (operational segment of a multi-leg shipment)"
    _order = "booking_id, sequence"

    booking_id = fields.Many2one("logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, default=10)

    # Stops
    origin_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Origin Stop", required=True,
        help="Where freight is picked up for this leg."
    )
    destination_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Destination Stop", required=True,
        help="Where freight is delivered for this leg."
    )

    # Corridor departure this leg rides on
    departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Corridor Departure",
        help="The scheduled corridor departure serving this leg."
    )

    # Dates
    pickup_date = fields.Date(string="Pickup Date")
    delivery_date = fields.Date(string="Delivery Date")

    # Freight on this leg
    pallets = fields.Integer(default=1)
    weight_lbs = fields.Float(default=0.0)

    # Status
    status = fields.Selection([
        ("scheduled", "Scheduled"),
        ("in_transit", "In Transit"),
        ("completed", "Completed"),
    ], default="scheduled")

    # ── Capacity Reservation State ──────────────────────────────────────
    reservation_state = fields.Selection([
        ("pending", "Pending"),
        ("reserved", "Reserved"),
        ("released", "Released"),
        ("consumed", "Consumed"),
        ("cancelled", "Cancelled"),
    ], default="pending", string="Reservation State",
       help="Capacity reservation lifecycle for corridor departure segments.")

    # ── Leg Type ────────────────────────────────────────────────────────
    leg_type = fields.Selection([
        ("direct", "Direct"),
        ("feeder", "Feeder"),
        ("linehaul", "Linehaul"),
        ("transfer", "Transfer"),
        ("final_delivery", "Final Delivery"),
    ], string="Leg Type", help="Operational classification of this leg.")

    # ── Region references ───────────────────────────────────────────────
    origin_region_id = fields.Many2one("logistics.region", string="Origin Region")
    destination_region_id = fields.Many2one("logistics.region", string="Destination Region")

    # ── Frozen pricing from route snapshot ──────────────────────────────
    lane_id = fields.Many2one("logistics.lane", string="Ordered Lane")
    offering_id = fields.Many2one("logistics.service.offering", string="Service Offering")
    rate_plan_id = fields.Many2one("logistics.rate.plan", string="Rate Plan")
    rate_plan_name = fields.Char(string="Rate Plan Name", readonly=True)
    rate_plan_version = fields.Integer(string="Rate Plan Version", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency")
    frozen_leg_price = fields.Float(string="Frozen Leg Price", readonly=True)
    frozen_price_breakdown = fields.Json(string="Frozen Price Breakdown", readonly=True)

    # ── Hub transfer ────────────────────────────────────────────────────
    transfer_hub_id = fields.Many2one("logistics.hub", string="Transfer Hub")

    # ── Customer visibility ─────────────────────────────────────────────
    customer_visible = fields.Boolean(default=True, string="Customer Visible",
                                      help="Whether this leg is shown to the customer.")

    # Display
    name = fields.Char(compute="_compute_name", store=True)

    @api.depends("origin_stop_id", "destination_stop_id", "sequence")
    def _compute_name(self):
        for rec in self:
            origin = rec.origin_stop_id.city or rec.origin_stop_id.company_name or f"Stop {rec.origin_stop_id.sequence}"
            dest = rec.destination_stop_id.city or rec.destination_stop_id.company_name or f"Stop {rec.destination_stop_id.sequence}"
            rec.name = f"Leg {rec.sequence}: {origin} → {dest}"

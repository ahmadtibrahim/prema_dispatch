"""Transient model for per-delivery-stop data within a pricing session.

One session can have 1-20 delivery stops. Pickup is stored on the session itself.
"""

from odoo import fields, models


class LogisticsPricingSessionStop(models.TransientModel):
    _name = "logistics.pricing.session.stop"
    _description = "Delivery stop within a pricing session"
    _order = "session_id, sequence"

    session_id = fields.Many2one(
        "logistics.pricing.session", string="Session",
        required=True, ondelete="cascade",
    )
    stop_key = fields.Char(
        string="Stable Stop Key", index=True,
        help="Client-side stable identifier so pallet allocations never "
             "depend on transient array positions.")
    stop_type = fields.Selection(
        [("pickup", "Pickup"), ("delivery", "Delivery")],
        string="Stop Type", default="delivery", required=True)
    sequence = fields.Integer(string="Stop #", required=True, default=1)
    liftgate_required = fields.Boolean()
    dock_available = fields.Boolean()
    appointment_required = fields.Boolean()
    timing_type = fields.Selection([
        ("flexible", "Flexible"),
        ("time_window", "Time Window"),
        ("exact_appointment", "Exact Appointment"),
        ("deadline", "Hard Deadline"),
    ], default="flexible")
    window_start = fields.Float()
    window_end = fields.Float()
    appointment_time = fields.Float()
    hard_deadline = fields.Datetime()
    service_time_minutes = fields.Integer(default=15)
    operating_hours_snapshot = fields.Json()
    timezone = fields.Char(default="America/Toronto")
    instructions = fields.Char()
    saved_location_id = fields.Many2one(
        "logistics.saved.location", string="Saved Location",
        help="Legacy — new stops prefer facility_id/customer_access_id.",
    )
    facility_id = fields.Many2one(
        "prema.dispatch.location", string="Facility",
        help="Canonical physical facility for this stop (one building = "
             "one row). Preferred over saved_location_id for new data.",
    )
    customer_access_id = fields.Many2one(
        "logistics.location.customer.access", string="Customer Access",
        help="This customer's access relation for the facility — their "
             "private contact/instructions for this stop.",
    )
    # Address snapshot (frozen at time of selection)
    location_name = fields.Char()
    street = fields.Char()
    city = fields.Char()
    state_code = fields.Char()
    postal_code = fields.Char()
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))

    # Per-stop freight
    pallets = fields.Integer(string="Pallets", default=1, required=True)
    weight_lbs = fields.Float(string="Weight (lbs)", default=500)
    shared_pallet = fields.Boolean(
        string="Shared Pallet", default=False,
        help="True when this delivery stop shares a physical pallet with other stops.",
    )

    # Accessorials
    liftgate_delivery = fields.Boolean(string="Liftgate Delivery")
    appointment = fields.Boolean(string="Appointment Required")

    # Timing
    timing_type = fields.Selection([
        ("flexible", "Flexible"), ("time_window", "Time Window"),
        ("exact_appointment", "Exact Appointment"),
    ], default="flexible", string="Timing")
    requested_service_date = fields.Date(string="Requested Date")
    window_start = fields.Float(string="Window Start (hours)")
    window_end = fields.Float(string="Window End (hours)")
    appointment_time = fields.Float(string="Appointment Time (hours)")

    # Instructions
    instructions = fields.Text(string="Delivery Instructions")

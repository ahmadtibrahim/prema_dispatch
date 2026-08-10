"""Multi-stop booking stop — one record per pickup or delivery on a logistics.booking."""
from odoo import fields, models


class LogisticsBookingStop(models.Model):
    _name = "logistics.booking.stop"
    _description = "Booking Stop (Pickup / Delivery)"
    _order = "booking_id, sequence"

    booking_id = fields.Many2one("logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, default=10)
    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")], required=True)

    # Saved location (reuse existing dispatch.location)
    saved_location_id = fields.Many2one("prema.dispatch.location", string="Saved Location", index=True, ondelete="set null")
    # New customer Saved Location (commercial layer)
    logistics_saved_location_id = fields.Many2one(
        "logistics.saved.location", string="Customer Saved Location",
        index=True, ondelete="set null",
        help="Customer-facing saved location reference.",
    )

    # Identity
    company_name = fields.Char()
    location_name = fields.Char()
    branch_number = fields.Char()
    unit = fields.Char()

    # Address
    formatted_address = fields.Char()
    street = fields.Char()
    city = fields.Char()
    province_state = fields.Char(string="Province / State")
    postal_zip = fields.Char(string="Postal / ZIP")
    country_id = fields.Many2one("res.country")
    google_place_id = fields.Char()
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))

    # Contact
    contact_name = fields.Char()
    phone = fields.Char()
    email = fields.Char()

    # Time window
    requested_date = fields.Date()
    requested_time_from = fields.Float(string="Time From (hrs)", help="e.g. 8.0 = 8:00 AM")
    requested_time_to = fields.Float(string="Time To (hrs)", help="e.g. 12.0 = 12:00 PM")

    # Load
    pallet_count = fields.Integer(default=0)
    weight_lb = fields.Float(default=0.0)

    # Access
    dock_available = fields.Boolean()
    liftgate_required = fields.Boolean()
    appointment_required = fields.Boolean()

    # Reference
    reference = fields.Char()
    # Timing
    timing_type = fields.Selection([
        ("flexible", "Flexible"), ("time_window", "Time Window"),
        ("exact_appointment", "Exact Appointment"),
    ], default="flexible", string="Timing")
    requested_service_date = fields.Date(string="Requested Date")
    window_start = fields.Float(string="Window Start")
    window_end = fields.Float(string="Window End")
    appointment_time = fields.Float(string="Appointment Time")
    timezone = fields.Char(string="Timezone")

    instructions = fields.Text()

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
    sequence = fields.Integer(string="Stop #", required=True, default=1)
    saved_location_id = fields.Many2one(
        "logistics.saved.location", string="Saved Location",
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

    # Instructions
    instructions = fields.Text(string="Delivery Instructions")

from odoo import fields, models


class LogisticsBookingLine(models.Model):
    _name = "logistics.booking.line"
    _description = "Freight line on a confirmed booking (maps 1:1 to a prema.dispatch.item)"
    _order = "booking_id, sequence"

    booking_id = fields.Many2one("logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    description = fields.Char(default="LTL Shipment")
    pallets = fields.Integer(required=True, default=1)
    weight_lbs = fields.Float(required=True)
    commodity = fields.Char()

    # ── Phase 5: Multi-Leg ────────────────────────────────────────────
    leg_id = fields.Many2one(
        "logistics.booking.leg", string="Leg",
        help="Which operational leg this freight rides on (for multi-leg bookings)."
    )

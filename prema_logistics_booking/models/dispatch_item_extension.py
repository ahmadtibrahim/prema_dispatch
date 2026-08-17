"""Milk-run bridge fields on the dispatch item.

Same upward-comodel rule as dispatch_stop_extension.py: the Many2one to
logistics.booking.pallet must be defined here (in prema_logistics_booking)
so the comodel is always in the pool when the field is set up.
"""

from odoo import fields, models


class PremaDispatchItem(models.Model):
    _inherit = "prema.dispatch.item"

    logistics_booking_pallet_id = fields.Many2one(
        "logistics.booking.pallet", string="Booking Pallet",
        ondelete="set null", index=True,
        help="Stable bridge to the canonical booking pallet movement.")

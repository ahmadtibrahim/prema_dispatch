from odoo import _, fields, models
from odoo.exceptions import AccessError


class LogisticsBookingPriceAdjust(models.TransientModel):
    """Manager-only post-confirmation customer sell-price adjustment.

    Requires a new price and a reason; blocked once a customer invoice is
    posted (use the normal credit/debit invoice workflow after that).
    Delegates to logistics.booking._apply_sell_price_change — the single
    authority for booking-level sell-price changes (preserves the original
    confirmed price, appends a booking-level snapshot line, recomputes the
    estimated margin, and never touches physical leg frozen prices, carrier
    BUY rates, or corridor pricing).
    """

    _name = "logistics.booking.price.adjust"
    _description = "Adjust Customer Sell Price"

    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", required=True, readonly=True)
    new_price = fields.Float(
        string="New Customer Sell Price", required=True, digits=(12, 2),
        help="Final price to bill the customer. Any positive amount is "
             "allowed (discounts AND increases).")
    reason = fields.Char(string="Reason", required=True)

    def action_apply(self):
        self.ensure_one()
        if not self.env.user.has_group(
                "prema_logistics_booking.group_logistics_booking_manager"):
            raise AccessError(_(
                "Only booking managers may adjust customer sell prices."))
        self.booking_id._apply_sell_price_change(self.new_price, self.reason)
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.booking",
            "res_id": self.booking_id.id,
            "view_mode": "form",
            "target": "current",
        }

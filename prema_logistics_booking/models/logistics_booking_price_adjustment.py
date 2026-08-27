# ════════════════════════════════════════════════════════════════════
# Post-confirmation customer sell-price adjustment history.
#
# price_snapshot is IMMUTABLE after booking confirmation — a later
# "Adjust Customer Price" action NEVER modifies it (no appended lines, no
# rewritten amounts). Every post-confirmation adjustment is recorded here
# as an append-only, structured audit row, alongside the flat audit
# fields on the booking and a mail.message audit note.
# ════════════════════════════════════════════════════════════════════
from odoo import _, fields, models
from odoo.exceptions import UserError


class LogisticsBookingPriceAdjustment(models.Model):
    _name = "logistics.booking.price.adjustment"
    _description = "Booking Price Adjustment"
    _order = "create_date, id"

    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", required=True,
        ondelete="cascade", index=True)
    old_price = fields.Float(string="Old Price", readonly=True, digits=(12, 2))
    new_price = fields.Float(string="New Price", readonly=True, digits=(12, 2))
    adjustment_amount = fields.Float(
        string="Adjustment", readonly=True, digits=(12, 2),
        help="New price minus old price (negative = discount).")
    reason = fields.Char(string="Reason", readonly=True)
    changed_by = fields.Many2one("res.users", string="Changed By", readonly=True)
    changed_at = fields.Datetime(string="Changed At", readonly=True)

    def write(self, vals):
        """Append-only audit history — edits are always refused."""
        raise UserError(_(
            "Booking price adjustment history is append-only and may never "
            "be edited."))

    def unlink(self):
        """Append-only audit history — deletion is always refused."""
        raise UserError(_(
            "Booking price adjustment history is append-only and may never "
            "be deleted."))

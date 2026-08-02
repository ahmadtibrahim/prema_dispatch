"""Link account.move back to logistics.booking for the booking→invoice flow.

Enforces tax-review blocking: invoices linked to bookings requiring tax review
cannot be posted or sent until the tax configuration is resolved.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMoveBooking(models.Model):
    _inherit = "account.move"

    logistics_booking_id = fields.Many2one(
        "logistics.booking", string="Logistics Booking",
        readonly=True, index=True, copy=False,
        help="The confirmed logistics booking that generated this draft invoice.",
    )

    # ── Tax Review Posting Constraint ────────────────────────────────────

    def action_post(self):
        """Block posting of invoices linked to bookings requiring tax review.

        When a freight tax mapping is missing, the invoice must remain Draft
        until an accounting user resolves the tax configuration and clears
        the tax_review_required flag on the booking.
        """
        for move in self:
            if move.logistics_booking_id and move.logistics_booking_id.tax_review_required:
                booking = move.logistics_booking_id
                raise UserError(_(
                    "Cannot post invoice %(invoice)s because the linked "
                    "booking %(booking)s requires freight tax review.\n\n"
                    "Reason: %(reason)s\n\n"
                    "Action required:\n"
                    "1. Go to Settings → Prema Logistics → Freight Tax Configuration\n"
                    "2. Configure the missing tax mapping(s)\n"
                    "3. Open booking %(booking)s and clear the Tax Review flag\n"
                    "4. Then post this invoice.",
                    invoice=move.name or "Draft",
                    booking=booking.booking_number,
                    reason=booking.tax_reason or "Tax configuration missing",
                ))
        return super().action_post()

    def _can_send_invoice(self):
        """Prevent sending/emailing invoices linked to tax-review bookings."""
        for move in self:
            if move.logistics_booking_id and move.logistics_booking_id.tax_review_required:
                return False
        return super()._can_send_invoice()

    def action_invoice_sent(self):
        """Block the Send & Print wizard for tax-review bookings."""
        for move in self:
            if move.logistics_booking_id and move.logistics_booking_id.tax_review_required:
                booking = move.logistics_booking_id
                raise UserError(_(
                    "Cannot send invoice %(invoice)s: the linked booking "
                    "%(booking)s requires freight tax review.\n\n"
                    "Reason: %(reason)s\n\n"
                    "Please resolve the tax configuration first.",
                    invoice=move.name or "Draft",
                    booking=booking.booking_number,
                    reason=booking.tax_reason or "Tax configuration missing",
                ))
        return super().action_invoice_sent()

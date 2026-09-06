"""Link account.move back to logistics.booking for the booking→invoice flow.

Enforces tax-review blocking: invoices linked to bookings requiring tax review
cannot be posted or sent until the tax configuration is resolved.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMoveBooking(models.Model):
    _inherit = "account.move"

    logistics_booking_id = fields.Many2one(
        "logistics.booking", string="Logistics Booking",
        readonly=True, index=True, copy=False,
        help="The confirmed logistics booking that generated this draft invoice.",
    )

    # ── §7 (D-B2) payment snapshot ───────────────────────────────────────
    # Copied from the booking at invoice creation (invoice = customer
    # document). Edits here are a DOCUMENT OVERRIDE: they win and are
    # audited (explicit mail.message row on write — see write() below;
    # no tracking= attribute, so the commit-time chatter tracker cannot
    # duplicate the row) — never silently replayed back onto the booking.
    logistics_payment_method_id = fields.Many2one(
        "logistics.payment.method", string="Payment Method",
        help="Payment method agreed with the customer (credit card / "
             "Interac e-Transfer / terms). Copied from the booking; "
             "overriding it on the invoice is audited.")
    logistics_payment_instructions = fields.Text(
        string="Payment Instructions",
        help="Resolved customer-facing instructions shown on this "
             "document (e-Transfer instructions or secure card-payment "
             "link) at issue time.")
    logistics_price_tax_mode = fields.Selection([
        ("exclusive", "Exclusive of Taxes"),
        ("inclusive", "Inclusive of Taxes"),
    ], string="Price Is", readonly=True,
        help="How the freight price on this invoice is stated: the "
             "booking's agreed basis (exclusive = tax added on top; "
             "inclusive = the total is the agreed amount).")
    logistics_quickpay_apply = fields.Boolean(
        string="QuickPay Discount Applies", default=False)
    logistics_quickpay_discount_pct = fields.Float(
        string="QuickPay Discount %")
    logistics_quickpay_deadline_days = fields.Integer(
        string="QuickPay Deadline (days)", default=0)
    logistics_quickpay_deadline_date = fields.Date(
        string="QuickPay Deadline", compute="_compute_logistics_quickpay_deadline_date",
        help="Invoice date + QuickPay deadline days — the last day the "
             "discount applies.")
    logistics_quickpay_discount_amount = fields.Float(
        string="QuickPay Discount Amount", readonly=True)
    logistics_quickpay_discounted_total = fields.Float(
        string="Discounted Total (paid by deadline)", readonly=True)

    @api.depends("invoice_date", "logistics_quickpay_deadline_days")
    def _compute_logistics_quickpay_deadline_date(self):
        for move in self:
            if not move.invoice_date or not move.logistics_quickpay_deadline_days:
                move.logistics_quickpay_deadline_date = False
                continue
            move.logistics_quickpay_deadline_date = (
                move.invoice_date + datetime.timedelta(
                    days=move.logistics_quickpay_deadline_days))

    def write(self, vals):
        """§7 document override on the invoice is never silent: every
        change to the payment-snapshot fields creates a mail.message
        audit row (deterministic, mirrors the logistics.booking audit —
        the chatter tracker only runs at transaction commit and cannot be
        asserted inside a transaction)."""
        _AUDITED = {
            "logistics_payment_method_id": "Payment Method",
            "logistics_payment_instructions": "Payment Instructions",
            "logistics_quickpay_apply": "QuickPay Discount Applies",
            "logistics_quickpay_discount_pct": "QuickPay Discount %",
            "logistics_quickpay_deadline_days": "QuickPay Deadline (days)",
        }
        if not (_AUDITED.keys() & vals.keys()):
            return super().write(vals)
        changes = []
        for rec in self:
            for fname, label in _AUDITED.items():
                if fname not in vals:
                    continue
                new_value = vals.get(fname)
                old_value = getattr(rec, fname)
                if fname.endswith("_id"):
                    old_value = old_value.id if old_value else False
                if bool(new_value) != bool(old_value) or \
                        (new_value is not None and str(new_value).strip()
                         != str(old_value or "").strip()):
                    changes.append((rec, label, old_value, new_value))
        result = super().write(vals)
        for rec, label, old_value, new_value in changes:
            self.env["mail.message"].sudo().create({
                "model": rec._name,
                "res_id": rec.id,
                "body": self.env["logistics.booking"]._payment_change_audit_message(
                    label, old_value, new_value,
                    self.env.user.name or ""),
                "message_type": "comment",
                "subtype_id": self.env.ref("mail.mt_comment").id,
            })
        return result

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

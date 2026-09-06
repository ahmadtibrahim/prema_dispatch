from odoo import _, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    """Make Logistics Booking the single Sales-to-Dispatch authority."""

    _inherit = "sale.order"

    logistics_booking_ids = fields.One2many(
        "logistics.booking",
        "sale_order_id",
        string="Canonical Dispatch Bookings",
        readonly=True,
        copy=False,
    )

    def _open_canonical_booking(self, booking):
        return {
            "type": "ir.actions.act_window",
            "name": _("Dispatch Booking"),
            "res_model": "logistics.booking",
            "res_id": booking.id,
            "view_mode": "form",
            "target": "current",
        }

    def _dispatch_rate_source_text(self):
        self.ensure_one()
        text = (getattr(self, "x_so_text_input", "") or "").strip()
        if text:
            return text
        details = [
            "Pickup: %s" % (getattr(self, "pickup_city", "") or ""),
            "Delivery: %s" % (getattr(self, "delivery_city", "") or ""),
            "Reference: %s" % (
                getattr(self, "load_reference", "")
                or self.client_order_ref
                or self.name
                or ""
            ),
        ]
        line_text = "\n".join(self.order_line.filtered(
            lambda line: not line.display_type
        ).mapped("name"))
        if line_text:
            details.append(line_text)
        return "\n".join(line for line in details if not line.endswith(": "))

    def _open_dispatch_rate_review(self):
        self.ensure_one()
        opportunity = getattr(self, "opportunity_id", False)
        return {
            "type": "ir.actions.act_window",
            "name": _("Review Rate & Schedule"),
            "res_model": "logistics.phone.booking",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.partner_id.id,
                "default_sale_order_id": self.id,
                "default_crm_lead_id": opportunity.id if opportunity else False,
                "default_source_text": self._dispatch_rate_source_text(),
            },
        }

    def action_book_load(self):
        """Open/reuse the canonical booking; never create a raw job."""
        self.ensure_one()
        booking = self.logistics_booking_ids[:1]
        if booking:
            return self._open_canonical_booking(booking)

        # Preserve access to old jobs without creating more legacy records.
        if self.dispatch_job_ids:
            return self._open_existing_job_action()

        if self.state not in ("sale", "done"):
            raise UserError(_(
                "This quotation is still editable. Confirm it internally "
                "before booking the load to a truck."
            ))
        return self._open_dispatch_rate_review()

    def action_generate_dispatch_from_text(self):
        """Extract and review in the canonical engine; do not auto-book."""
        self.ensure_one()
        if not (getattr(self, "x_so_text_input", "") or "").strip():
            raise UserError(_(
                "Paste the customer freight request in Generate from Text first."
            ))
        return self._open_dispatch_rate_review()

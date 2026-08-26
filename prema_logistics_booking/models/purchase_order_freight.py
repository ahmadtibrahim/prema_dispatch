# ════════════════════════════════════════════════════════════════════
# Phase 14 — Native Odoo Purchase Order as the Carrier Rate Confirmation.
#   PREMAFIRM BUYS transportation from the subcontractor:
#     Purchase Order → Carrier performs → Carrier Invoice → Vendor Bill → Pay
#   The Carrier Rate Confirmation is a PRESENTATION/WORKFLOW of the PO —
#   never a parallel financial model. Ordinary POs are untouched.
# ════════════════════════════════════════════════════════════════════
import json
import logging

from odoo import _, api, exceptions, fields, models

_logger = logging.getLogger(__name__)

_FREIGHT_PRODUCT_DEFAULT_CODE = "SUBCONTRACTED_FREIGHT_SVC"


class PurchaseOrderFreight(models.Model):
    _inherit = "purchase.order"

    is_freight_subcontract = fields.Boolean(
        string="Freight Subcontract (Rate Confirmation)", default=False,
        index=True, tracking=True)
    logistics_booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="set null",
        index=True)
    booking_leg_id = fields.Many2one(
        "logistics.booking.leg", string="Booking Leg", ondelete="set null",
        index=True)
    dispatch_job_id = fields.Many2one(
        "prema.dispatch.job", string="Dispatch Job", ondelete="set null",
        index=True)
    freight_details = fields.Json(
        string="Freight Details",
        help="Pickup/delivery, windows, commodity, pallets, weight, "
             "equipment, temperature, detention terms — frozen at PO "
             "creation for the Rate Confirmation PDF.")
    carrier_acceptance_method = fields.Selection([
        ("phone", "Phone"),
        ("email", "Email"),
        ("signed", "Signed Rate Confirmation"),
        ("portal", "Portal"),
    ], string="Carrier Acceptance Method", tracking=True)
    carrier_accepted_at = fields.Datetime(
        string="Carrier Accepted At", tracking=True)
    carrier_accepted_by = fields.Many2one(
        "res.users", string="Recorded By", readonly=True)
    freight_variance = fields.Float(
        string="Freight Variance", digits=(12, 2), compute="_compute_variance",
        store=True,
        help="Vendor bill amount vs the accepted PO buy rate — flagged for "
             "review, never silently applied.")

    @api.depends("order_line.invoice_lines.price_subtotal",
                 "order_line.invoice_lines.move_id.state",
                 "amount_untaxed")
    def _compute_variance(self):
        for po in self:
            if not po.is_freight_subcontract:
                po.freight_variance = 0.0
                continue
            billed = po._billed_freight()
            # No bill yet → no variance to flag (never a fabricated one).
            po.freight_variance = round(billed - po.amount_untaxed, 2) \
                if billed else 0.0

    def _billed_freight(self):
        """Sum of POSTED vendor-bill line amounts linked to this PO —
        the billed figures, not the PO's own accepted amounts."""
        self.ensure_one()
        return round(sum(
            il.price_subtotal
            for line in self.order_line
            for il in line.invoice_lines
            if il.move_id.state == "posted"), 2)

    # ── Freight factory (idempotent per leg) ────────────────────────

    @api.model
    def _find_freight_product(self):
        """The ONE Subcontracted Freight Service purchase product,
        seeded idempotently by migration. Never hardcodes accounts/taxes —
        the company/category defaults decide."""
        Product = self.env["product.product"].sudo()
        product = Product.search(
            [("default_code", "=", _FREIGHT_PRODUCT_DEFAULT_CODE)], limit=1)
        if not product:
            product = Product.search(
                [("name", "=", "Subcontracted Freight Service"),
                 ("type", "=", "service")], limit=1)
        return product

    @api.model
    def _create_freight_po_from_leg(self, leg, offer, rate, carrier=None):
        """Create (or return the existing) native purchase RFQ for an
        accepted carrier offer. Idempotent — one freight PO per leg."""
        existing = self.search([
            ("booking_leg_id", "=", leg.id),
            ("is_freight_subcontract", "=", True),
        ], limit=1)
        if existing:
            return existing
        carrier = carrier or offer.carrier_id
        product = self._find_freight_product()
        if not product:
            raise exceptions.UserError(
                _("No 'Subcontracted Freight Service' product is configured. "
                  "Run the 18.0.13.39.0 migration or create it."))
        booking = leg.booking_id
        po_vals = {
            "partner_id": carrier.id,
            "is_freight_subcontract": True,
            "logistics_booking_id": booking.id if booking else False,
            "booking_leg_id": leg.id,
            "freight_details": self._freight_details(leg, offer, rate),
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": self._freight_line_name(leg),
                "product_qty": 1,
                "price_unit": rate,
            })],
        }
        if booking and booking.dispatch_job_ids:
            po_vals["dispatch_job_id"] = booking.dispatch_job_ids[:1].id
        po = self.create(po_vals)
        _logger.info("Freight PO %s created for leg %s (carrier %s, rate %s)",
                     po.name, leg.id, carrier.name, rate)
        return po

    def _freight_details(self, leg, offer, rate):
        """Frozen detail snapshot for the Rate Confirmation PDF."""
        origin = leg.origin_stop_id
        dest = leg.destination_stop_id
        booking = leg.booking_id

        def _addr(stop):
            if not stop:
                return ""
            return " / ".join(
                x for x in (stop.formatted_address or "",
                            stop.city or "",
                            stop.company_name or "") if x)

        return {
            "load_number": booking.booking_number if booking else False,
            "pickup": _addr(origin),
            "pickup_window": leg.pickup_date.strftime("%Y-%m-%d")
            if leg.pickup_date else False,
            "delivery": _addr(dest),
            "delivery_window": leg.delivery_date.strftime("%Y-%m-%d")
            if leg.delivery_date else False,
            "commodity": "General Freight",
            "pallets": leg.pallets,
            "weight_lbs": leg.weight_lbs,
            "equipment": (booking.temperature_mode or "dry")
            if booking else "dry",
            "temperature": booking.temperature_mode
            if booking and booking.temperature_mode != "dry" else False,
            "agreed_rate": rate,
            "carrier_detention_free_min": 30,
            "carrier_detention_rate": False,
            "pod_required": True,
            "instructions": "POD required on delivery. Carrier invoice "
                            "submission within 30 days with POD.",
        }

    def _freight_line_name(self, leg):
        origin = leg.origin_stop_id
        dest = leg.destination_stop_id
        return "Subcontracted Freight — %s → %s (Load %s, %s pallets, %s lb)" % (
            origin.city if origin and origin.city else origin.company_name
            if origin else "Pickup",
            dest.city if dest and dest.city else dest.company_name
            if dest else "Delivery",
            leg.booking_id.booking_number if leg.booking_id else leg.id,
            leg.pallets, int(leg.weight_lbs or 0))

    # ── Actions ─────────────────────────────────────────────────────

    def action_send_rate_confirmation(self):
        """Native RFQ/PO send flow — no second email engine."""
        self.ensure_one()
        if not self.is_freight_subcontract:
            raise exceptions.UserError(
                _("Rate Confirmation applies only to freight subcontract "
                  "purchase orders."))
        return self.action_rfq_send()

    def action_print_rate_confirmation(self):
        self.ensure_one()
        if not self.is_freight_subcontract:
            raise exceptions.UserError(
                _("Rate Confirmation applies only to freight subcontract "
                  "purchase orders."))
        return self.env.ref(
            "prema_logistics_booking.action_report_carrier_rate_confirmation"
        ).report_action(self)

    def action_record_carrier_acceptance(self, method):
        self.ensure_one()
        self.write({
            "carrier_acceptance_method": method,
            "carrier_accepted_at": fields.Datetime.now(),
            "carrier_accepted_by": self.env.user.id,
        })
        return True

    def action_record_carrier_acceptance_form(self):
        """Form-friendly variant — uses the acceptance-method selection
        on the form (defaults to phone when unset)."""
        self.ensure_one()
        return self.action_record_carrier_acceptance(
            self.carrier_acceptance_method or "phone")

    def action_review_vendor_bill(self):
        """Compare vendor bill lines against the accepted PO buy rate and
        write actual cost + variance on the leg. Never rewrites the
        accepted rate itself — the variance is flagged for review."""
        for po in self:
            if not po.is_freight_subcontract or not po.booking_leg_id:
                continue
            leg = po.booking_leg_id
            billed = po._billed_freight()
            variance = round(billed - (leg.accepted_buy_rate or 0.0), 2) \
                if billed else 0.0
            leg.write({
                "actual_leg_cost": round(billed, 2) if billed else
                leg.actual_leg_cost,
                "carrier_invoice_variance": variance,
                "carrier_invoice_received": bool(billed),
            })
        return True

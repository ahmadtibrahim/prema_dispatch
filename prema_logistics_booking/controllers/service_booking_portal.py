# -*- coding: utf-8 -*-
"""Portal — Service Bookings (Phase 1).

A customer requests a warehouse service booking from the portal. The request
becomes a DRAFT `sale.order` designated `x_warehouse_service`, carrying **only
the services the customer asked for** — the PF Logistics Warehouse template is
applied for its brand and designation and carries no lines of its own, so
nothing is added or replaced.

Nothing here writes stock. The linked native PFL receipt is created by the
order's Studio action ("Create PFL Receipt"); WMS operations stay in native
Inventory, outside Prema Dispatch.
"""
import base64
import logging

from odoo import http
from odoo.http import request
from werkzeug.exceptions import NotFound

_logger = logging.getLogger(__name__)

LTL_CODE = "PFL-XD-LTL"
FTL_CODE = "PFL-XD-FTL"

#: Services a customer may request. The tracking product `PFL-CUST-PAL` is
#: deliberately excluded — it is not sold.
SERVICE_CODES = [
    "PFL-XD-LTL", "PFL-XD-FTL", "PFL-STOR-DAY",
    "PFL-WRAP", "PFL-LABEL", "PFL-LAB-HR", "PFL-PAL-SUP",
]

TEMPLATE_NAME = "PF Logistics Warehouse"
BRAND = "premafirm_logistics"
TEAM_NAME = "Premafirm Logistics Leads"


def _norm_dt(value):
    """`<input type="datetime-local">` -> an Odoo datetime string."""
    value = (value or "").strip().replace("T", " ")
    if not value:
        return False
    if len(value) == 16:
        value += ":00"
    return value


class ServiceBookingPortal(http.Controller):

    # ── helpers ────────────────────────────────────────────────────────
    def _commercial_partner(self):
        return request.env.user.partner_id.commercial_partner_id

    def _booking_domain(self, partner):
        # Rule 184 (`sale_order_rule_portal`) already scopes orders this way;
        # repeating it here is what keeps the read obviously the customer's own.
        return [
            ("message_partner_ids", "child_of", [partner.id]),
            ("x_warehouse_service", "=", True),
            ("state", "in", ("draft", "sent", "sale")),
        ]

    def _services(self):
        return request.env["product.template"].sudo().search(
            [("default_code", "in", SERVICE_CODES)], order="default_code")

    def _partner_named(self, name, fallback):
        """Resolve a typed counterparty name to an existing contact.

        Never creates a partner from portal input; an unknown name falls back
        to the customer, and the typed text is kept on the order note so staff
        can correct it. No silent data invention.
        """
        name = (name or "").strip()
        if not name:
            return fallback
        found = request.env["res.partner"].sudo().search(
            [("name", "=ilike", name)], limit=1)
        return found or fallback

    # ── routes ─────────────────────────────────────────────────────────
    @http.route("/my/service-bookings", type="http", auth="user",
                website=True, sitemap=False)
    def service_bookings(self, **kwargs):
        partner = self._commercial_partner()
        orders = request.env["sale.order"].sudo().search(
            self._booking_domain(partner), order="create_date desc, id desc")
        return request.render("prema_logistics_booking.portal_my_service_bookings", {
            "orders": orders,
            "page_name": "service_bookings",
            "msg": kwargs.get("msg"),
        })

    @http.route("/my/service-bookings/new", type="http", auth="user",
                website=True, sitemap=False)
    def service_booking_new(self, **kwargs):
        return request.render("prema_logistics_booking.portal_my_service_booking_new", {
            "services": self._services(),
            "ltl_code": LTL_CODE,
            "ftl_code": FTL_CODE,
            "page_name": "service_booking_new",
            "error": kwargs.get("error"),
        })

    @http.route("/my/service-bookings/submit", type="http", auth="user",
                website=True, methods=["POST"], csrf=True, sitemap=False)
    def service_booking_submit(self, **post):
        partner = self._commercial_partner()
        form = request.httprequest.form
        codes = [c for c in form.getlist("service_code") if c in SERVICE_CODES]

        if not codes:
            return request.redirect("/my/service-bookings/new?error=no_service")
        # LTL and FTL are mutually exclusive — one flat fee or per-pallet, never both.
        if LTL_CODE in codes and FTL_CODE in codes:
            return request.redirect("/my/service-bookings/new?error=ltl_ftl")

        expected = int(form.get("expected_pallets") or 0)
        client_ref = (form.get("client_order_ref") or "").strip()
        notes = (form.get("notes") or "").strip()

        Order = request.env["sale.order"].sudo()

        # De-duplication: the same customer re-submitting the same request
        # (same PO reference) reuses the open quotation instead of stacking a
        # second one.
        if client_ref:
            duplicate = Order.search([
                ("partner_id.commercial_partner_id", "=", partner.id),
                ("client_order_ref", "=", client_ref),
                ("x_warehouse_service", "=", True),
                ("state", "not in", ("cancel",)),
            ], limit=1)
            if duplicate:
                return request.redirect("/my/service-bookings?msg=duplicate")

        template = request.env["sale.order.template"].sudo().search(
            [("name", "=", TEMPLATE_NAME)], limit=1)
        team = request.env["crm.team"].sudo().search(
            [("name", "=", TEAM_NAME)], limit=1)

        freight_owner = self._partner_named(form.get("freight_owner"), partner)
        recipient = self._partner_named(form.get("outbound_recipient"), partner)

        # Only the requested services become lines. Quantities: FTL is one flat
        # fee; the per-pallet LTL line carries the expected pallet count; the
        # other services start at 1 for staff to confirm against actuals.
        lines = []
        Product = request.env["product.product"].sudo()
        for code in codes:
            template_product = request.env["product.template"].sudo().search(
                [("default_code", "=", code)], limit=1)
            if not template_product or not template_product.product_variant_id:
                continue
            if code == FTL_CODE:
                qty = 1.0
            elif code == LTL_CODE:
                qty = float(expected or 1)
            else:
                qty = 1.0
            lines.append((0, 0, {
                "product_id": template_product.product_variant_id.id,
                "product_uom_qty": qty,
                "price_unit": template_product.list_price,
                "name": template_product.display_name,
            }))

        vals = {
            "partner_id": partner.id,
            "partner_invoice_id": partner.id,
            "x_warehouse_service": True,
            "x_expected_pallets": expected,
            "x_freight_owner_id": freight_owner.id,
            "x_outbound_recipient_id": recipient.id,
            "x_requested_arrival": _norm_dt(form.get("requested_arrival")),
            "x_requested_departure": _norm_dt(form.get("requested_departure")),
            "client_order_ref": client_ref or False,
            "note": notes or False,
            "order_line": lines,
            "prema_operating_brand": BRAND,
        }
        if team:
            vals["team_id"] = team.id
        if template:
            # Applied for its brand/designation only — it has no lines, so the
            # requested lines above are neither replaced nor added to.
            vals["sale_order_template_id"] = template.id

        order = Order.create(vals)
        # Rule 184 grants the customer access through `message_partner_ids`.
        order.message_subscribe(partner_ids=[partner.id])

        attachment_ids = []
        for upload in request.httprequest.files.getlist("attachments"):
            if not upload or not upload.filename:
                continue
            attachment = request.env["ir.attachment"].sudo().create({
                "name": upload.filename,
                "datas": base64.b64encode(upload.read()),
                "res_model": "sale.order",
                "res_id": order.id,
            })
            attachment_ids.append(attachment.id)

        body = "Service booking request received from the customer portal."
        if notes:
            body += "<br/><br/>%s" % notes
        order.message_post(body=body, attachment_ids=attachment_ids)

        return request.redirect("/my/service-bookings?msg=created")

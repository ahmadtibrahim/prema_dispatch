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

    def _own_contacts(self):
        """The caller's OWN private contacts — never the global directory."""
        return request.env["logistics.private.contact"].search(
            [("user_id", "=", request.env.user.id)], order="name")

    def _own_contact(self, contact_id):
        """Resolve a submitted contact id to a row this user owns.

        A foreign or malformed id resolves to an empty recordset — the same
        ownership rule the contact routes use, so a directly submitted id can
        never reach another user's contact.
        """
        try:
            contact_id = int(contact_id or 0)
        except (TypeError, ValueError):
            contact_id = 0
        if not contact_id:
            return request.env["logistics.private.contact"].browse()
        return request.env["logistics.private.contact"].search(
            [("id", "=", contact_id), ("user_id", "=", request.env.user.id)], limit=1)

    def _own_order(self, order_id):
        """A warehouse service booking belonging to the caller's company."""
        partner = self._commercial_partner()
        order = request.env["sale.order"].sudo().search(
            [("id", "=", int(order_id or 0)),
             ("message_partner_ids", "child_of", [partner.id]),
             ("x_warehouse_service", "=", True)], limit=1)
        if not order:
            raise NotFound()
        return order

    def _own_collection(self, order):
        """The order's current (uncollected) authorization record.

        Returned WITHOUT sudo on purpose: look-ups run elevated, but the write
        itself must pass through the model's portal guard.
        """
        Collection = request.env["logistics.pickup.collection"]
        ids = Collection.sudo().search(
            [("sale_order_id", "=", order.id), ("state", "=", "authorized")],
            order="id desc", limit=1).ids
        if not ids:
            ids = Collection.sudo().create(
                {"sale_order_id": order.id}).ids
        return Collection.browse(ids)

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

        # Counterparties are the caller's OWN saved contacts, resolved by id and
        # re-checked here — a foreign id simply falls back to the customer.
        owner_contact = self._own_contact(form.get("freight_owner_contact_id"))
        recipient_contact = self._own_contact(form.get("recipient_contact_id"))
        pickup_contact = self._own_contact(form.get("pickup_party_contact_id"))
        freight_owner = owner_contact.partner_id or partner
        recipient = recipient_contact.partner_id or partner

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

        # Trip-specific pickup authorization — held on the booking, never on
        # the reusable contact. Created here so the customer can add or change
        # the driver later, before collection.
        collection = request.env["logistics.pickup.collection"].sudo().create({
            "sale_order_id": order.id,
            "outbound_recipient_id": recipient.id if recipient != partner else False,
            "driver_name": (form.get("driver_name") or "").strip() or False,
            "driver_phone": (form.get("driver_phone") or "").strip() or False,
            "truck_details": (form.get("truck_details") or "").strip() or False,
            "pickup_reference": (form.get("pickup_reference") or "").strip() or False,
            "customer_instructions": (form.get("customer_instructions") or "").strip() or False,
        })
        if pickup_contact:
            # sudo: the contact was validated as the caller's own above.
            collection.action_apply_snapshot(pickup_contact)

        return request.redirect("/my/service-bookings?msg=created")

    # ── customer pickup authorization ──────────────────────────────────
    @http.route("/my/service-bookings/<int:order_id>/pickup", type="http",
                auth="user", website=True, sitemap=False)
    def service_booking_pickup(self, order_id, **kwargs):
        order = self._own_order(order_id)
        collections = request.env["logistics.pickup.collection"].sudo().search(
            [("sale_order_id", "=", order.id)], order="id desc")
        return request.render("prema_logistics_booking.portal_service_booking_pickup", {
            "order": order,
            "collection": self._own_collection(order),
            "collections": collections,
            "contacts": self._own_contacts(),
            "page_name": "service_bookings",
            "msg": kwargs.get("msg"),
        })

    @http.route("/my/service-bookings/<int:order_id>/pickup/save", type="http",
                auth="user", website=True, methods=["POST"], csrf=True, sitemap=False)
    def service_booking_pickup_save(self, order_id, **post):
        order = self._own_order(order_id)          # 404 for a foreign booking
        collection = self._own_collection(order)
        if collection.state == "collected":
            return request.redirect(
                "/my/service-bookings/%s/pickup?msg=collected" % order.id)

        # Driver details — the customer's own fields, guarded by the model.
        collection.write({
            "driver_name": (post.get("driver_name") or "").strip(),
            "driver_phone": (post.get("driver_phone") or "").strip(),
            "truck_details": (post.get("truck_details") or "").strip(),
            "pickup_reference": (post.get("pickup_reference") or "").strip(),
            "customer_instructions": (post.get("customer_instructions") or "").strip(),
        })

        # Authorized party / recipient are partner links: set under sudo, but
        # only after the chosen contact was proven to be the caller's own.
        elevated = {}
        contact = self._own_contact(post.get("pickup_party_contact_id"))
        if contact:
            elevated.update(contact._snapshot_vals())
            elevated["authorized_contact_id"] = contact.id
        recipient_contact = self._own_contact(post.get("recipient_contact_id"))
        elevated["outbound_recipient_id"] = (
            recipient_contact.partner_id.id if recipient_contact else False)
        if elevated:
            collection.sudo().write(elevated)

        return request.redirect(
            "/my/service-bookings/%s/pickup?msg=saved" % order.id)

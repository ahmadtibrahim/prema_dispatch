# -*- coding: utf-8 -*-
"""Portal — Service Bookings.

A customer requests a warehouse service booking from the portal. The request
becomes a DRAFT `sale.order` designated `x_warehouse_service`, carrying **only
the services the customer asked for** — the PF Logistics Warehouse template is
applied for its brand and designation and carries no lines of its own, so
nothing is added or replaced.

Counterparties (freight owner, delivery recipient, authorized pickup party) are
chosen from the customer's OWN private contacts through a searchable picker.
The picker's browser behaviour lives in
`static/src/js/portal_contact_picker.js`; every rule it enforces here in the
browser is re-checked below, because the browser is convenience and this is the
control.

**Authorizing a company without knowing the driver.** The authorized pickup
party is the only mandatory party field: a customer who hires "Ryder Trucking"
authorizes the company and leaves the driver blank. The driver columns are
optional everywhere, and nothing on either form ever requires a driver name.

**Warehouse appointment times are America/Toronto.** The `<input
type="datetime-local">` gives a wall-clock reading in the warehouse's own zone,
which is converted to UTC for storage and back again for display, with DST
handled by `zoneinfo`. Storing the raw reading would silently shift every
appointment by the UTC offset.

Nothing here writes stock. The linked native PFL receipt is created by the
order's Studio action ("Create PFL Receipt"); WMS operations stay in native
Inventory, outside Prema Dispatch.
"""
import base64
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

#: Every warehouse appointment is entered and shown in the warehouse's own
#: zone. Storage is always UTC.
WAREHOUSE_TZ = "America/Toronto"
_TZ = ZoneInfo(WAREHOUSE_TZ)

#: The three pickers on the forms, in the order they appear.
PICKER_FIELDS = ("freight_owner_contact_id", "recipient_contact_id",
                 "pickup_party_contact_id")

#: Free-text columns the customer may edit on a pending authorization.
DRIVER_FIELDS = ("driver_name", "driver_phone", "truck_details",
                 "pickup_reference", "customer_instructions")


# ── timezone helpers ───────────────────────────────────────────────────
def _local_input_to_utc(value):
    """`<input type="datetime-local">` (warehouse local) -> naive UTC."""
    value = (value or "").strip().replace("T", " ")
    if not value:
        return False
    if len(value) == 16:
        value += ":00"
    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (naive.replace(tzinfo=_TZ)
            .astimezone(timezone.utc)
            .replace(tzinfo=None))


def _utc_to_local_input(value):
    """Stored naive UTC -> the `datetime-local` value for America/Toronto."""
    if not value:
        return ""
    return (value.replace(tzinfo=timezone.utc)
            .astimezone(_TZ)
            .strftime("%Y-%m-%dT%H:%M"))


def _utc_to_local_display(value):
    """Stored naive UTC -> a human reading, labelled with the zone."""
    if not value:
        return ""
    return "%s (%s)" % (
        value.replace(tzinfo=timezone.utc).astimezone(_TZ).strftime("%Y-%m-%d %I:%M %p"),
        WAREHOUSE_TZ)


def _clean(value):
    return (value or "").strip()


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
        try:
            order_id = int(order_id or 0)
        except (TypeError, ValueError):
            order_id = 0
        order = request.env["sale.order"].sudo().search(
            [("id", "=", order_id),
             ("message_partner_ids", "child_of", [partner.id]),
             ("x_warehouse_service", "=", True)], limit=1)
        if not order:
            raise NotFound()
        return order

    def _own_collections(self, order):
        return request.env["logistics.pickup.collection"].sudo().search(
            [("sale_order_id", "=", order.id)], order="id desc")

    def _pending_collection(self, order):
        """The order's open (not yet collected) authorization, if any.

        Never creates anything: viewing a page must not mint a record.
        """
        return request.env["logistics.pickup.collection"].sudo().search(
            [("sale_order_id", "=", order.id), ("state", "=", "authorized")],
            order="id desc", limit=1)

    def _pickup_form(self, order, post=None):
        """The values to render into the pickup form.

        On a fresh GET they come from the order's open authorization, carrying
        the customer's own earlier authorization forward when a previous
        pickup has already been collected. On a rejected POST they come from
        what the customer actually submitted, so nothing typed is ever lost.
        """
        collections = self._own_collections(order)
        pending = self._pending_collection(order)
        source = pending or collections[:1]

        if post is not None:
            return {
                "driver_name": _clean(post.get("driver_name")),
                "driver_phone": _clean(post.get("driver_phone")),
                "truck_details": _clean(post.get("truck_details")),
                "pickup_reference": _clean(post.get("pickup_reference")),
                "customer_instructions": _clean(post.get("customer_instructions")),
                "party_text": _clean(post.get("pickup_party_contact_id_text")),
                "party_id": post.get("pickup_party_contact_id") or "",
                "recipient_text": _clean(post.get("recipient_contact_id_text")),
                "recipient_id": post.get("recipient_contact_id") or "",
                "previous_party": False,
            }

        party = source.authorized_contact_id if source else source
        if party and not self._own_contact(party.id):
            party = False
        recipient = source.outbound_recipient_id if source else source
        recipient_contact = request.env["logistics.private.contact"].search(
            [("user_id", "=", request.env.user.id),
             ("partner_id", "=", recipient.id)], limit=1) if recipient else False

        return {
            "driver_name": source.driver_name or "" if source else "",
            "driver_phone": source.driver_phone or "" if source else "",
            "truck_details": source.truck_details or "" if source else "",
            "pickup_reference": source.pickup_reference or "" if source else "",
            "customer_instructions": source.customer_instructions or "" if source else "",
            "party_text": party.name if party else "",
            "party_id": party.id if party else "",
            "recipient_text": recipient_contact.name if recipient_contact else "",
            "recipient_id": recipient_contact.id if recipient_contact else "",
            # Shown as a note only, never as a silent pre-selection: a party
            # whose saved contact is gone must be re-chosen deliberately.
            "previous_party": (
                source.authorized_contact_name
                if source and not party and source.authorized_contact_name else False),
        }

    def _booking_form(self, post=None):
        """Values for the "Request a Service Booking" form.

        Returned on every render so a rejected submission comes back with what
        the customer typed still in place.
        """
        if post is None:
            return {"service_codes": [], "previous": False}
        return {
            "service_codes": [c for c in request.httprequest.form.getlist("service_code")
                              if c in SERVICE_CODES],
            "expected_pallets": _clean(post.get("expected_pallets")),
            "client_order_ref": _clean(post.get("client_order_ref")),
            "requested_arrival": _clean(post.get("requested_arrival")),
            "requested_departure": _clean(post.get("requested_departure")),
            "freight_owner_text": _clean(post.get("freight_owner_contact_id_text")),
            "freight_owner_id": post.get("freight_owner_contact_id") or "",
            "recipient_text": _clean(post.get("recipient_contact_id_text")),
            "recipient_id": post.get("recipient_contact_id") or "",
            "pickup_party_text": _clean(post.get("pickup_party_contact_id_text")),
            "pickup_party_id": post.get("pickup_party_contact_id") or "",
            "driver_name": _clean(post.get("driver_name")),
            "driver_phone": _clean(post.get("driver_phone")),
            "truck_details": _clean(post.get("truck_details")),
            "customer_instructions": _clean(post.get("customer_instructions")),
            "notes": _clean(post.get("notes")),
        }

    def _check_pickers(self, post, require_party=True):
        """Apply the picker rules server-side.

        Returns ``(resolved, error)`` where ``resolved`` maps each picker field
        to a private-contact recordset. Any typed-but-unselected text is an
        error — never silently dropped.
        """
        resolved = {}
        for field in PICKER_FIELDS:
            resolved[field] = self._own_contact(post.get(field))
        if require_party and not resolved["pickup_party_contact_id"]:
            typed = _clean(post.get("pickup_party_contact_id_text"))
            return resolved, ("party_unresolved" if typed else "party_required")
        for field in PICKER_FIELDS:
            typed = _clean(post.get("%s_text" % field))
            if typed and not resolved[field]:
                return resolved, "%s_unresolved" % field
        return resolved, None

    # ── routes ─────────────────────────────────────────────────────────
    @http.route("/my/service-bookings", type="http", auth="user",
                website=True, sitemap=False)
    def service_bookings(self, **kwargs):
        partner = self._commercial_partner()
        orders = request.env["sale.order"].sudo().search(
            self._booking_domain(partner), order="create_date desc, id desc")
        Collection = request.env["logistics.pickup.collection"].sudo()
        rows = []
        for order in orders:
            pending = Collection.search(
                [("sale_order_id", "=", order.id), ("state", "=", "authorized")],
                order="id desc", limit=1)
            rows.append({
                "order": order,
                "arrival_local": _utc_to_local_display(order.x_requested_arrival),
                "collection": pending,
                "carrier_name": pending.authorized_contact_name or "",
                "driver_name": pending.driver_name or "",
                "positions": order.pfl_reservation_ids.filtered(
                    lambda r: r.state == "reserved"),
                "capacity_state": order.pfl_capacity_state,
            })
        return request.render("prema_logistics_booking.portal_my_service_bookings", {
            "orders": orders,
            "rows": rows,
            "warehouse_tz": WAREHOUSE_TZ,
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
            "warehouse_tz": WAREHOUSE_TZ,
            "page_name": "service_bookings",
            "error": kwargs.get("error"),
            "form": self._booking_form(),
        })

    @http.route("/my/service-bookings/submit", type="http", auth="user",
                website=True, methods=["POST"], csrf=True, sitemap=False)
    def service_booking_submit(self, **post):
        partner = self._commercial_partner()

        def reject(code):
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": code,
                    "form": self._booking_form(post),
                })

        codes = [c for c in request.httprequest.form.getlist("service_code")
                 if c in SERVICE_CODES]
        if not codes:
            return reject("no_service")
        # LTL and FTL are mutually exclusive — one flat fee or per-pallet, never both.
        if LTL_CODE in codes and FTL_CODE in codes:
            return reject("ltl_ftl")

        resolved, error = self._check_pickers(post, require_party=True)
        if error:
            return reject(error)

        expected = int(_clean(post.get("expected_pallets")) or 0)
        client_ref = _clean(post.get("client_order_ref"))
        notes = _clean(post.get("notes"))
        arrival = _local_input_to_utc(post.get("requested_arrival"))
        departure = _local_input_to_utc(post.get("requested_departure"))

        # Space is promised BEFORE the order exists. The capacity report takes
        # the position locks, so what it reports free stays free for the
        # reservation this same transaction writes a moment later. A refusal
        # here leaves no order and no stranded reservation behind.
        if expected > 0 and not (arrival and departure):
            return reject("appointment_required")
        capacity = request.env["logistics.position.reservation"].sudo().pfl_capacity_report(
            arrival, departure, expected) if expected > 0 else None
        if capacity and not capacity["ok"]:
            form = self._booking_form(post)
            form["capacity_message"] = capacity["message"]
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": "capacity",
                    "form": form,
                })

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

        owner_contact = resolved["freight_owner_contact_id"]
        recipient_contact = resolved["recipient_contact_id"]
        pickup_contact = resolved["pickup_party_contact_id"]
        freight_owner = owner_contact.partner_id or partner
        recipient = recipient_contact.partner_id or partner

        # Only the requested services become lines. Quantities: FTL is one flat
        # fee; the per-pallet LTL line carries the expected pallet count; the
        # other services start at 1 for staff to confirm against actuals.
        lines = []
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
            "x_requested_arrival": arrival,
            "x_requested_departure": departure,
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
        # the reusable contact. Created here so the customer can change the
        # driver later, before collection. Only the authorized party is
        # required; the driver columns stay empty unless the customer knows
        # them, which is the normal case for a hired carrier.
        collection = request.env["logistics.pickup.collection"].sudo().create({
            "sale_order_id": order.id,
            "outbound_recipient_id": recipient.id if recipient != partner else False,
            "driver_name": _clean(post.get("driver_name")) or False,
            "driver_phone": _clean(post.get("driver_phone")) or False,
            "truck_details": _clean(post.get("truck_details")) or False,
            "customer_instructions": _clean(post.get("customer_instructions")) or False,
        })
        if pickup_contact:
            # sudo: the contact was validated as the caller's own above.
            collection.action_apply_snapshot(pickup_contact)

        # Now that the booking exists, take the space. The locks this
        # transaction already holds make this a formality; if anything did go
        # wrong the whole request rolls back, order included.
        if expected > 0:
            order.action_pfl_reserve_space()

        return request.redirect("/my/service-bookings?msg=created")

    # ── customer pickup authorization ──────────────────────────────────
    @http.route("/my/service-bookings/<int:order_id>/pickup", type="http",
                auth="user", website=True, sitemap=False)
    def service_booking_pickup(self, order_id, **kwargs):
        order = self._own_order(order_id)
        return self._render_pickup(order, msg=kwargs.get("msg"))

    def _render_pickup(self, order, msg=None, error=None, post=None):
        collections = self._own_collections(order)
        return request.render("prema_logistics_booking.portal_service_booking_pickup", {
            "booking": order,
            "collections": collections,
            "latest": collections[:1],
            "pending": self._pending_collection(order),
            "form": self._pickup_form(order, post=post),
            "error": error,
            "msg": msg,
            "warehouse_tz": WAREHOUSE_TZ,
            "arrival_local": _utc_to_local_display(order.x_requested_arrival),
            "departure_local": _utc_to_local_display(order.x_requested_departure),
            "page_name": "service_booking",
        })

    @http.route("/my/service-bookings/<int:order_id>/pickup/save", type="http",
                auth="user", website=True, methods=["POST"], csrf=True, sitemap=False)
    def service_booking_pickup_save(self, order_id, **post):
        order = self._own_order(order_id)          # 404 for a foreign booking

        # Validate BEFORE writing, so a rejected save loses nothing the
        # customer typed.
        resolved, error = self._check_pickers(post, require_party=True)
        if error:
            return self._render_pickup(order, error=error, post=post)

        party = resolved["pickup_party_contact_id"]
        recipient = resolved["recipient_contact_id"]

        # Driver details go through the model's portal guard; the party and
        # recipient are partner links and are written elevated, only after the
        # chosen contact was proven to belong to the caller.
        driver_vals = {field: _clean(post.get(field)) for field in DRIVER_FIELDS}
        elevated = dict(party._snapshot_vals())
        elevated["authorized_contact_id"] = party.id
        elevated["outbound_recipient_id"] = recipient.partner_id.id if recipient else False

        pending = self._pending_collection(order)
        if pending:
            pending.write(driver_vals)
            pending.sudo().write(elevated)
            collection = pending
        else:
            collection = request.env["logistics.pickup.collection"].sudo().create(
                dict(driver_vals, sale_order_id=order.id, **elevated))

        return request.redirect(
            "/my/service-bookings/%s/pickup?msg=saved" % order.id)

    # ── proof of pickup ────────────────────────────────────────────────
    def _own_collection(self, collection_id):
        """A COMPLETED collection of the caller's own bookings, or 404."""
        try:
            collection_id = int(collection_id or 0)
        except (TypeError, ValueError):
            collection_id = 0
        collection = request.env["logistics.pickup.collection"].sudo().search([
            ("id", "=", collection_id),
            ("commercial_partner_id", "=", self._commercial_partner().id),
            ("state", "=", "collected"),
        ], limit=1)
        if not collection:
            raise NotFound()
        return collection

    @http.route("/my/pickup-proof/<int:collection_id>", type="http", auth="user",
                website=True, sitemap=False)
    def pickup_proof(self, collection_id, **kwargs):
        collection = self._own_collection(collection_id)
        return request.render("prema_logistics_booking.portal_pickup_proof", {
            "collection": collection,
            "warehouse_tz": WAREHOUSE_TZ,
            "collected_local": _utc_to_local_display(collection.collection_time),
            "page_name": "service_booking",
        })

    @http.route("/my/pickup-proof/<int:collection_id>/pdf", type="http", auth="user",
                website=True, sitemap=False)
    def pickup_proof_pdf(self, collection_id, **kwargs):
        collection = self._own_collection(collection_id)
        report = request.env["ir.actions.report"].sudo()
        pdf, _ = report._render_qweb_pdf(
            "prema_logistics_booking.action_report_pickup_proof",
            res_ids=collection.ids)
        filename = "Proof-of-Pickup-%s.pdf" % collection.name.replace("/", "-")
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", str(len(pdf))),
            ("Content-Disposition", 'attachment; filename="%s"' % filename),
        ])

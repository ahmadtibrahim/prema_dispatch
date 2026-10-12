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
import json
import re
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from odoo import _, fields, http
from odoo.addons.prema_logistics_booking.models.logistics_warehouse_billing import (
    chargeable_storage_days)
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

#: How each service is priced, shown next to its rate so nobody has to guess.
SERVICE_UNITS = {
    "PFL-XD-LTL": _("per pallet (in and out)"),
    "PFL-XD-FTL": _("flat fee per job"),
    "PFL-STOR-DAY": _("per pallet per chargeable day"),
    "PFL-WRAP": _("per pallet"),
    "PFL-LABEL": _("agreed rate"),
    "PFL-LAB-HR": _("per person-hour"),
    "PFL-PAL-SUP": _("per pallet"),
}

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
            "accept_name": _clean(post.get("accept_name")),
            "accept_terms": _clean(post.get("accept_terms")),
            "accept_signature": post.get("accept_signature") or "",
            # Quantities the customer chose, kept through a rejected submission.
            "wrap_qty": _clean(post.get("wrap_qty")),
            "pallet_supply_qty": _clean(post.get("pallet_supply_qty")),
            "labour_persons": _clean(post.get("labour_persons")),
            "labour_hours": _clean(post.get("labour_hours")),
        }

    # ── the estimate ───────────────────────────────────────────────────
    #: which customer input drives each service's quantity, for the live preview
    QTY_SOURCE = {
        "PFL-XD-LTL": "pallets",
        "PFL-XD-FTL": "one",
        "PFL-STOR-DAY": "storage_days_x_pallets",
        "PFL-WRAP": "wrap_qty",
        "PFL-PAL-SUP": "pallet_supply_qty",
        "PFL-LAB-HR": "labour",
        "PFL-LABEL": "one",
    }

    def _rate_table(self):
        """Rates and resolved taxes for every selectable service.

        Sent to the page so the estimate can be recalculated as the customer
        types. It is a convenience only — the server recomputes on submit and
        never trusts what the browser reports.
        """
        partner = self._commercial_partner()
        table = []
        for product in self._services():
            taxes = self._service_taxes(product, partner)
            table.append({
                "code": product.default_code,
                "name": self._customer_name(product),
                "unit": SERVICE_UNITS.get(product.default_code, _("each")),
                "rate": product.list_price,
                "qty_source": self.QTY_SOURCE.get(product.default_code, "one"),
                "tax_pct": sum(taxes.mapped("amount")),
                "tax_label": ", ".join(
                    "%s %s%%" % (t.name, t.amount) for t in taxes) or _("no tax"),
                "estimated": product.default_code == "PFL-STOR-DAY",
            })
        return table

    #: A warehouse booking consumes pallet positions as soon as any of these is
    #: selected — FTL too, because it still occupies four positions.
    PALLET_BASED = ("PFL-XD-LTL", "PFL-XD-FTL", "PFL-WRAP", "PFL-PAL-SUP",
                    "PFL-STOR-DAY")

    @staticmethod
    def _customer_name(product):
        """The product name as a customer should read it: no internal code."""
        label = product.display_name or product.name or ""
        label = re.sub(r"^\[[^\]]+\]\s*", "", label)
        return re.sub(r"^PFL\s+", "", label).strip()

    def _service_options(self):
        """What the form lists, and what the estimate is keyed on."""
        return [{"id": product.id, "code": product.default_code,
                 "label": self._customer_name(product)}
                for product in self._services()]

    @staticmethod
    def _whole(value):
        """A positive whole number, or None when it is not one."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number <= 0 or number != int(number):
            return None
        return int(number)

    def _validate_quantities(self, inputs):
        """The booking form's own rules, applied on the server.

        Returns an error code, or None. The browser runs the same checks; this
        is the one that decides.
        """
        codes = inputs["codes"]
        pallets = inputs["expected_pallets"]
        pallet_based = [c for c in codes if c in self.PALLET_BASED]
        if pallet_based and pallets <= 0:
            return "pallets_required"
        raw = inputs.get("expected_pallets_raw") or ""
        if raw:
            try:
                if float(raw) != int(float(raw)):
                    return "pallets_required"
            except (TypeError, ValueError):
                return "pallets_required"
        if "PFL-WRAP" in codes:
            wrapped = self._whole(inputs["wrap_qty"])
            if wrapped is None:
                return "wrap_invalid"
            if pallets and wrapped > pallets:
                return "wrap_exceeds_pallets"
        if "PFL-PAL-SUP" in codes:
            supplied = self._whole(inputs["pallet_supply_qty"])
            if supplied is None:
                return "supply_invalid"
            if pallets and supplied > pallets:
                return "supply_exceeds_pallets"
        if "PFL-LAB-HR" in codes:
            workers = self._whole(inputs["labour_persons"])
            hours = inputs["labour_hours"]
            if workers is None or hours <= 0:
                return "labour_invalid"
        return None

    def _booking_inputs(self, post):
        """The customer's raw inputs, parsed once.

        The preview and the created order both read their quantities from
        here, which is what keeps the two from drifting apart.
        """
        def number(key):
            try:
                return max(0.0, float(_clean(post.get(key)) or 0))
            except (TypeError, ValueError):
                return 0.0
        raw_pallets = _clean(post.get("expected_pallets"))
        try:
            expected = int(float(raw_pallets or 0))
        except (TypeError, ValueError):
            expected = 0
        return {
            "codes": [c for c in request.httprequest.form.getlist("service_code")
                      if c in SERVICE_CODES],
            "expected_pallets": expected,
            # kept raw so "2.5 pallets" can be refused rather than truncated
            "expected_pallets_raw": raw_pallets,
            "wrap_qty": number("wrap_qty"),
            "pallet_supply_qty": number("pallet_supply_qty"),
            "labour_persons": number("labour_persons"),
            "labour_hours": number("labour_hours"),
            "arrival": _local_input_to_utc(post.get("requested_arrival")),
            "departure": _local_input_to_utc(post.get("requested_departure")),
        }

    def _service_quantity(self, code, inputs):
        """How much of one service the customer is asking for.

        One rule per service, used by the preview and by the order alike.
        """
        if code == FTL_CODE:
            return 1.0                      # one flat fee per job, whatever the pallets
        if code == LTL_CODE:
            return float(inputs["expected_pallets"] or 0)   # per pallet handled
        if code == "PFL-WRAP":
            return inputs["wrap_qty"]                       # only what was asked for
        if code == "PFL-PAL-SUP":
            return inputs["pallet_supply_qty"]
        if code == "PFL-LAB-HR":
            hours = inputs["labour_hours"]
            persons = inputs["labour_persons"]
            if not (hours or persons):
                return 0.0
            # one hour per person is the minimum, so a person booked for less
            # than an hour still bills an hour
            return max(hours, persons)
        if code == "PFL-STOR-DAY":
            arrival, departure = inputs["arrival"], inputs["departure"]
            if not (arrival and departure) or departure <= arrival:
                return 0.0
            days = chargeable_storage_days(
                (departure - arrival).total_seconds() / 3600.0)
            return days * float(inputs["expected_pallets"] or 0)
        return 1.0

    def _service_taxes(self, product, partner):
        """The taxes that will really apply — resolved the way a sale order
        line resolves them, by company and fiscal position.

        Summing `product.taxes_id` by hand is what applied two 13% HST taxes:
        these products are shared across companies and legitimately carry one
        13% HST per company. Odoo applies only the current company's, mapped
        through the customer's fiscal position.
        """
        company = request.env.company
        taxes = product.taxes_id.filtered(
            lambda t: not t.company_id or t.company_id == company)
        position = request.env["account.fiscal.position"].sudo()._get_fiscal_position(
            partner)
        if position:
            taxes = position.map_tax(
                taxes, product=product.product_variant_id, partner=partner)
        return taxes.filtered(lambda t: t.company_id == company or not t.company_id)

    def _quote_summary(self, inputs):
        """What the customer is about to accept: the services they selected,
        their quantities, the applicable rates and taxes, and the terms.

        Nothing is computed for a service that was not selected.
        """
        partner = self._commercial_partner()
        currency = request.env.company.currency_id
        rows, untaxed, taxed, needs_rate = [], 0.0, 0.0, []
        if inputs["codes"]:
            products = request.env["product.template"].sudo().search(
                [("default_code", "in", inputs["codes"])], order="default_code")
            for product in products:
                code = product.default_code
                quantity = self._service_quantity(code, inputs)
                rate = product.list_price
                taxes = self._service_taxes(product, partner)
                computed = taxes.compute_all(
                    rate, currency=currency, quantity=quantity or 0.0,
                    product=product.product_variant_id, partner=partner)
                line_untaxed = computed["total_excluded"]
                line_total = computed["total_included"]
                if rate == 0:
                    needs_rate.append(self._customer_name(product))
                untaxed += line_untaxed
                taxed += line_total - line_untaxed
                rows.append({
                    "code": code,
                    "name": self._customer_name(product),
                    "description": product.description_sale or "",
                    "unit": SERVICE_UNITS.get(code, _("each")),
                    "quantity": quantity,
                    "rate": rate,
                    "tax_label": ", ".join(
                        "%s %s%%" % (t.name, t.amount) for t in taxes
                    ) or _("no tax"),
                    "subtotal": line_untaxed,
                    "tax": line_total - line_untaxed,
                    "total": line_total,
                    "estimated": code == "PFL-STOR-DAY",
                })
        template = request.env["sale.order.template"].sudo().search(
            [("name", "=", TEMPLATE_NAME)], limit=1)
        return {
            "rows": rows,
            "has_selection": bool(inputs["codes"]),
            "untaxed": untaxed,
            "tax": taxed,
            "total": untaxed + taxed,
            "currency": currency,
            "terms": template.note or "",
            "needs_rate": needs_rate,
        }

    def _preview_rows(self, post):
        """The rows the customer actually saw, sent back with the form.

        Used only to notice that the estimate moved under them — never as a
        source of truth for money.
        """
        raw = post.get("estimate_preview")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _previews_agree(shown, computed):
        """Same services, same quantities, same money to the cent."""
        if shown is None:
            return True                    # nothing was displayed to disagree with
        if len(shown) != len(computed):
            return False
        for left, right in zip(sorted(shown, key=lambda r: r.get("code", "")),
                               sorted(computed, key=lambda r: r["code"])):
            if left.get("code") != right["code"]:
                return False
            for key in ("quantity", "rate", "subtotal", "tax", "total"):
                if abs(float(left.get(key) or 0) - float(right[key])) > 0.01:
                    return False
        return True

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
        form = self._booking_form()
        form["summary"] = self._quote_summary(self._booking_inputs({}))
        return request.render("prema_logistics_booking.portal_my_service_booking_new", {
            "services": self._services(),
            "service_options": self._service_options(),
            "ltl_code": LTL_CODE,
            "ftl_code": FTL_CODE,
            "warehouse_tz": WAREHOUSE_TZ,
            "page_name": "service_bookings",
            "error": kwargs.get("error"),
            "form": form,
            "rate_table": self._rate_table(),
            "rate_table_json": json.dumps(self._rate_table()),
        })

    @http.route("/my/service-bookings/submit", type="http", auth="user",
                website=True, methods=["POST"], csrf=True, sitemap=False)
    def service_booking_submit(self, **post):
        partner = self._commercial_partner()

        def reject(code):
            form = self._booking_form(post)
            form["summary"] = self._quote_summary(self._booking_inputs(post))
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "service_options": self._service_options(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": code,
                    "form": form,
                    "rate_table": self._rate_table(),
            "rate_table_json": json.dumps(self._rate_table()),
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

        inputs = self._booking_inputs(post)
        expected = inputs["expected_pallets"]
        client_ref = _clean(post.get("client_order_ref"))
        notes = _clean(post.get("notes"))
        arrival = inputs["arrival"]
        departure = inputs["departure"]
        summary = self._quote_summary(inputs)

        quantity_error = self._validate_quantities(inputs)
        if quantity_error:
            return reject(quantity_error)

        # Acceptance is captured here, not in a second journey: the customer
        # sees the services, quantities, rates, taxes and terms, and signs once.
        accepted_name = _clean(post.get("accept_name"))
        signature = post.get("accept_signature") or ""
        accepted = (_clean(post.get("accept_terms")) == "1"
                    and bool(accepted_name) and signature.startswith("data:image"))
        if not accepted:
            form = self._booking_form(post)
            form["summary"] = summary
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "service_options": self._service_options(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": "acceptance_required",
                    "form": form,
                    "rate_table": self._rate_table(),
            "rate_table_json": json.dumps(self._rate_table()),
                })

        # The customer signed a specific estimate. If what we now compute is
        # not what they were shown, that signature no longer covers it: send
        # them back to review and sign again rather than booking silently.
        if not self._previews_agree(self._preview_rows(post), summary["rows"]):
            form = self._booking_form(post)
            form["summary"] = summary
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "service_options": self._service_options(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": "estimate_changed",
                    "form": form,
                    "rate_table": self._rate_table(),
            "rate_table_json": json.dumps(self._rate_table()),
                })

        # A service with no agreed rate cannot be silently accepted at zero.
        needs_quotation = bool(summary["needs_rate"])

        # Space is promised BEFORE the order exists. The capacity report takes
        # the position locks, so what it reports free stays free for the
        # reservation this same transaction writes a moment later. A refusal
        # here leaves no order and no stranded reservation behind.
        if expected > 0 and not (arrival and departure):
            return reject("appointment_required")
        capacity = None
        if expected > 0 and not needs_quotation:
            capacity = request.env[
                "logistics.position.reservation"].sudo().pfl_capacity_report(
                    arrival, departure, expected)
        if capacity and not capacity["ok"]:
            form = self._booking_form(post)
            form["capacity_message"] = capacity["message"]
            form["summary"] = summary
            return request.render(
                "prema_logistics_booking.portal_my_service_booking_new", {
                    "services": self._services(),
                    "service_options": self._service_options(),
                    "ltl_code": LTL_CODE,
                    "ftl_code": FTL_CODE,
                    "warehouse_tz": WAREHOUSE_TZ,
                    "page_name": "service_bookings",
                    "error": "capacity",
                    "form": form,
                    "rate_table": self._rate_table(),
            "rate_table_json": json.dumps(self._rate_table()),
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

        # Only the selected services become lines, at the quantities the
        # estimate showed — the same rule, so the two cannot disagree.
        lines = []
        for code in codes:
            template_product = request.env["product.template"].sudo().search(
                [("default_code", "=", code)], limit=1)
            if not template_product or not template_product.product_variant_id:
                continue
            if code == "PFL-STOR-DAY":
                # An intentional zero-quantity provision: storage is billed from
                # the pallets' actual receipt and release, never from the
                # estimate.
                quantity = 0.0
            else:
                quantity = self._service_quantity(code, inputs)
            lines.append((0, 0, {
                "product_id": template_product.product_variant_id.id,
                "product_uom_qty": quantity,
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
        if expected > 0 and not needs_quotation:
            order.action_pfl_reserve_space()

        # Record the acceptance on the commercial record — the evidence, and
        # the rates and terms exactly as they were when the customer agreed.
        snapshot = "\n".join(
            "%s — %s x %s %s" % (row["name"], row["quantity"], row["rate"],
                                 row["tax_label"])
            for row in summary["rows"])
        snapshot += "\n\n" + _("Estimated total: %(untaxed)s + %(tax)s tax = %(total)s") % {
            "untaxed": summary["untaxed"], "tax": summary["tax"],
            "total": summary["total"]}
        try:
            signature_b64 = signature.split(",", 1)[1]
        except IndexError:
            signature_b64 = False
        order.write({
            "x_pfl_accepted_at": fields.Datetime.now(),
            "x_pfl_accepted_by_name": accepted_name,
            "x_pfl_acceptance_signature": signature_b64,
            "x_pfl_accepted_terms": snapshot + "\n\n" + (summary["terms"] or ""),
            "x_pfl_acceptance_state": (
                "subject_to_quotation" if needs_quotation else "accepted"),
        })
        order.message_post(body=_(
            "Quotation%(suffix)s accepted by %(name)s in the portal and signed."
            "<br/>%(terms)s",
            suffix=(
                _(" (subject to quotation for: %s)")
                % ", ".join(summary["needs_rate"])) if needs_quotation else "",
            name=accepted_name,
            terms=snapshot.replace("\n", "<br/>")))

        return request.redirect("/my/service-bookings?msg=%s" % (
            "quotation" if needs_quotation else "created"))

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

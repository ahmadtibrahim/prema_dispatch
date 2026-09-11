# -*- coding: utf-8 -*-
"""CRM → Sales Quotation — the ONE commercial quotation workflow.

The `sale.order` in draft/sent state IS the customer-facing quotation. The
CRM opportunity is its origin and its communication home. This bridge is the
only place an opportunity turns into a quotation, through two deliberate
staff actions:

    AI Rate Quote     — ask the dispatch pricing engine for the number, then
                        build the quotation around it.
    Create Quotation  — build the quotation from the customer's own facts and
                        let the salesperson price it.

Both land on the same object, linked through the NATIVE `opportunity_id`
relation, so there is exactly one quotation authority per opportunity:

    CRM opportunity → quotation → review/edit → Send by Email → the customer
    replies on the opportunity → revise the same quotation → accepted →
    Confirm → Sales Order → Book Load.

Neither action creates a `logistics.custom.quote`. The Rate Confirmation was
a *pre-quotation* document; issuing one alongside the quotation produced two
parallel quote authorities for one shipment, which is the duplication this
module exists to end.

Pricing still comes from the canonical dispatch engine — the same
`BookingOrchestrationService` call the estimate reply and the phone wizard
use — so a "manual" figure is never invented here, and a quotation created
without engine pricing says so plainly instead of implying a price it never
computed.
"""

from odoo import _, models
from odoo.exceptions import UserError

from ..services.lead_quote_draft_service import LeadQuoteDraftService

# Book Load mapping reads the freight line's description through
# `_service_note` / `_route_from` / `_load_figures` / `_commodity`. The
# summary below is written in that exact grammar on purpose — see
# `_quotation_summary`.
_ICP_PRODUCT_KEYS = {
    ("CA", True): "logistics.product_ca_reefer_ltl_id",
    ("CA", False): "logistics.product_ca_dry_ltl_id",
    ("US", True): "logistics.product_us_reefer_ltl_id",
    ("US", False): "logistics.product_us_dry_ltl_id",
}
_ICP_FTL_KEYS = {
    ("CA", True): "logistics.product_ca_reefer_ftl_id",
    ("CA", False): "logistics.product_ca_dry_ftl_id",
    ("US", True): "logistics.product_us_reefer_ftl_id",
    ("US", False): "logistics.product_us_dry_ftl_id",
}


class CrmLeadQuotationBridge(models.Model):
    _inherit = "crm.lead"

    # ── §2: the two quotation entry points ─────────────────────────────

    def action_ai_rate_quote(self):
        """Price this shipment through the engine, then quote it.

        Creates (or reopens) the opportunity's Sales quotation with the
        engine's own figure on the freight line. Refuses BEFORE writing
        anything when the customer never stated an input the price depends
        on — a reefer setpoint or an LTL pallet count — because a quotation
        carrying an invented figure is worse than no quotation.

        Nothing is sent, confirmed or booked: the quotation is a draft the
        salesperson reviews and edits before Send by Email.
        """
        self.ensure_one()
        self._require_customer()
        existing = self._open_quotation()
        if existing:
            return self._reopen(existing)

        companion = LeadQuoteDraftService(self.env)
        facts = companion.extract_effective_facts(self)
        # Validates BOTH sides and raises BEFORE anything is written when a
        # side is city-only or not priceable (§6 rules — shared with the
        # estimate-reply path so the two never disagree about what is
        # quotable).
        stops = companion.resolve_stops(self, facts, require_priceable=True)
        shipment = companion.shipment_values(facts)
        shipment["facts"] = facts
        self._validate_quotable(shipment)

        quote = companion.canonical_quote(
            self, companion.estimate_request_values(self, stops, shipment))
        amount = quote["calculated_price"]
        price_reference = companion.price_reference_from_quote(quote)

        order = self._create_quotation_order(
            stops, shipment, amount, price_reference)
        # The provenance goes on BOTH records: the quotation is the
        # commercial document, the opportunity is where the conversation
        # lives, and a reviewer reading either one must be able to see where
        # the number came from without opening the other.
        order.message_post(
            body=_(
                "Quotation priced by the dispatch engine: %s — %s.") % (
                format(amount, ",.2f"), price_reference),
            subtype_xmlid="mail.mt_note",
        )
        self.message_post(
            body=_(
                "Quotation %s created for %s (amount %s — %s). Review it, "
                "then send it from the quotation; the customer's reply will "
                "land back on this opportunity.") % (
                order.name,
                self.partner_id.display_name,
                format(amount, ",.2f"),
                price_reference),
            subtype_xmlid="mail.mt_note",
        )
        return self._quotation_open_action(order)

    def action_create_quotation(self):
        """Create (or reopen) the opportunity's quotation, unpriced.

        The deliberate "I will price this myself" path. It writes the
        customer's own shipment facts into the quotation in the shape Book
        Load reads, attaches the lane's freight product, and leaves the
        price at zero for the salesperson to fill in.

        It is NEVER blocked by the pricing policy: no engine call, no
        setpoint/pallet requirement, no Stop resolution, no side effects.
        The refusals that guard `action_ai_rate_quote` exist because the
        engine cannot price without those inputs; a human quoting by hand
        needs none of them, and demanding them here is what made the old
        draft path unusable.
        """
        self.ensure_one()
        self._require_customer()
        existing = self._open_quotation()
        if existing:
            return self._reopen(existing)

        companion = LeadQuoteDraftService(self.env)
        facts = companion.extract_effective_facts(self)
        shipment = companion.shipment_values(facts)
        # Read the customer's stated stops straight off the facts: no Saved
        # Location lookup, no Pending Review facility, no Google call. A
        # blank quotation is a paperwork step, not a pricing run.
        stops = companion.fact_stops(facts)

        order = self._create_quotation_order(
            stops, shipment, amount=None, price_reference="")
        order.message_post(
            body=_(
                "Quotation created from the CRM opportunity without pricing. "
                "The shipment facts above are the customer's own; set the "
                "freight price before sending."),
            subtype_xmlid="mail.mt_note",
        )
        self.message_post(
            body=_(
                "Quotation %s created for %s (unpriced). Add the freight "
                "price on the quotation, then send it from there; the "
                "customer's reply will land back on this opportunity.") % (
                order.name, self.partner_id.display_name),
            subtype_xmlid="mail.mt_note",
        )
        return self._quotation_open_action(order)

    # ── quotation lookup / window ──────────────────────────────────────

    def _require_customer(self):
        if not self.partner_id:
            raise UserError(_(
                "Select or create the Customer on this opportunity before "
                "creating a quotation."))

    def _reopen(self, order):
        """Surface the quotation that already exists — never fork a second."""
        self.ensure_one()
        self.message_post(
            body=_(
                "Quotation %s is already open for this opportunity — "
                "reopened. Nothing was re-priced or duplicated; edit the "
                "quotation and send it, or revise it in place.") % order.name,
            subtype_xmlid="mail.mt_note",
        )
        return self._quotation_open_action(order)

    def _open_quotation(self):
        """The opportunity's current open quotation, newest first.

        Only draft/sent count: a cancelled order is history, and a new
        quotation after one of those is a genuinely new commercial act, not
        a duplicate."""
        self.ensure_one()
        if "opportunity_id" not in self.env["sale.order"]._fields:
            return self.env["sale.order"]
        return self.env["sale.order"].sudo().search([
            ("opportunity_id", "=", self.id),
            ("state", "in", ("draft", "sent")),
        ], order="id desc", limit=1)

    def _quotation_open_action(self, order):
        """One proper window action opening the quotation itself."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Quotation"),
            "res_model": "sale.order",
            "view_mode": "form",
            "res_id": order.id,
            "context": {"default_opportunity_id": self.id},
        }

    # ── guards (engine-priced path only) ───────────────────────────────

    def _validate_quotable(self, shipment):
        """Refuse an unquotable shipment BEFORE anything is written.

        Same two refusals the estimate reply makes, for the same reason: a
        reefer setpoint and an LTL pallet count are inputs to the price, so
        a quotation without them would carry an invented figure."""
        if shipment["temperature_mode"] == "reefer" \
                and not shipment["temperature_setpoint_stated"]:
            raise UserError(_(
                "Reefer shipment, but the customer never stated a numerical "
                "setpoint ('frozen' or 'chilled' alone is not a setpoint). "
                "Confirm the setpoint with the customer, then retry — "
                "nothing was priced or created."))
        if shipment["load_type"] != "ftl" and not shipment["pallets_stated"]:
            raise UserError(_(
                "The customer never stated how many pallets this LTL "
                "shipment is, and the LTL price is quoted per pallet. "
                "Confirm the pallet count with the customer (and state it "
                "on the opportunity), then retry — nothing was priced or "
                "created."))

    # ── the quotation record ───────────────────────────────────────────

    def _freight_product(self, stops, shipment):
        """The EXISTING freight product for this lane (§5).

        Read from the same `logistics.product_*` parameters the booking and
        invoice sides already use, so a quotation, its booking and its
        invoice can never name three different freight products for one
        shipment. No product is ever created here."""
        self.ensure_one()
        country = "CA"
        postal = (stops.get("pickup") or {}).get("postal_code") or ""
        # Canadian FSA letters never include D,F,I,O,Q,U and the first
        # letter is a letter; a US ZIP is 5 digits. Postal evidence beats a
        # guess, and an unknown postal keeps the Canadian default — the
        # same convention `_select_freight_product` uses.
        if postal and postal[:1].isdigit():
            country = "US"
        is_reefer = shipment["temperature_mode"] == "reefer"
        key = (_ICP_FTL_KEYS if shipment["load_type"] == "ftl"
               else _ICP_PRODUCT_KEYS).get((country, is_reefer))
        ICP = self.env["ir.config_parameter"].sudo()
        product_id = int(ICP.get_param(key, "0") or "0") if key else 0
        product = self.env["product.product"].sudo().browse(product_id)
        return product if product.exists() and product.active else \
            self.env["product.product"]

    def _quotation_summary(self, stops, shipment, amount, price_reference):
        r"""The operational summary line, in the exact labelled grammar
        BookLoadMappingService parses.

        `Route: A → B`, `Load: N pallets / W lbs`, `Commodity: C` — the
        slash form is what `_LOAD_RE` matches (`Load:\s*N pallets /\s*W
        lbs`); the prose form the AI writer uses ("N pallets, W lbs") does
        NOT match it. Writing the parseable form is what lets Book Load
        pre-fill a quotation that no AI ever read (§23), and it costs
        nothing: the same facts are legible to a human."""
        pickup = stops.get("pickup") or {}
        delivery = stops.get("delivery") or {}
        parts = []
        route = " → ".join(part for part in (
            (pickup.get("city") or "").strip(),
            (delivery.get("city") or "").strip(),
        ) if part)
        if route:
            parts.append("Route: %s" % route)
        metrics = []
        if shipment["pallets"]:
            metrics.append("%s pallets" % shipment["pallets"])
        if shipment["weight_lbs"]:
            metrics.append("%s lbs" % format(shipment["weight_lbs"], ",.0f"))
        load = "Load: %s" % " / ".join(metrics) if len(metrics) == 2 else ""
        if not load and metrics:
            load = "Load: %s" % metrics[0]
        if load:
            parts.append(load)
        if shipment["commodity"]:
            parts.append("Commodity: %s" % shipment["commodity"])
        if shipment["temperature_mode"] == "reefer" \
                and shipment.get("required_temperature_c") is not None:
            parts.append("Temperature: %s°C" % format(
                shipment["required_temperature_c"], "g"))
        schedule = self._pickup_schedule_line(shipment)
        if schedule:
            parts.append(schedule)
        # An unpriced quotation says so in words. A bare "Rate: 0.00" would
        # read as a real price of zero — and would be copied into the
        # booking and the invoice as one.
        if amount is None:
            parts.append("Rate: to be quoted")
        else:
            parts.append("Rate: %s" % format(amount, ",.2f"))
            if price_reference:
                parts.append("Priced by the dispatch engine — %s"
                             % price_reference)
        return "\n".join(part for part in parts if part)

    def _pickup_schedule_line(self, shipment):
        """The customer's requested pickup date, in Book Load's own grammar.

        Without this the quotation carries no requested date, so Book Load
        falls back to `date_order` — the moment the quotation happened to be
        confirmed — and the dispatcher books whatever day the paperwork was
        typed on. The customer asked for a specific day; that day is what the
        booking must offer them (Tier 1 refuses to serve any other silently).

        The channel is the summary note on purpose. It is the ONE place
        Book Load already reads a schedule from (`_pickup_from` prefers it
        over the order date), and a note line is legible to the human
        reviewing the quotation, so the date has a single source instead of
        a note and a `commitment_date` that can drift apart.

        Only the DATE is written, at the 8 AM local convention the mapper
        and the Book Load wizard already default to: the customer states a
        day ("every Friday"), never a dock appointment, and inventing an
        hour they did not ask for would read as a commitment."""
        day = shipment.get("requested_pickup_date")
        if not day:
            return ""
        return "Date: %s\nPickup: 08:00 AM" % day.strftime("%B %d, %Y")

    def _create_quotation_order(self, stops, shipment, amount,
                                price_reference):
        """Build the draft `sale.order` — the commercial quotation.

        `amount` is None for the unpriced path: the freight line is created
        with the lane's product so the salesperson only has to type a
        figure, and the summary says "to be quoted" rather than 0.00.
        """
        self.ensure_one()
        Order = self.env["sale.order"].sudo()
        order_vals = {
            "partner_id": self.partner_id.id,
            "origin": self.name or False,
        }
        # §3: the NATIVE sale_crm relation is the only CRM link — never a
        # second custom field pointing at the same opportunity.
        if "opportunity_id" in Order._fields:
            order_vals["opportunity_id"] = self.id
        order = Order.create(order_vals)

        product = self._freight_product(stops, shipment)
        line_vals = {
            "order_id": order.id,
            "name": product.display_name if product else _("Freight Service"),
            "product_uom_qty": 1,
            "price_unit": amount or 0.0,
        }
        if product:
            line_vals["product_id"] = product.id
            if "product_uom" in self.env["sale.order.line"]._fields \
                    and product.uom_id:
                line_vals["product_uom"] = product.uom_id.id
        self.env["sale.order.line"].sudo().create(line_vals)

        # The operational summary rides on a note line, exactly where the
        # AI Generate flow puts it — Book Load scans every line for it.
        self.env["sale.order.line"].sudo().create({
            "order_id": order.id,
            "display_type": "line_note",
            "name": self._quotation_summary(
                stops, shipment, amount, price_reference),
            "sequence": 99,
        })
        return order

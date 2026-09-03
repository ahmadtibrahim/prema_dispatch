import datetime
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SHIPMENT_TYPES = [("ltl", "LTL"), ("ftl", "FTL")]
TEMP_MODES = [("dry", "Dry"), ("reefer", "Reefer")]


class LogisticsPhoneBooking(models.TransientModel):
    """Staff phone-quote wizard backed by the canonical booking engine.

    This wizard deliberately does not create new Saved Location / Master
    Facility records while a caller is only asking for a price. Staff may
    reuse an existing canonical facility or type a one-off address. The
    actual booking is created only after the quote is accepted, through the
    same BookingOrchestrationService used by the customer portal.
    """

    _name = "logistics.phone.booking"
    _description = "Phone Booking Wizard"

    # Customer
    partner_id = fields.Many2one("res.partner", string="Customer", required=True)
    crm_lead_id = fields.Many2one(
        "crm.lead",
        string="CRM Opportunity",
        readonly=True,
        help="Opportunity that opened this draft rate calculation.",
    )
    source_text = fields.Text(
        string="Customer Freight Request",
        help="Customer-supplied CRM text. AI may extract shipment facts from "
             "this text, but it never sets the customer price.",
    )
    extraction_summary = fields.Text(
        string="Extraction Review",
        readonly=True,
    )

    # Optional reuse of the canonical physical-location database. These are
    # read/select only in the phone flow; choosing one never creates a new
    # customer-location relationship.
    pickup_location_id = fields.Many2one(
        "prema.dispatch.location",
        string="Existing Pickup Location",
        domain="[('active', '=', True), ('stop_type', 'in', ['pickup', 'both'])]",
        help="Optional. Reuse a verified facility already in the Prema Dispatch location database.",
    )
    delivery_location_id = fields.Many2one(
        "prema.dispatch.location",
        string="Existing Delivery Location",
        domain="[('active', '=', True), ('stop_type', 'in', ['delivery', 'both'])]",
        help="Optional. Reuse a verified facility already in the Prema Dispatch location database.",
    )

    # Shipment / one-off address fields. If no existing location is chosen,
    # these stay quote-only data and are not persisted as locations.
    pickup_postal_code = fields.Char(string="Pickup Postal Code")
    pickup_address = fields.Char(string="Pickup Address")
    pickup_company_name = fields.Char(string="Pickup Company")
    pickup_instructions = fields.Text(string="Pickup Instructions")
    delivery_postal_code = fields.Char(string="Delivery Postal Code")
    delivery_address = fields.Char(string="Delivery Address")
    delivery_company_name = fields.Char(string="Delivery Company")
    delivery_instructions = fields.Text(string="Delivery Instructions")

    requested_pickup_date = fields.Date(
        string="Requested Pickup Date",
        help="Optional. Leave blank to price the next compatible scheduled departure. "
             "An unserved date is refused (never silently moved) with the nearest "
             "valid pickup dates listed below.",
    )
    available_pickup_dates = fields.Char(
        string="Available Pickup Dates",
        readonly=True,
        help="Nearest valid pickup dates for this request, from the same "
             "availability authority the customer portal calendar uses.",
    )
    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float(string="Weight (lbs)", default=750.0)
    temperature_mode = fields.Selection(TEMP_MODES, default="reefer", required=True)
    required_temperature_c = fields.Float(
        string="Required Temperature",
        default=15.0,
        help="Required for Reefer bookings. 0°C is a valid value.",
    )
    submitted_temperature_unit = fields.Selection(
        [("c", "°C"), ("f", "°F")], string="Unit", default="c",
        help="Unit the dispatcher typed the temperature in. Storage is "
             "canonical Celsius — the value is converted once at intake.")
    temperature_display_dual = fields.Char(
        string="Temperature", compute="_compute_temperature_display_dual")

    @api.depends("required_temperature_c", "temperature_mode",
                 "submitted_temperature_unit")
    def _compute_temperature_display_dual(self):
        from ..services.temperature_service import parse_temperature, format_dual
        for rec in self:
            if rec.temperature_mode != "reefer":
                rec.temperature_display_dual = ""
                continue
            celsius = parse_temperature(
                rec.required_temperature_c,
                unit=rec.submitted_temperature_unit or "c")
            rec.temperature_display_dual = (
                format_dual(celsius) if celsius is not None else "")

    def _canonical_required_temperature(self):
        """Convert the wizard's typed value to canonical Celsius (or None
        for non-reefer). 0°C survives as 0.0."""
        if self.temperature_mode != "reefer":
            return None
        if not self.temperature_confirmed:
            raise UserError(_(
                "Confirm the numerical reefer setpoint before calculating "
                "a price. 'Frozen' or 'chilled' alone is not a setpoint."
            ))
        from ..services.temperature_service import parse_temperature
        return parse_temperature(
            self.required_temperature_c,
            unit=self.submitted_temperature_unit or "c")
    # Kept for compatibility with existing transient rows/views from older
    # versions. The phone flow no longer needs a separate confirmation box;
    # the canonical service validates the numeric temperature itself.
    temperature_confirmed = fields.Boolean(
        string="Numerical Setpoint Confirmed",
        default=False,
        help="Required for reefer pricing. Select only after the customer or "
             "authorized staff confirms the numerical setpoint.",
    )
    shipment_type = fields.Selection(SHIPMENT_TYPES, default="ltl", required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean(string="Same-Day Requested")

    # Results / persistent quotation link
    result_text = fields.Text(readonly=True)
    price = fields.Float(readonly=True)
    # ── Manual / negotiated customer sell price ──────────────────────
    # System Calculated Price (readonly audit) + editable Customer Quoted
    # Price. Any positive amount is allowed (discounts AND increases);
    # Manual Price Reason is required whenever the quoted price differs
    # from the system price. Reset restores the system price.
    system_calculated_price = fields.Float(
        string="System Calculated Price", readonly=True,
        help="Pricing-engine price for this shipment. Audit reference — "
             "never shown to the customer.")
    customer_quoted_price = fields.Float(
        string="Customer Quoted Price",
        help="Final price offered to the customer — the revenue authority. "
             "Any positive amount is allowed (discounts AND increases).")
    manual_price_reason = fields.Char(string="Manual Price Reason")
    manual_price_adjustment = fields.Float(
        string="Adjustment", readonly=True,
        compute="_compute_manual_price_adjustment")
    pickup_date = fields.Date(readonly=True)
    delivery_date = fields.Date(readonly=True)
    quote_token = fields.Char(readonly=True, copy=False)
    quote_id = fields.Many2one(
        "logistics.custom.quote",
        string="Quotation",
        readonly=True,
        copy=False,
        help="Persistent quotation created from the canonical price result.",
    )
    booking_id = fields.Many2one("logistics.booking", readonly=True, string="Created Booking")

    # ── Existing facility reuse ──────────────────────────────────────

    def _customer_access_for(self, location):
        self.ensure_one()
        if not location or not self.partner_id:
            return self.env["logistics.location.customer.access"].browse()
        return self.env["logistics.location.customer.access"].sudo().search([
            ("facility_id", "=", location.id),
            ("commercial_partner_id", "=", self.partner_id.commercial_partner_id.id),
            ("active", "=", True),
        ], limit=1)

    def _location_payload(self, location, stop_type):
        """Physical master + only this customer's private metadata.

        (The legacy logistics.saved.location lookup was retired in
        18.0.13.25.0 — access rows are the only customer-profile source.)
        """
        self.ensure_one()
        if not location:
            return {}
        access = self._customer_access_for(location)
        instructions = (
            (access.pickup_instructions if access else "")
            if stop_type == "pickup" else
            (access.delivery_instructions if access else "")
        )
        return {
            "company_name": location.business_name or location.name or "",
            "formatted_address": location.address_formatted or location.address or "",
            "street": location.street or location.address or "",
            "city": location.city or "",
            "postal_code": location.postal_code or "",
            "latitude": location.pin_lat or 0.0,
            "longitude": location.pin_lng or 0.0,
            "instructions": instructions,
            "contact_name": (access.contact_name if access else "") or "",
            "phone": (access.contact_phone if access else "") or "",
            "email": (access.contact_email if access else "") or "",
            "dispatch_location_id": location.id,
            # Canonical refs (SAVED LOCATION CONSOLIDATION §14): the
            # orchestrator's _stop_refs reads facility_id/customer_access_id
            # for the session + stop records — never a legacy create.
            "facility_id": location.id,
            "customer_access_id": access.id if access else False,
        }

    @api.onchange("pickup_location_id")
    def _onchange_pickup_location_id(self):
        for rec in self:
            if not rec.pickup_location_id:
                continue
            vals = rec._location_payload(rec.pickup_location_id, "pickup")
            rec.pickup_postal_code = vals.get("postal_code") or rec.pickup_postal_code
            rec.pickup_address = vals.get("formatted_address") or rec.pickup_address
            rec.pickup_instructions = vals.get("instructions") or False

    @api.onchange("delivery_location_id")
    def _onchange_delivery_location_id(self):
        for rec in self:
            if not rec.delivery_location_id:
                continue
            vals = rec._location_payload(rec.delivery_location_id, "delivery")
            rec.delivery_postal_code = vals.get("postal_code") or rec.delivery_postal_code
            rec.delivery_address = vals.get("formatted_address") or rec.delivery_address
            rec.delivery_instructions = vals.get("instructions") or False

    @api.onchange("partner_id")
    def _onchange_partner_location_metadata(self):
        """Refresh only the selected customer's private instructions/contact."""
        for rec in self:
            if rec.pickup_location_id:
                vals = rec._location_payload(rec.pickup_location_id, "pickup")
                rec.pickup_instructions = vals.get("instructions") or False
            if rec.delivery_location_id:
                vals = rec._location_payload(rec.delivery_location_id, "delivery")
                rec.delivery_instructions = vals.get("instructions") or False

    @api.onchange(
        "partner_id", "pickup_location_id", "pickup_postal_code", "pickup_address",
        "delivery_location_id", "delivery_postal_code", "delivery_address",
        "requested_pickup_date", "pallets", "weight_lbs", "temperature_mode",
        "required_temperature_c", "shipment_type", "liftgate_pickup",
        "liftgate_delivery", "appointment", "residential", "same_day_requested",
    )
    def _onchange_quote_inputs(self):
        """A displayed price is valid only for the unchanged request."""
        if not self.booking_id:
            self.quote_token = False
            self.price = 0.0
            self.pickup_date = False
            self.delivery_date = False
            self.result_text = False
            self.available_pickup_dates = False

    # ── Canonical pricing request ────────────────────────────────────

    def _stop_values(self, stop_type):
        self.ensure_one()
        is_pickup = stop_type == "pickup"
        location = self.pickup_location_id if is_pickup else self.delivery_location_id
        payload = self._location_payload(location, stop_type) if location else {}
        address = self.pickup_address if is_pickup else self.delivery_address
        postal = self.pickup_postal_code if is_pickup else self.delivery_postal_code
        instructions = self.pickup_instructions if is_pickup else self.delivery_instructions
        payload.update({
            "company_name": payload.get("company_name") or (
                self.pickup_company_name if is_pickup
                else self.delivery_company_name
            ) or "",
            "formatted_address": payload.get("formatted_address") or address or "",
            "street": payload.get("street") or address or "",
            "postal_code": payload.get("postal_code") or postal or "",
            "instructions": instructions or payload.get("instructions") or "",
            "pallet_count": self.pallets,
            "pallets": self.pallets,
            "weight_lb": self.weight_lbs,
            "weight_lbs": self.weight_lbs,
            "liftgate_required": self.liftgate_pickup if is_pickup else self.liftgate_delivery,
        })
        return payload

    def _normalized_request(self, service):
        self.ensure_one()
        return service.normalize_request({
            "partner_id": self.partner_id.id,
            "source_model": self._name,
            "source_res_id": self.id,
            "pickup_stops": [self._stop_values("pickup")],
            "delivery_stops": [self._stop_values("delivery")],
            "pallets": self.pallets,
            "physical_pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "load_type": self.shipment_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": self._canonical_required_temperature(),
            "submitted_temperature_unit": self.submitted_temperature_unit
                or "c",
            "requested_pickup_date": self.requested_pickup_date or None,
            "pricing_method": "corridor",
            "transfer_allowed": self.shipment_type != "ftl",
            "liftgate_pickup": self.liftgate_pickup,
            "liftgate_delivery": self.liftgate_delivery,
            "appointment": self.appointment,
            "residential": self.residential,
            "same_day_requested": self.same_day_requested,
            "idempotency_key": f"phone:{self.id}",
        }, source_channel="phone")

    def _validate_quote_inputs(self):
        """Require pricing inputs only when pricing, not before extraction."""
        self.ensure_one()
        missing = []
        if not self.pickup_location_id and not (self.pickup_postal_code or "").strip():
            missing.append(_("Pickup Postal Code"))
        if not self.delivery_location_id and not (self.delivery_postal_code or "").strip():
            missing.append(_("Delivery Postal Code"))
        if missing:
            raise UserError(_(
                "Complete these fields before selecting Get Price: %s"
            ) % ", ".join(missing))
        self._canonical_required_temperature()

    @staticmethod
    def _postal_from_address(address):
        match = re.search(
            r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVWXYZ])\s*"
            r"(\d[ABCEGHJ-NPRSTVWXYZ]\d)\b",
            address or "",
            re.IGNORECASE,
        )
        return (
            "%s %s" % (match.group(1), match.group(2))
        ).upper() if match else ""

    def _match_saved_location(self, address, postal_code):
        """Return only a confident canonical-location match; create nothing."""
        Location = self.env["prema.dispatch.location"].sudo()
        if not (address or "").strip():
            return Location.browse()
        exact = Location.search([
            ("active", "=", True),
            ("address", "=ilike", address.strip()),
        ], limit=1)
        if exact:
            return exact
        normalized = Location._normalize_address_street(address)
        if normalized:
            match = Location.search([
                ("active", "=", True),
                ("normalized_address", "=", normalized),
            ], limit=1)
            if match:
                return match
        postal = Location._normalize_postal(postal_code or "")
        if postal:
            candidates = Location.search([
                ("active", "=", True),
                ("postal_code", "=ilike", postal),
            ], limit=20)
            normalized_matches = candidates.filtered(
                lambda location: location.normalized_address == normalized
            )
            if len(normalized_matches) == 1:
                return normalized_matches
        return Location.browse()

    @staticmethod
    def _manual_location_values(address, postal_code, company_name, stop_type):
        """Build a reviewable master-facility row from a complete CA address."""
        address = (address or "").strip()
        postal_code = (postal_code or "").strip().upper()
        if not address or not postal_code or not re.search(r"\b\d+[A-Za-z]?\b", address):
            return None
        parts = [part.strip() for part in address.split(",") if part.strip()]
        province_match = re.search(
            r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b",
            address,
            re.IGNORECASE,
        )
        city = ""
        if province_match and len(parts) >= 2:
            province_index = next(
                (index for index, part in enumerate(parts)
                 if re.search(r"\b%s\b" % province_match.group(1), part, re.IGNORECASE)),
                len(parts) - 1,
            )
            if province_index > 0:
                city = parts[province_index - 1]
        street = parts[0] if parts else address
        return {
            "name": (company_name or city or address)[:80],
            "business_name": company_name or "",
            "address": address,
            "street": street,
            "city": city,
            "province_code": province_match.group(1).upper() if province_match else "",
            "postal_code": postal_code,
            "stop_type": stop_type,
            "source_type": "dispatcher_manual",
            "verification_state": "pending_review",
        }

    def _ensure_customer_location_access(self, location, stop_type, company_name=""):
        commercial_partner = self.partner_id.commercial_partner_id
        values = {}
        if company_name:
            values["customer_alias"] = company_name
        values["can_pickup" if stop_type == "pickup" else "can_delivery"] = True
        self.env["logistics.location.customer.access"].sudo().ensure_access(
            location,
            commercial_partner,
            **values,
        )

    def _ensure_saved_location(self, stop_type):
        self.ensure_one()
        is_pickup = stop_type == "pickup"
        selected = self.pickup_location_id if is_pickup else self.delivery_location_id
        address = self.pickup_address if is_pickup else self.delivery_address
        postal = self.pickup_postal_code if is_pickup else self.delivery_postal_code
        company = self.pickup_company_name if is_pickup else self.delivery_company_name
        location = selected or self._match_saved_location(address, postal)
        if not location:
            values = self._manual_location_values(
                address, postal, company, stop_type)
            if not values:
                raise UserError(_(
                    "The %s location needs a complete civic address and postal "
                    "code before it can be saved. You may still prepare a "
                    "city-to-city quote and add the location later."
                ) % stop_type)
            location = self.env["prema.dispatch.location"].sudo().create(values)
        elif location.stop_type != "both" and location.stop_type != stop_type:
            location.sudo().write({"stop_type": "both"})
        self._ensure_customer_location_access(location, stop_type, company)
        return location

    def action_match_save_locations(self):
        """Explicitly reuse or create reviewed Saved Locations for this quote."""
        self.ensure_one()
        # Validate both manual rows before writing either one, avoiding a
        # partially saved pair when the other address is still city-only.
        for stop_type in ("pickup", "delivery"):
            selected = self.pickup_location_id if stop_type == "pickup" else self.delivery_location_id
            address = self.pickup_address if stop_type == "pickup" else self.delivery_address
            postal = self.pickup_postal_code if stop_type == "pickup" else self.delivery_postal_code
            company = self.pickup_company_name if stop_type == "pickup" else self.delivery_company_name
            if not selected and not self._match_saved_location(address, postal) \
                    and not self._manual_location_values(address, postal, company, stop_type):
                raise UserError(_(
                    "The %s location needs a complete civic address and postal "
                    "code before both locations can be saved."
                ) % stop_type)
        pickup = self._ensure_saved_location("pickup")
        delivery = self._ensure_saved_location("delivery")
        self.write({
            "pickup_location_id": pickup.id,
            "delivery_location_id": delivery.id,
            "extraction_summary": _(
                "Pickup and delivery were matched to Saved Locations. New "
                "manual locations, if any, were saved as Pending Review. "
                "Verify the facilities and then select Get Price."
            ),
        })
        return self._quote_form_action()

    def _weight_from_source_text(self):
        matches = re.findall(
            r"(\d[\d,]*(?:\.\d+)?)\s*(?:lb|lbs|pounds)\b",
            self.source_text or "",
            re.IGNORECASE,
        )
        if not matches:
            return 0.0
        return max(float(value.replace(",", "")) for value in matches)

    @staticmethod
    def _explicit_temperature(result):
        raw = result.get("temp_requirement")
        if raw in (None, False, ""):
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", str(raw))
        if not match:
            return None
        value = float(match.group(0))
        if re.search(r"(?:°\s*F|\bFahrenheit\b)", str(raw), re.IGNORECASE):
            value = (value - 32.0) * 5.0 / 9.0
        return value

    def _apply_source_extraction(self, result):
        """Apply extracted facts to editable fields; never apply an AI rate."""
        self.ensure_one()
        stops = result.get("stops") if isinstance(result, dict) else []
        stops = stops if isinstance(stops, list) else []
        pickup = next(
            (stop for stop in stops
             if str(stop.get("type") or "").lower() == "pickup"),
            stops[0] if stops else {},
        )
        deliveries = [
            stop for stop in stops
            if str(stop.get("type") or "").lower()
            in ("dropoff", "delivery", "drop", "drop-off", "drop off")
        ]
        delivery = deliveries[-1] if deliveries else (stops[-1] if len(stops) > 1 else {})
        pickup_address = (pickup.get("address") or "").strip()
        delivery_address = (delivery.get("address") or "").strip()
        pickup_company = (
            pickup.get("company_name") or pickup.get("name") or ""
        ).strip()
        delivery_company = (
            delivery.get("company_name") or delivery.get("name") or ""
        ).strip()

        pallets = int(
            result.get("max_onboard_pallets")
            or result.get("approximate_skids")
            or pickup.get("pallets_in")
            or pickup.get("pallets")
            or self.pallets
            or 1
        )
        load_type = str(result.get("service_type") or "").lower()
        reefer = bool(result.get("requires_reefer")) or bool(re.search(
            r"\b(?:frozen|chilled|reefer|refrigerated|temperature[- ]controlled)\b",
            self.source_text or "",
            re.IGNORECASE,
        ))
        explicit_temp = self._explicit_temperature(result)
        scheduled_date = result.get("scheduled_date")
        try:
            scheduled_date = (
                datetime.date.fromisoformat(str(scheduled_date)[:10])
                if scheduled_date else False
            )
        except ValueError:
            scheduled_date = False

        self.write({
            "pickup_location_id": False,
            "pickup_address": pickup_address or self.pickup_address,
            "pickup_company_name": pickup_company or self.pickup_company_name,
            "pickup_postal_code": self._postal_from_address(pickup_address)
                or self.pickup_postal_code,
            "delivery_location_id": False,
            "delivery_address": delivery_address or self.delivery_address,
            "delivery_company_name": delivery_company or self.delivery_company_name,
            "delivery_postal_code": self._postal_from_address(delivery_address)
                or self.delivery_postal_code,
            "pallets": max(pallets, 1),
            "weight_lbs": self._weight_from_source_text() or self.weight_lbs,
            "shipment_type": load_type if load_type in ("ltl", "ftl") else self.shipment_type,
            "temperature_mode": "reefer" if reefer else "dry",
            "required_temperature_c": explicit_temp if explicit_temp is not None else 0.0,
            "temperature_confirmed": explicit_temp is not None if reefer else True,
            "requested_pickup_date": scheduled_date or self.requested_pickup_date,
            "liftgate_pickup": bool(pickup.get("liftgate")),
            "liftgate_delivery": bool(delivery.get("liftgate")),
            "price": 0.0,
            "system_calculated_price": 0.0,
            "customer_quoted_price": 0.0,
            "quote_token": False,
            "pickup_date": False,
            "delivery_date": False,
            "result_text": False,
            "extraction_summary": _(
                "Shipment details were extracted for review. Verify both "
                "addresses, postal codes, pallets, weight, date, equipment, "
                "and temperature before selecting Get Price. No rate was "
                "generated and nothing was sent or booked."
            ),
        })
        pickup_match = self._match_saved_location(
            self.pickup_address, self.pickup_postal_code)
        delivery_match = self._match_saved_location(
            self.delivery_address, self.delivery_postal_code)
        matched_values = {}
        if pickup_match:
            matched_values["pickup_location_id"] = pickup_match.id
        if delivery_match:
            matched_values["delivery_location_id"] = delivery_match.id
        if matched_values:
            self.write(matched_values)
            self.extraction_summary += _(
                " Existing Saved Locations were reused where an exact "
                "address match was found."
            )
        return self._quote_form_action()

    def action_extract_source_text(self):
        """Use Prema AI for facts only, then return to the editable wizard."""
        self.ensure_one()
        if not self.crm_lead_id:
            raise UserError(_("Open this quotation from a CRM opportunity first."))
        if not (self.source_text or "").strip():
            raise UserError(_("Paste or enter the customer's freight request first."))
        from odoo.addons.premafirm_ai_engine.services.invoice_ai_service import (
            InvoiceAIService,
        )
        try:
            result = InvoiceAIService(self.env).analyze_from_text(
                self.crm_lead_id,
                self.source_text,
                "",
            )
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        except Exception as exc:
            _logger.exception(
                "CRM freight-request extraction failed for lead %s",
                self.crm_lead_id.id,
            )
            raise UserError(_(
                "The shipment details could not be extracted. The source "
                "text is unchanged; enter the fields manually or try again."
            )) from exc
        return self._apply_source_extraction(result or {})

    def _sync_persistent_quote(self, session, quote):
        """Create/update the persistent phone quotation; never save locations."""
        self.ensure_one()
        pickup = self._stop_values("pickup")
        delivery = self._stop_values("delivery")
        vals = {
            "partner_id": self.partner_id.id,
            "crm_lead_id": self.crm_lead_id.id or False,
            "company_name": self.partner_id.commercial_partner_id.name or self.partner_id.name,
            "contact_name": self.partner_id.name or "",
            "contact_email": self.partner_id.email or "",
            "contact_phone": self.partner_id.phone or self.partner_id.mobile or "",
            "pickup_postal_code": pickup.get("postal_code") or "",
            "pickup_address": pickup.get("formatted_address") or "",
            "delivery_postal_code": delivery.get("postal_code") or "",
            "delivery_address": delivery.get("formatted_address") or "",
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "temperature_mode": self.temperature_mode,
            "required_temperature_c": self._canonical_required_temperature(),
            "load_type": self.shipment_type,
            "requested_pickup_date": session.pickup_date or self.requested_pickup_date,
            # quoted_price is the FINAL customer-facing price; the system
            # price is preserved as the audit reference on the quote.
            "quoted_price": self.customer_quoted_price or session.calculated_price,
            "system_calculated_price": session.calculated_price,
            "manual_price_reason": self.manual_price_reason or "",
            "departure_id": session.departure_id.id if session.departure_id else False,
            "routing_strategy": quote.get("lane_name") or "",
            "resolved_fsa_pickup": session.pickup_fsa_id.fsa if session.pickup_fsa_id else "",
            "resolved_fsa_delivery": session.delivery_fsa_id.fsa if session.delivery_fsa_id else "",
            "state": "quoted",
            "source": "internal" if self.crm_lead_id else "phone",
            "notes": "\n".join(filter(None, [
                f"Pickup instructions: {self.pickup_instructions}" if self.pickup_instructions else "",
                f"Delivery instructions: {self.delivery_instructions}" if self.delivery_instructions else "",
            ])),
        }
        if self.quote_id and self.quote_id.exists() and not self.quote_id.booking_id:
            self.quote_id.sudo().write(vals)
        else:
            self.quote_id = self.env["logistics.custom.quote"].sudo().create(vals)
        return self.quote_id

    # ── Requested-date availability (the portal's own authority) ─────

    def _available_pickup_dates(self):
        """Nearest valid pickup dates for this exact request.

        ONE authority per path, the same the customer portal uses:
          - Coordinates available (an existing facility is selected): the
            exact calendar_availability service the portal calendar renders
            (eligible_pickup_dates) — full date list, transfer chains
            included, capacity-validated per departure.
          - FSA-only path (one-off typed address): enumerate the same
            DepartureResolver the FSA pricing path runs — resolve, then
            step past each found date — so direct, transfer, LTL and FTL
            all report exactly the dates the pricing engine will accept.

        The portal calendar never offers TODAY (same-day pickup requires a
        cutoff check), so the list starts tomorrow — a requested date that
        equals today is treated as unavailable, exactly as the portal does.

        Returns a sorted list of datetime.date (bounded). Creates nothing.
        """
        self.ensure_one()
        pickup = self._stop_values("pickup")
        delivery = self._stop_values("delivery")
        stops = [
            {"stop_type": "pickup", "latitude": pickup.get("latitude") or 0.0,
             "longitude": pickup.get("longitude") or 0.0,
             "city": pickup.get("city") or "",
             "postal_code": pickup.get("postal_code") or ""},
            {"stop_type": "delivery", "latitude": delivery.get("latitude") or 0.0,
             "longitude": delivery.get("longitude") or 0.0,
             "city": delivery.get("city") or "",
             "postal_code": delivery.get("postal_code") or ""},
        ]
        if all(s["latitude"] and s["longitude"] for s in stops):
            from ..services.shipment_routing_service import ShipmentRoutingService
            verdict = ShipmentRoutingService(self.env).calendar_availability(
                stops,
                physical_pallets=self.pallets,
                weight_lbs=self.weight_lbs,
                equipment=self.temperature_mode,
                shipment_type=self.shipment_type,
            )
            dates = []
            for entry in verdict["dates"] or []:
                date_val = entry.get("date")
                if hasattr(date_val, "strftime"):
                    date_val = date_val.date() if hasattr(date_val, "date") else date_val
                elif date_val:
                    date_val = datetime.date.fromisoformat(str(date_val)[:10])
                if date_val and date_val not in dates:
                    dates.append(date_val)
            return sorted(dates)[:24]

        Fsa = self.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.resolve_from_postal(self.pickup_postal_code)
        delivery_fsa = Fsa.resolve_from_postal(self.delivery_postal_code)
        if not pickup_fsa or not delivery_fsa \
                or not pickup_fsa.region_id or not delivery_fsa.region_id:
            return []

        from ..services.departure_resolver import DepartureResolver
        from ..services.region_resolver import RegionResolver
        from ..services.temperature_compat import to_canonical_temperature_mode
        region_resolver = RegionResolver(self.env)
        origin_region = region_resolver.canonical_region(pickup_fsa.region_id)
        dest_region = region_resolver.canonical_region(delivery_fsa.region_id)
        if not origin_region or not dest_region:
            return []
        resolver = DepartureResolver(self.env)
        equipment = to_canonical_temperature_mode(self.temperature_mode)
        dates = []
        cursor = datetime.date.today() + datetime.timedelta(days=1)
        horizon_end = cursor + datetime.timedelta(days=56)
        while cursor <= horizon_end and len(dates) < 24:
            resolution = resolver.resolve(
                origin_region, dest_region, equipment,
                self.pallets, self.weight_lbs,
                earliest_pickup_date=cursor,
                service_type=self.shipment_type,
            )
            if not resolution.available:
                break
            dep_date = resolution.legs[0].departure.departure_date
            if dep_date not in dates:
                dates.append(dep_date)
            cursor = dep_date + datetime.timedelta(days=1)
        return sorted(dates)[:24]

    def _quote_form_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    @api.depends("system_calculated_price", "customer_quoted_price")
    def _compute_manual_price_adjustment(self):
        for rec in self:
            rec.manual_price_adjustment = round(
                (rec.customer_quoted_price or 0.0)
                - (rec.system_calculated_price or 0.0), 2)

    def action_reset_price(self):
        """Convenience reset — quoted price back to the system price; the
        reason is then no longer required."""
        self.ensure_one()
        self.customer_quoted_price = self.system_calculated_price
        self.manual_price_reason = ""
        return self._quote_form_action()

    def action_get_price(self):
        self.ensure_one()
        from ..services.booking_orchestration_service import BookingOrchestrationService

        self._validate_quote_inputs()

        # ── Requested-date validation ───────────────────────────────
        # A requested pickup date no scheduled departure can serve must
        # never be silently moved to another date — the price shown would
        # bind to a different pickup. Refuse it and offer the nearest
        # valid pickup dates (the same authority as the portal calendar)
        # so staff can pick an alternate and recalculate.
        if self.requested_pickup_date:
            requested = self.requested_pickup_date
            if not isinstance(requested, datetime.date):
                try:
                    requested = datetime.date.fromisoformat(str(requested)[:10])
                except ValueError:
                    raise UserError(_("The requested pickup date is not valid."))
            available = self._available_pickup_dates()
            if requested not in available:
                self.quote_token = False
                self.price = 0.0
                self.pickup_date = False
                self.delivery_date = False
                if not available:
                    self.available_pickup_dates = False
                    self.result_text = _(
                        "No scheduled service is available for this route in the "
                        "coming weeks.\nSubmit a Custom Quote request, or check "
                        "corridor configuration.")
                else:
                    labels = ", ".join(
                        d.strftime("%a %b %d") for d in available[:6])
                    self.available_pickup_dates = labels
                    self.result_text = _(
                        "Requested date unavailable. Available pickup dates: %s"
                        % labels)
                return self._quote_form_action()

        service = BookingOrchestrationService(self.env)
        quote = service.prepare_quote(self._normalized_request(service))
        session = self.env["logistics.pricing.session"].sudo().search([
            ("token", "=", quote["quote_token"]),
        ], limit=1)
        if not session:
            raise UserError(_("The price session could not be created. Please try again."))

        self.quote_token = session.token
        self.price = session.calculated_price
        # Each fresh price seeds the editable quoted price at the system
        # price; staff may then negotiate any positive amount.
        self.system_calculated_price = session.calculated_price
        self.customer_quoted_price = session.calculated_price
        self.manual_price_reason = ""
        self.pickup_date = session.pickup_date
        self.delivery_date = session.delivery_date_estimate
        self._sync_persistent_quote(session, quote)

        service_label = (
            "Dedicated Full Truckload"
            if session.shipment_type == "ftl"
            else "Scheduled LTL"
        )
        lines = [
            f"Service: {service_label}",
            f"Route: {quote.get('lane_name') or 'Scheduled Service'}",
            f"Pickup: {session.pickup_date} | Delivery: {session.delivery_date_estimate}",
            "",
        ]
        for line in session.customer_safe_snapshot_lines():
            lines.append(f"  {line['label']:<35s} ${line['amount']:>10.2f}")
        lines.extend(["", f"TOTAL{'':<30s} ${session.calculated_price:>10.2f}"])
        self.result_text = "\n".join(lines)
        return self._quote_form_action()

    def action_open_quote(self):
        self.ensure_one()
        if not self.quote_id:
            raise UserError(_("Get a price first."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Quotation"),
            "res_model": "logistics.custom.quote",
            "res_id": self.quote_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_confirm_booking(self):
        """Accept the phone quote and create the booking through one engine."""
        self.ensure_one()
        if not self.price or not self.quote_token:
            raise UserError(_("Get a price first."))

        if self.booking_id:
            if self.quote_id and not self.quote_id.booking_id:
                self.quote_id.sudo().write({
                    "booking_id": self.booking_id.id,
                    "state": "converted",
                })
            return {
                "type": "ir.actions.act_window",
                "res_model": "logistics.phone.booking",
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }

        from ..services.booking_orchestration_service import BookingOrchestrationService

        svc = BookingOrchestrationService(self.env)
        session = self.env["logistics.pricing.session"].sudo().search([
            ("token", "=", self.quote_token),
        ], limit=1)
        if not session or session.is_expired():
            raise UserError(_("This price is no longer available. Please get a new price."))

        # Manual / negotiated sell price: any positive amount is allowed
        # (discounts AND increases); a reason is required when it differs
        # from the system calculated price.
        final_price = self.customer_quoted_price or self.price
        if final_price <= 0:
            raise UserError(_("The customer quoted price must be greater than zero."))
        if round(final_price, 2) != round(self.system_calculated_price or self.price, 2) \
                and not self.manual_price_reason:
            raise UserError(_(
                "Please provide a Manual Price Reason — the quoted price "
                "differs from the system calculated price."))

        # The persistent quotation must reflect the FINAL price offered
        # (audit + PDF). The CQ write path records the change audit.
        if self.quote_id and self.quote_id.exists() and not self.quote_id.booking_id:
            self.quote_id.sudo().write({
                "quoted_price": final_price,
                "manual_price_reason": self.manual_price_reason or "",
            })

        # No Saved Location / Master Facility is created by this action.
        # Existing selected facility ids and one-off text are passed directly
        # into the canonical confirmation request. Capacity and departure are
        # revalidated by BookingOrchestrationService under the normal locks.
        booking = svc.confirm_from_internal(
            self._normalized_request(svc),
            skip_invoice=False,
            pricing_session=session,
            sell_price_override=final_price,
            sell_price_override_reason=self.manual_price_reason or "",
            sell_price_override_by=self.env.user.id,
            system_calculated_price=self.system_calculated_price or self.price,
        )
        self.booking_id = booking.id
        if not self.quote_id:
            self._sync_persistent_quote(session, {"lane_name": session.corridor_id.name if session.corridor_id else ""})
        self.quote_id.sudo().write({
            "booking_id": booking.id,
            "state": "converted",
        })

        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

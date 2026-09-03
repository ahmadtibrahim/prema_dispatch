import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
    pickup_postal_code = fields.Char(string="Pickup Postal Code", required=True)
    pickup_address = fields.Char(string="Pickup Address")
    pickup_instructions = fields.Text(string="Pickup Instructions")
    delivery_postal_code = fields.Char(string="Delivery Postal Code", required=True)
    delivery_address = fields.Char(string="Delivery Address")
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
        from ..services.temperature_service import parse_temperature
        return parse_temperature(
            self.required_temperature_c,
            unit=self.submitted_temperature_unit or "c")
    # Kept for compatibility with existing transient rows/views from older
    # versions. The phone flow no longer needs a separate confirmation box;
    # the canonical service validates the numeric temperature itself.
    temperature_confirmed = fields.Boolean(default=True)
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

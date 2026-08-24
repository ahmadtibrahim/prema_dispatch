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
        help="Optional. Leave blank to price the next compatible scheduled departure.",
    )
    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float(string="Weight (lbs)", default=750.0)
    temperature_mode = fields.Selection(TEMP_MODES, default="reefer", required=True)
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        default=15.0,
        help="Required for Reefer bookings. 0°C is a valid value.",
    )
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

    def _saved_location_for(self, location):
        """Return this customer's existing Saved Location copy when present.

        This is a lookup only. The phone quote flow never creates one.
        """
        self.ensure_one()
        if not location or not self.partner_id:
            return self.env["logistics.saved.location"].browse()
        return self.env["logistics.saved.location"].sudo().search([
            ("dispatch_location_id", "=", location.id),
            ("commercial_partner_id", "=", self.partner_id.commercial_partner_id.id),
            ("active", "=", True),
        ], limit=1)

    def _location_payload(self, location, stop_type):
        """Physical master + only this customer's private metadata."""
        self.ensure_one()
        if not location:
            return {}
        access = self._customer_access_for(location)
        saved = self._saved_location_for(location)
        instructions = ""
        if stop_type == "pickup":
            instructions = (
                (access.pickup_instructions if access else "")
                or (saved.pickup_instructions if saved else "")
                or ""
            )
        else:
            instructions = (
                (access.delivery_instructions if access else "")
                or (saved.delivery_instructions if saved else "")
                or ""
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
            "saved_location_id": saved.id if saved else False,
            "dispatch_location_id": location.id,
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
            "required_temperature_c": (
                self.required_temperature_c
                if self.temperature_mode == "reefer"
                else None
            ),
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
            "required_temperature_c": (
                self.required_temperature_c if self.temperature_mode == "reefer" else None
            ),
            "load_type": self.shipment_type,
            "requested_pickup_date": session.pickup_date or self.requested_pickup_date,
            "quoted_price": session.calculated_price,
            "departure_id": session.departure_id.id if session.departure_id else False,
            "routing_strategy": quote.get("lane_name") or "",
            "resolved_fsa_pickup": session.pickup_fsa_id.fsa if session.pickup_fsa_id else "",
            "resolved_fsa_delivery": session.delivery_fsa_id.fsa if session.delivery_fsa_id else "",
            "state": "quoted",
            "source": "phone",
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

    def action_get_price(self):
        self.ensure_one()
        from ..services.booking_orchestration_service import BookingOrchestrationService

        service = BookingOrchestrationService(self.env)
        quote = service.prepare_quote(self._normalized_request(service))
        session = self.env["logistics.pricing.session"].sudo().search([
            ("token", "=", quote["quote_token"]),
        ], limit=1)
        if not session:
            raise UserError(_("The price session could not be created. Please try again."))

        self.quote_token = session.token
        self.price = session.calculated_price
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
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

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

        # No Saved Location / Master Facility is created by this action.
        # Existing selected facility ids and one-off text are passed directly
        # into the canonical confirmation request. Capacity and departure are
        # revalidated by BookingOrchestrationService under the normal locks.
        booking = svc.confirm_from_internal(
            self._normalized_request(svc),
            skip_invoice=False,
            pricing_session=session,
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

from odoo import _, api, fields, models
from odoo.exceptions import UserError

SHIPMENT_TYPES = [("ltl", "LTL"), ("ftl", "FTL")]
TEMP_MODES = [("dry", "Dry"), ("reefer", "Reefer")]


class LogisticsPhoneBooking(models.TransientModel):
    """Internal wizard for dispatchers/staff to book on behalf of a phone customer."""
    _name = "logistics.phone.booking"
    _description = "Phone Booking Wizard"

    # Customer
    partner_id = fields.Many2one("res.partner", string="Customer", required=True)

    # Shipment
    pickup_postal_code = fields.Char(string="Pickup Postal Code", required=True)
    pickup_address = fields.Char(string="Pickup Address")
    pickup_instructions = fields.Text(string="Pickup Instructions")
    delivery_postal_code = fields.Char(string="Delivery Postal Code", required=True)
    delivery_address = fields.Char(string="Delivery Address")
    delivery_instructions = fields.Text(string="Delivery Instructions")

    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float(string="Weight (lbs)", default=800.0)
    temperature_mode = fields.Selection(TEMP_MODES, default="dry", required=True)
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        help="Required for Reefer bookings. 0°C is a valid value.",
    )
    temperature_confirmed = fields.Boolean(
        string="Temperature Confirmed",
        help="Confirms that the numeric Reefer temperature was intentionally entered; 0°C is valid.",
    )
    shipment_type = fields.Selection(SHIPMENT_TYPES, default="ltl")
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean(string="Same-Day Requested")

    # Results
    result_text = fields.Text(readonly=True)
    price = fields.Float(readonly=True)
    pickup_date = fields.Date(readonly=True)
    delivery_date = fields.Date(readonly=True)
    quote_token = fields.Char(readonly=True, copy=False)
    booking_id = fields.Many2one("logistics.booking", readonly=True, string="Created Booking")

    @api.onchange(
        "partner_id", "pickup_postal_code", "pickup_address",
        "delivery_postal_code", "delivery_address", "pallets", "weight_lbs",
        "temperature_mode", "required_temperature_c", "temperature_confirmed",
        "shipment_type", "liftgate_pickup", "liftgate_delivery", "appointment",
        "residential", "same_day_requested",
    )
    def _onchange_quote_inputs(self):
        """A displayed price is valid only for the unchanged request."""
        if not self.booking_id:
            self.quote_token = False
            self.price = 0.0
            self.pickup_date = False
            self.delivery_date = False
            self.result_text = False

    def _normalized_request(self, service):
        self.ensure_one()
        return service.normalize_request({
            "partner_id": self.partner_id.id,
            "source_model": self._name,
            "source_res_id": self.id,
            "pickup_stops": [{
                "street": self.pickup_address or "",
                "formatted_address": self.pickup_address or "",
                "postal_code": self.pickup_postal_code,
                "instructions": self.pickup_instructions or "",
                "pallet_count": self.pallets,
                "weight_lb": self.weight_lbs,
                "liftgate_required": self.liftgate_pickup,
            }],
            "delivery_stops": [{
                "street": self.delivery_address or "",
                "formatted_address": self.delivery_address or "",
                "postal_code": self.delivery_postal_code,
                "instructions": self.delivery_instructions or "",
                "pallet_count": self.pallets,
                "weight_lb": self.weight_lbs,
                "liftgate_required": self.liftgate_delivery,
            }],
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "load_type": self.shipment_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": (
                self.required_temperature_c
                if self.temperature_mode == "reefer"
                else None
            ),
            "pricing_method": "corridor",
            "liftgate_pickup": self.liftgate_pickup,
            "liftgate_delivery": self.liftgate_delivery,
            "appointment": self.appointment,
            "residential": self.residential,
            "same_day_requested": self.same_day_requested,
            "idempotency_key": f"phone:{self.id}",
        }, source_channel="phone")

    def action_get_price(self):
        self.ensure_one()
        if self.temperature_mode == "reefer" and not self.temperature_confirmed:
            raise UserError(_("Enter and confirm the numeric Reefer temperature; 0°C is valid."))
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

        lines = []
        lines.append(f"Corridor: {quote['lane_name']}")
        lines.append("Service: Scheduled LTL")
        lines.append(f"Pickup: {session.pickup_date} | Delivery: {session.delivery_date_estimate}")
        lines.append("")
        for line in quote["price_lines"]:
            lines.append(f"  {line['label']:<35s} ${line['amount']:>10.2f}")
        self.result_text = "\n".join(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_confirm_booking(self):
        self.ensure_one()
        if self.temperature_mode == "reefer" and not self.temperature_confirmed:
            raise UserError(_("Enter and confirm the numeric Reefer temperature; 0°C is valid."))
        if not self.price or not self.quote_token:
            raise UserError(_("Get a price first."))

        # Idempotency: return existing booking if already created
        if self.booking_id:
            return {
                "type": "ir.actions.act_window",
                "res_model": "logistics.phone.booking",
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }

        # Use canonical BookingOrchestrationService for phone bookings
        from ..services.booking_orchestration_service import BookingOrchestrationService

        svc = BookingOrchestrationService(self.env)
        session = self.env["logistics.pricing.session"].sudo().search([
            ("token", "=", self.quote_token),
        ], limit=1)
        if not session:
            raise UserError(_("This price is no longer available. Please get a new price."))

        booking = svc.confirm_from_internal(
            self._normalized_request(svc),
            skip_invoice=False,
            pricing_session=session,
        )
        self.booking_id = booking.id

        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

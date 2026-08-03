from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services.pricing_service import PricingService

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
    shipment_type = fields.Selection(SHIPMENT_TYPES, default="ltl")
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()

    # Results
    result_text = fields.Text(readonly=True)
    price = fields.Float(readonly=True)
    pickup_date = fields.Date(readonly=True)
    delivery_date = fields.Date(readonly=True)
    booking_id = fields.Many2one("logistics.booking", readonly=True, string="Created Booking")

    def action_get_price(self):
        self.ensure_one()
        Fsa = self.env["logistics.fsa"]
        pickup_fsa = Fsa.resolve_from_postal(self.pickup_postal_code)
        delivery_fsa = Fsa.resolve_from_postal(self.delivery_postal_code)

        if not pickup_fsa or not pickup_fsa.pickup_supported:
            raise UserError(_("Pickup postal code not recognized or not in service area."))
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            raise UserError(_("Delivery postal code not recognized or not in service area."))

        result = PricingService(self.env).calculate(
            pickup_fsa, delivery_fsa, self.shipment_type, self.temperature_mode,
            self.pallets, self.weight_lbs, self.liftgate_pickup, self.liftgate_delivery,
            self.appointment, self.residential, partner=self.partner_id,
            required_temperature_c=self.required_temperature_c if self.temperature_mode == "reefer" else None,
        )

        if not result.available:
            raise UserError(_("Pricing not available: %s") % result.reason)

        self.price = result.calculated_price
        self.pickup_date = result.pickup_date
        self.delivery_date = result.delivery_date_estimate

        lines = []
        lines.append(f"Lane: {result.lane.name}")
        lines.append(f"Service: {result.service_offering.name}")
        lines.append(f"Pickup: {result.pickup_date} | Delivery: {result.delivery_date_estimate}")
        lines.append("")
        for line in result.price_lines:
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
        if not self.price:
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

        wizard_uuid = getattr(self, '_phone_booking_uuid', None)
        if not wizard_uuid:
            import uuid
            wizard_uuid = uuid.uuid4().hex[:12]
            self._phone_booking_uuid = wizard_uuid

        norm = svc.normalize_request({
            "partner_id": self.partner_id.id,
            "pickup_stops": [{
                "postal_code": self.pickup_postal_code,
                "formatted_address": self.pickup_address or "",
                "instructions": self.pickup_instructions or "",
            }],
            "delivery_stops": [{
                "postal_code": self.delivery_postal_code,
                "formatted_address": self.delivery_address or "",
                "instructions": self.delivery_instructions or "",
            }],
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "load_type": self.shipment_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": self.required_temperature_c if self.temperature_mode == "reefer" else None,
            "pricing_method": "rate_plan",
            "liftgate_pickup": self.liftgate_pickup,
            "liftgate_delivery": self.liftgate_delivery,
            "appointment": self.appointment,
            "residential": self.residential,
            "idempotency_key": f"phone:{wizard_uuid}",
        }, source_channel="phone")

        booking = svc.confirm_from_internal(norm, skip_invoice=False)
        self.booking_id = booking.id

        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.phone.booking",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PremaDispatchBookLoadWizard(models.TransientModel):
    _name = "prema.dispatch.book.load.wizard"
    _description = "Book Dispatch Load"

    move_id = fields.Many2one("account.move", required=True, ondelete="cascade")
    partner_id = fields.Many2one("res.partner")
    booking_mode = fields.Selection([
        ("scheduled_ltl", "Scheduled LTL Network"),
        ("custom", "Custom / Expedited"),
    ], default="scheduled_ltl", required=True)
    service_type = fields.Selection([("local", "Local"), ("ltl", "LTL"), ("ftl", "FTL"), ("dedicated", "Dedicated"), ("other", "Other")], default="ltl")
    equipment_type = fields.Selection([("dry", "Dry Van"), ("reefer", "Reefer"), ("flatbed", "Flatbed"), ("other", "Other")], default="dry")
    requires_liftgate = fields.Boolean()
    commodity = fields.Char()
    expected_skids = fields.Integer()
    total_weight_lbs = fields.Float()
    scheduled_pickup = fields.Datetime()
    pickup_saved_location_id = fields.Many2one(
        "prema.dispatch.location",
        domain="[('active','=',True), '|', ('partner_id','=',partner_id), ('partner_id','=',False)]",
    )
    delivery_saved_location_id = fields.Many2one(
        "prema.dispatch.location",
        domain="[('active','=',True), '|', ('partner_id','=',partner_id), ('partner_id','=',False)]",
    )
    required_temperature_c = fields.Float(string="Required Temperature °C")
    temperature_confirmed = fields.Boolean(string="Temperature Confirmed")
    customer_reference = fields.Char()
    purchase_order = fields.Char()
    bol_reference = fields.Char(string="BOL / Reference")
    general_notes = fields.Text()

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        move = self.env["account.move"].browse(self.env.context.get("active_id") or vals.get("move_id"))
        if move.exists():
            vals.update({"move_id": move.id, "partner_id": move.partner_id.id, "customer_reference": move.ref or move.name, "purchase_order": getattr(move, "premafirm_po", "") or "", "bol_reference": getattr(move, "premafirm_bol", "") or "", "scheduled_pickup": move._resolve_scheduled_pickup()})
        return vals

    def action_confirm(self):
        self.ensure_one()
        move = self.move_id
        if move.logistics_booking_id:
            return {
                "type": "ir.actions.act_window", "name": _("Booking"),
                "res_model": "logistics.booking", "res_id": move.logistics_booking_id.id,
                "view_mode": "form", "target": "current",
            }
        if self.expected_skids <= 0 or self.total_weight_lbs < 0:
            raise UserError(_("Pallets must be at least 1 and weight cannot be negative."))
        if not self.pickup_saved_location_id or not self.delivery_saved_location_id:
            raise UserError(_("Choose both Pickup and Delivery Saved Locations."))
        for label, location in ((_("Pickup"), self.pickup_saved_location_id), (_("Delivery"), self.delivery_saved_location_id)):
            if not location.google_verified or not location.google_place_id:
                raise UserError(_("%s address must be selected and verified through Google Places.") % label)
        if not self.scheduled_pickup:
            raise UserError(_("Requested pickup date/time is required."))
        if self.booking_mode == "scheduled_ltl" and self.service_type != "ltl":
            raise UserError(_("Scheduled Network booking must use LTL. Choose Custom / Expedited for FTL."))
        if self.equipment_type == "reefer" and not self.temperature_confirmed:
            raise UserError(_("Enter and confirm the numeric Reefer temperature; 0°C is valid."))

        try:
            from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import BookingOrchestrationService
        except ImportError as exc:
            raise UserError(_("Prema Logistics Booking is required before an invoice load can be booked.")) from exc

        def location_values(location, pickup):
            return {
                "saved_location_id": location.id,
                "company_name": location.business_name or location.name,
                "formatted_address": location.normalized_address or location.address,
                "street": location.street or location.address,
                "city": location.city or "",
                "province_state": location.province_code or "",
                "postal_code": location.postal_code or "",
                "google_place_id": location.google_place_id,
                "latitude": location.pin_lat,
                "longitude": location.pin_lng,
                "pallet_count": self.expected_skids if pickup else 0,
                "weight_lbs": self.total_weight_lbs if pickup else 0.0,
                "liftgate_required": self.requires_liftgate,
                "instructions": self.general_notes or "",
            }

        service = BookingOrchestrationService(self.env)
        request = service.normalize_request({
            "partner_id": self.partner_id.id,
            "source_model": "account.move",
            "source_res_id": move.id,
            "source_reference": move.name or move.ref or "",
            "pickup_stops": [location_values(self.pickup_saved_location_id, True)],
            "delivery_stops": [location_values(self.delivery_saved_location_id, False)],
            "pallets": self.expected_skids,
            "weight_lbs": self.total_weight_lbs,
            "load_type": "ltl" if self.service_type == "ltl" else "ftl",
            "equipment_type": "reefer" if self.equipment_type == "reefer" else "dry",
            "required_temperature_c": self.required_temperature_c if self.equipment_type == "reefer" else None,
            "commodity": self.commodity or "",
            "po_number": self.purchase_order or "",
            "bol_number": self.bol_reference or "",
            "customer_reference": self.customer_reference or move.ref or move.name or "",
            "instructions": self.general_notes or "",
            "requested_pickup_date": self.scheduled_pickup.date(),
            "pricing_method": "corridor" if self.booking_mode == "scheduled_ltl" else "imported_invoice",
            "agreed_rate": 0.0 if self.booking_mode == "scheduled_ltl" else (move.amount_untaxed or move.amount_total),
            "existing_invoice_id": move.id,
            "idempotency_key": f"invoice:{move.id}:{self.booking_mode}",
        }, source_channel="invoice")
        booking = service.confirm_from_internal(request, existing_invoice=move, skip_invoice=False)
        move.write({"logistics_booking_id": booking.id})
        return {
            "type": "ir.actions.act_window", "name": _("Booking"),
            "res_model": "logistics.booking", "res_id": booking.id,
            "view_mode": "form", "target": "current",
        }

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleOrderBookWizard(models.TransientModel):
    """Book a canonical logistics.booking from a Sales Order.

    D-C1 (master §1/§14): mirrors the invoice Book Load wizard
    (prema.dispatch.book.load.wizard) — the same confirm path through
    BookingOrchestrationService with the "sale_order" source channel and
    idempotency key f"sale.order:{so.id}:{booking_mode}". The booking's
    dispatch-job bridge creates the Planner card(s) and back-links
    job.sale_order_id / job.logistics_booking_id.
    """

    _name = "prema.dispatch.so.book.wizard"
    _inherit = ["prema.dispatch.book.load.timing.mixin"]
    _description = "Book Load from Sales Order"

    sale_order_id = fields.Many2one("sale.order", required=True, ondelete="cascade")
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
    required_temperature_c = fields.Float(string="Required Temperature")
    submitted_temperature_unit = fields.Selection(
        [("c", "°C"), ("f", "°F")], string="Temperature Unit", default="c")
    temperature_c_readback = fields.Float(
        string="Stored °C (canonical)", readonly=True,
        compute="_compute_temperature_c_readback",
        help="What will actually be stored at confirm: your entry converted "
             "to canonical Celsius. Live — correct the value or the unit any "
             "time before confirming.")
    temperature_confirmed = fields.Boolean(
        string="Temperature Confirmed",
        help="Confirms that the numeric Reefer temperature was intentionally "
             "entered; 0°C is valid. Editing the temperature or the unit "
             "clears this box — confirm the corrected value again.")
    customer_reference = fields.Char()
    purchase_order = fields.Char()
    bol_reference = fields.Char(string="BOL / Reference")
    general_notes = fields.Text()
    mapping_summary = fields.Text(
        string="Auto-mapped (review)", readonly=True,
        help="Deterministic fields already carried over from the Sales Order"
             " (structured columns -> stored AI result -> line details)."
             " Remaining gaps are entered manually — never guessed.")

    @api.depends("equipment_type", "required_temperature_c",
                 "submitted_temperature_unit")
    def _compute_temperature_c_readback(self):
        for wizard in self:
            value = wizard.required_temperature_c
            # Identity checks only — 0.0 is a VALID temperature (0°C).
            if (wizard.equipment_type != "reefer"
                    or value is False or value is None or value == ""):
                wizard.temperature_c_readback = False
                continue
            try:
                from odoo.addons.prema_logistics_booking.services.temperature_service import (  # noqa: E501
                    parse_temperature)
                canonical = parse_temperature(
                    value, unit=wizard.submitted_temperature_unit or "c")
            except Exception:
                canonical = None
            wizard.temperature_c_readback = (
                False if canonical is None else round(canonical, 1))

    @api.onchange("equipment_type", "required_temperature_c",
                  "submitted_temperature_unit")
    def _onchange_temperature_revalidate(self):
        """Temperature edits re-validate immediately (never a stale block):
        editing the value or the unit clears the earlier confirmation, and
        Dry never carries a temperature value."""
        for wizard in self:
            if wizard.equipment_type != "reefer":
                wizard.required_temperature_c = False
                wizard.temperature_confirmed = False
            else:
                wizard.temperature_confirmed = False

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        so = self.env["sale.order"].browse(
            self.env.context.get("active_id") or vals.get("sale_order_id"))
        if not so.exists():
            return vals
        vals.update({
            "sale_order_id": so.id,
            "partner_id": (so.partner_invoice_id or so.partner_id).id,
        })
        # ONE shared deterministic mapping service (same ladder and rules as
        # the invoice Book Load wizard — never invokes AI).
        try:
            from odoo.addons.prema_dispatch.services.book_load_mapping import (
                BookLoadMappingService)
            service = BookLoadMappingService(self.env)
            mapped = service.suggest_for(so)
            # Explicit default_* context values (caller intent) win.
            applied = {}
            for key, value in mapped.items():
                if "default_%s" % key not in self.env.context:
                    vals[key] = value
                    applied[key] = value
            vals["mapping_summary"] = "\n".join(
                service.summary_for(so, applied))
        except Exception:
            _logger.warning("Book Load auto-map failed for sale.order %s",
                            so.id, exc_info=True)
        return vals

    def action_confirm(self):
        self.ensure_one()
        so = self.sale_order_id
        # Idempotency net: a canonical booking already exists for this SO
        # (either click of a double-click, or another entry flow) — open it.
        existing = self.env["logistics.booking"].sudo().search(
            [("sale_order_id", "=", so.id)], order="id", limit=1)
        if existing:
            return self._open_booking(existing)

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
            raise UserError(_("Prema Logistics Booking is required before a Sales Order load can be booked.")) from exc

        # The order's ACCEPTED commercial amount is the customer price —
        # for BOTH booking modes. A priced Sales Order is a closed deal: the
        # booking carries it, and the corridor quote is calculated for
        # routing and audit only (never substituted for it, and never a $0).
        agreed_rate = so.amount_untaxed or so.amount_total
        if not agreed_rate:
            raise UserError(_(
                "Sales Order %s carries no accepted amount — price the order "
                "before booking the load; the accepted price is what the "
                "customer will be billed."
            ) % so.name)

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

        def timing_values(side):
            """TIER 2 §2/§4: the dispatcher's (auto-filled, then edited)
            timing for this side — the customer's window on one channel,
            the dock's own hours on the other."""
            values = self._timing_stop_values(side)
            values["timezone"] = "America/Toronto"
            return values

        service = BookingOrchestrationService(self.env)
        request = service.normalize_request({
            "partner_id": self.partner_id.id,
            "source_model": "sale.order",
            "source_res_id": so.id,
            "source_reference": so.name,
            "pickup_stops": [dict(location_values(
                self.pickup_saved_location_id, True), **timing_values("pickup"))],
            "delivery_stops": [dict(location_values(
                self.delivery_saved_location_id, False), **timing_values("delivery"))],
            "pallets": self.expected_skids,
            "weight_lbs": self.total_weight_lbs,
            "load_type": "ltl" if self.service_type == "ltl" else "ftl",
            "equipment_type": "reefer" if self.equipment_type == "reefer" else "dry",
            "required_temperature_c": self._canonical_required_temperature(),
            "submitted_temperature_unit": self.submitted_temperature_unit or "c",
            "commodity": self.commodity or "",
            "po_number": self.purchase_order or "",
            "bol_number": self.bol_reference or "",
            "customer_reference": self.customer_reference or so.client_order_ref or so.name,
            "instructions": self.general_notes or "",
            "requested_pickup_date": self.scheduled_pickup.date(),
            "pricing_method": "corridor" if self.booking_mode == "scheduled_ltl" else "manual",
            # Tier 1 — a confirmed Sales Order's terms are authoritative:
            # the accepted amount IS the customer price (the corridor quote
            # stays an internal/audit figure), the requested pickup date is
            # never silently rolled forward, and the pallet threshold never
            # converts the sold LTL service into Dedicated FTL pricing.
            "agreed_rate": agreed_rate,
            "agreed_rate_authoritative": True,
            "enforce_requested_pickup_date": True,
            "allow_ftl_autoupgrade": False,
            "existing_sale_order_id": so.id,
            "idempotency_key": f"sale.order:{so.id}:{self.booking_mode}",
        }, source_channel="sale_order")
        booking = service.confirm_from_internal(request)
        return self._open_booking(booking)

    def _canonical_required_temperature(self):
        """Reefer requirement converted to canonical Celsius (0°C survives);
        None for dry — dry bookings never carry a temperature."""
        if self.equipment_type != "reefer":
            return None
        try:
            from odoo.addons.prema_logistics_booking.services.temperature_service import (
                parse_temperature)
        except ImportError:  # prema_logistics_booking not loaded yet
            return self.required_temperature_c or None
        return parse_temperature(
            self.required_temperature_c, unit=self.submitted_temperature_unit or "c")

    @staticmethod
    def _open_booking(booking):
        return {
            "type": "ir.actions.act_window",
            "name": _("Booking"),
            "res_model": "logistics.booking",
            "res_id": booking.id,
            "view_mode": "form",
            "target": "current",
        }

from odoo import api, fields, models

QUOTE_STATE = [
    ("new", "New"),
    ("reviewing", "Reviewing"),
    ("quoted", "Quoted"),
    ("accepted", "Accepted"),
    ("declined", "Declined"),
    ("converted", "Converted to Booking"),
]


class LogisticsCustomQuote(models.Model):
    """Quote request for shipments outside automated pricing — manual review."""
    _name = "logistics.custom.quote"
    _description = "Custom Quote Request"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(string="Quote #", readonly=True, copy=False, default="New")
    partner_id = fields.Many2one("res.partner", string="Customer", index=True)
    contact_name = fields.Char(string="Contact Name")
    contact_email = fields.Char(string="Email")
    contact_phone = fields.Char(string="Phone")
    company_name = fields.Char(string="Company")

    # Shipment
    pickup_postal_code = fields.Char(string="Pickup Postal Code")
    pickup_address = fields.Text(string="Pickup Address")
    delivery_postal_code = fields.Char(string="Delivery Postal Code")
    delivery_address = fields.Text(string="Delivery Address")
    pallets = fields.Integer(default=1)
    weight_lbs = fields.Float(string="Weight (lbs)")
    temperature_mode = fields.Selection(
        [("dry", "Dry"), ("chilled", "Chilled"), ("frozen", "Frozen")],
        default="dry",
    )
    commodity = fields.Char(string="Commodity")
    requested_pickup_date = fields.Date(string="Requested Pickup")
    notes = fields.Text(string="Notes")

    # Resolution
    state = fields.Selection(QUOTE_STATE, default="new", tracking=True)
    quoted_price = fields.Float(string="Quoted Price")
    internal_notes = fields.Text(string="Internal Notes")
    reason_code = fields.Char(string="Reason", help="Why this required manual quoting.")

    # Resolution fields
    resolved_fsa_pickup = fields.Char(string="Resolved Pickup FSA")
    resolved_fsa_delivery = fields.Char(string="Resolved Delivery FSA")
    resolved_region_pickup = fields.Many2one("logistics.region")
    resolved_region_delivery = fields.Many2one("logistics.region")
    routing_strategy = fields.Char()

    # Converted booking
    booking_id = fields.Many2one("logistics.booking", string="Converted Booking", readonly=True)
    estimator_id = fields.Many2one("premafirm.rate.estimator", string="Estimator", readonly=True)

    # Source
    source = fields.Selection([
        ("website", "Website"),
        ("phone", "Phone"),
        ("email", "Email"),
        ("internal", "Internal"),
    ], default="website")

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("logistics.custom.quote") or "CQ-0001"
        return super().create(vals_list)

    def action_start_review(self):
        self.state = "reviewing"

    def action_quote(self):
        self.state = "quoted"

    def action_accept(self):
        self.state = "accepted"

    def action_decline(self):
        self.state = "declined"

    def action_convert_to_booking(self):
        """Convert accepted quote into a real booking using the canonical
        BookingOrchestrationService. Idempotent — returns existing booking
        if already converted."""
        self.ensure_one()
        if not self.quoted_price:
            return

        # Idempotency: return existing booking
        if self.booking_id:
            return

        try:
            from ..services.booking_orchestration_service import BookingOrchestrationService
            svc = BookingOrchestrationService(self.env)

            norm = svc.normalize_request({
                "partner_id": self.partner_id.id,
                "pickup_stops": [{
                    "postal_code": self.resolved_fsa_pickup or "",
                    "formatted_address": self.pickup_address or "",
                }],
                "delivery_stops": [{
                    "postal_code": self.resolved_fsa_delivery or "",
                    "formatted_address": self.delivery_address or "",
                }],
                "pallets": self.pallets,
                "weight_lbs": self.weight_lbs,
                "commodity": self.commodity or "",
                "equipment_type": "reefer" if self.temperature_mode in ("reefer", "chilled", "frozen") else "dry",
                "pricing_method": "manual",
                "agreed_rate": self.quoted_price,
                "custom_quote_id": self.id,
                "idempotency_key": f"custom_quote:{self.id}",
            }, source_channel="custom_quote")

            booking = svc.confirm_from_internal(norm, skip_invoice=False)
            self.booking_id = booking.id
            self.state = "converted"
        except ImportError:
            # Fallback for when orchestration service is not available
            booking = self.env["logistics.booking"].sudo().create({
                "partner_id": self.partner_id.id,
                "pickup_address": self.pickup_address or "",
                "delivery_address": self.delivery_address or "",
                "pickup_fsa_id": self.env["logistics.fsa"].search(
                    [("fsa", "=", self.resolved_fsa_pickup)], limit=1).id or False,
                "delivery_fsa_id": self.env["logistics.fsa"].search(
                    [("fsa", "=", self.resolved_fsa_delivery)], limit=1).id or False,
                "calculated_price": self.quoted_price,
                "pallets": self.pallets,
                "weight_lbs": self.weight_lbs,
                "temperature_mode": self.temperature_mode,
                "shipment_type": "ltl",
                "state": "confirmed",
                "source_channel": "custom_quote",
                "idempotency_key": f"custom_quote:{self.id}",
                "line_ids": [(0, 0, {
                    "description": self.commodity or "Custom Quote Shipment",
                    "pallets": self.pallets,
                    "weight_lbs": self.weight_lbs,
                })],
            })
            booking._apply_tax_decision()
            booking._create_dispatch_job()
            booking._create_draft_invoice()
            self.booking_id = booking.id
            self.state = "converted"

    def action_open_estimator(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Prema AI Estimator",
            "res_model": "premafirm.rate.estimator",
            "view_mode": "form",
            "target": "current",
            "context": {
                "default_origin_address": self.pickup_address,
                "default_destination_address": self.delivery_address,
                "default_load_pallets": self.pallets,
                "default_load_weight_lbs": self.weight_lbs,
                "default_partner_id": self.partner_id.id,
            },
        }

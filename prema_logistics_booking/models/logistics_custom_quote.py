import datetime

from odoo import _, api, fields, models

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
        [("dry", "Dry"), ("reefer", "Reefer")],
        default="dry",
    )
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        help="Required for Reefer quotes. 0°C is a valid value.",
    )
    load_type = fields.Selection([("ltl", "LTL"), ("ftl", "FTL")], default="ltl")
    departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Assigned Departure",
        help="Exact truck departure this quote will ride on when converted "
             "to a booking. Required to convert — a custom quote has no "
             "automated Rate Plan route to resolve one from.",
    )
    commodity = fields.Char(string="Commodity")
    requested_pickup_date = fields.Date(string="Requested Pickup")
    notes = fields.Text(string="Notes")

    # Resolution
    state = fields.Selection(QUOTE_STATE, default="new", tracking=True)
    quoted_price = fields.Float(string="Quoted Price")
    # ── Manual / negotiated sell price audit ─────────────────────────
    # quoted_price is the FINAL price offered — the customer-facing number
    # (the printed quotation shows only this). system_calculated_price
    # preserves the original pricing-engine result and the manual_price_*
    # fields the audit trail. Staff may edit quoted_price until the quote
    # is converted; a reason is required when it differs from the system
    # price (a reset back to the system price exempts).
    system_calculated_price = fields.Float(
        string="System Calculated Price", readonly=True,
        help="Original pricing-engine sell price — audit only, never shown "
             "to the customer.")
    manual_price_override = fields.Boolean(
        string="Manual Price Override", readonly=True, copy=False)
    manual_price_adjustment = fields.Float(
        string="Manual Price Adjustment", readonly=True,
        compute="_compute_manual_price_adjustment",
        help="Quoted price minus system calculated price "
             "(negative = discount).")
    manual_price_reason = fields.Char(string="Manual Price Reason")
    manual_price_changed_by = fields.Many2one(
        "res.users", string="Price Changed By", readonly=True)
    manual_price_changed_at = fields.Datetime(
        string="Price Changed At", readonly=True)
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
    currency_id = fields.Many2one("res.currency", related="company_id.currency_id", readonly=True)
    quotation_valid_until = fields.Date(
        string="Quotation Valid Until",
        compute="_compute_quotation_valid_until",
        help="30 days after the quote was created — shown on the printed quotation.",
    )

    @api.depends("create_date")
    def _compute_quotation_valid_until(self):
        for record in self:
            base = record.create_date or fields.Datetime.now()
            record.quotation_valid_until = (base + datetime.timedelta(days=30)).date()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("logistics.custom.quote") or "CQ-0001"
        return super().create(vals_list)

    @api.depends("quoted_price", "system_calculated_price")
    def _compute_manual_price_adjustment(self):
        for rec in self:
            rec.manual_price_adjustment = round(
                (rec.quoted_price or 0.0) - (rec.system_calculated_price or 0.0), 2)

    def write(self, vals):
        """Audit every manual quoted-price change on a non-converted quote.

        - quoted_price is the FINAL price offered — the customer-facing
          number. The first manual change on a legacy quote (no system
          price stored) captures the pre-change price as its system
          reference.
        - A reason is required whenever the new price differs from the
          system calculated price — except a reset back to the system
          price, which clears the reason.
        - Once converted, quoted_price is frozen (never silently
          rewritten — adjust the booking instead).
        """
        changed = []
        for rec in self:
            if "quoted_price" in vals and vals.get("quoted_price") is not None \
                    and round(float(vals.get("quoted_price")), 2) != round(rec.quoted_price or 0.0, 2):
                new_price = float(vals.get("quoted_price"))
                old_price = rec.quoted_price or 0.0
                if not rec.system_calculated_price:
                    vals["system_calculated_price"] = old_price
                system = vals.get("system_calculated_price", rec.system_calculated_price) or old_price
                if rec.state == "converted":
                    raise UserError(_(
                        "This quotation is already converted to a booking — "
                        "the final quoted price is frozen. Adjust the "
                        "booking's customer sell price instead."))
                if round(new_price, 2) != round(system, 2) \
                        and not (vals.get("manual_price_reason") or rec.manual_price_reason):
                    raise UserError(_(
                        "A Manual Price Reason is required when the quoted "
                        "price differs from the system calculated price."))
                changed.append((rec, old_price, new_price, system))
        result = super().write(vals)
        for rec, old_price, new_price, system in changed:
            rec.write({
                "manual_price_override": round(new_price, 2) != round(system, 2),
                "manual_price_changed_by": self.env.user.id,
                "manual_price_changed_at": fields.Datetime.now(),
            })
            rec.message_post(body=self.env["logistics.booking"]._sell_price_audit_message(
                old_price, new_price, rec.manual_price_reason or "",
                self.env.user.name or ""))
        return result

    def action_reset_price(self):
        """Convenience reset — quoted price back to the system price; the
        reason is then no longer required."""
        self.ensure_one()
        if not self.system_calculated_price:
            raise UserError(_("No system calculated price is stored for this quote."))
        self.write({
            "quoted_price": self.system_calculated_price,
            "manual_price_reason": "",
        })
        return True

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
        from odoo.exceptions import UserError
        if not self.quoted_price:
            return

        # Idempotency: return existing booking
        if self.booking_id:
            return

        if not self.departure_id:
            raise UserError(_(
                "Assign an exact departure before converting this custom "
                "quote to a booking — a custom quote has no automated route "
                "to resolve one from, and a booking may never be confirmed "
                "without a real, capacity-validated departure."
            ))

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
            "load_type": self.load_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": self.required_temperature_c if self.temperature_mode == "reefer" else None,
            "pricing_method": "manual",
            "agreed_rate": self.quoted_price,
            "departure_id": self.departure_id.id,
            "custom_quote_id": self.id,
            "idempotency_key": f"custom_quote:{self.id}",
        }, source_channel="custom_quote")

        booking = svc.confirm_from_internal(
            norm,
            skip_invoice=False,
            # The FINAL quoted price becomes the booking's customer sell
            # price (revenue authority); the system price and the reason
            # travel along for the permanent audit trail.
            sell_price_override=self.quoted_price,
            sell_price_override_reason=self.manual_price_reason or "",
            sell_price_override_by=self.manual_price_changed_by.id or False,
            system_calculated_price=self.system_calculated_price,
        )
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

    def action_print_quotation(self):
        """Print the PDF quotation document for this quote."""
        self.ensure_one()
        return self.env.ref(
            "prema_logistics_booking.action_report_logistics_quotation"
        ).report_action(self)

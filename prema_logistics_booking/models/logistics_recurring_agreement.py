"""Recurring shipment agreements — contract LTL service with committed capacity."""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from odoo import api, fields, models

FREQUENCY = [
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 Weeks"),
    ("monthly", "Monthly"),
]

STATES = [
    ("draft", "Draft"),
    ("quoted", "Quoted"),
    ("active", "Active"),
    ("paused", "Paused"),
    ("expired", "Expired"),
    ("cancelled", "Cancelled"),
]


class LogisticsRecurringAgreement(models.Model):
    _name = "logistics.recurring.agreement"
    _description = "Recurring Shipment Agreement"
    _order = "create_date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(compute="_compute_name", store=True)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True)

    # Shipment
    pickup_fsa_id = fields.Many2one("logistics.fsa", string="Pickup FSA")
    delivery_fsa_id = fields.Many2one("logistics.fsa", string="Delivery FSA")
    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float(string="Weight per Shipment (lbs)", default=500.0)
    temperature_mode = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")], default="dry")
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        help="Required for Reefer agreements. 0°C is a valid value.",
    )
    load_type = fields.Selection([("ltl", "LTL"), ("ftl", "FTL")], default="ltl")
    commodity = fields.Char()

    # Schedule
    frequency = fields.Selection(FREQUENCY, default="weekly", required=True)
    preferred_weekday = fields.Integer(string="Preferred Delivery Day", default=1,
                                        help="0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri")
    start_date = fields.Date(required=True, default=fields.Date.context_today)
    end_date = fields.Date(required=True)
    contract_months = fields.Integer(compute="_compute_contract_months", store=True)

    # Pricing
    rate_per_shipment = fields.Float(string="Rate per Shipment")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)

    # Status
    state = fields.Selection(STATES, default="draft", tracking=True)
    active = fields.Boolean(default=True)
    next_shipment_date = fields.Date(compute="_compute_next_shipment", store=True)

    # Route commitment
    route_run_id = fields.Many2one(
        "logistics.route.run", string="Preferred Route Run [DEPRECATED]",
        help="DEPRECATED — use departure_id instead."
    )
    departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Preferred Departure",
        help="The scheduled corridor departure this recurring agreement is committed to."
    )
    corridor_id = fields.Many2one(
        "logistics.corridor", string="Preferred Corridor",
        related="departure_id.corridor_id", store=True,
    )
    committed_pallets = fields.Integer(default=0)
    committed_weight_lbs = fields.Float(default=0.0)

    # Bookings
    booking_ids = fields.One2many("logistics.booking", "recurring_agreement_id", string="Bookings")
    booking_count = fields.Integer(compute="_compute_booking_count")

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.depends("partner_id", "pickup_fsa_id", "delivery_fsa_id", "frequency")
    def _compute_name(self):
        for rec in self:
            parts = [rec.partner_id.name or "Customer",
                     rec.pickup_fsa_id.fsa if rec.pickup_fsa_id else "",
                     "→",
                     rec.delivery_fsa_id.fsa if rec.delivery_fsa_id else "",
                     dict(FREQUENCY).get(rec.frequency, "")]
            rec.name = " ".join(p for p in parts if p)

    @api.depends("start_date", "end_date")
    def _compute_contract_months(self):
        for rec in self:
            if rec.start_date and rec.end_date:
                delta = relativedelta(rec.end_date, rec.start_date)
                rec.contract_months = delta.months + delta.years * 12
            else:
                rec.contract_months = 0

    @api.depends("start_date", "frequency", "preferred_weekday")
    def _compute_next_shipment(self):
        for rec in self:
            if not rec.start_date:
                rec.next_shipment_date = False
                continue
            today = date.today()
            # Find the next occurrence from today
            if rec.frequency == "weekly":
                days_ahead = rec.preferred_weekday - today.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                rec.next_shipment_date = today + timedelta(days=days_ahead)
            elif rec.frequency == "biweekly":
                days_ahead = rec.preferred_weekday - today.weekday()
                if days_ahead < 0:
                    days_ahead += 14
                rec.next_shipment_date = today + timedelta(days=days_ahead)
            elif rec.frequency == "monthly":
                rec.next_shipment_date = today.replace(day=1) + relativedelta(months=1)

    @api.depends("booking_ids")
    def _compute_booking_count(self):
        for rec in self:
            rec.booking_count = len(rec.booking_ids)

    def action_activate(self):
        self.state = "active"

    def action_pause(self):
        self.state = "paused"

    def action_expire(self):
        self.state = "expired"
        self.active = False

    def action_cancel(self):
        self.state = "cancelled"
        self.active = False

    def action_generate_future_occurrences(self):
        """Calculate and return projected shipment dates for this agreement."""
        self.ensure_one()
        if not self.start_date or not self.end_date:
            return []

        dates = []
        current = self.start_date
        if self.preferred_weekday is not None and current.weekday() != self.preferred_weekday:
            days_ahead = self.preferred_weekday - current.weekday()
            if days_ahead < 0:
                days_ahead += 7
            current = current + timedelta(days=days_ahead)

        while current <= self.end_date:
            dates.append(current)
            if self.frequency == "weekly":
                current += timedelta(days=7)
            elif self.frequency == "biweekly":
                current += timedelta(days=14)
            elif self.frequency == "monthly":
                current += relativedelta(months=1)

        return dates

    def action_calculate_rate(self):
        """Auto-calculate rate using standard pricing engine."""
        self.ensure_one()
        if not self.pickup_fsa_id or not self.delivery_fsa_id:
            return
        from ..services.pricing_service import PricingService
        result = PricingService(self.env).calculate(
            self.pickup_fsa_id, self.delivery_fsa_id,
            self.load_type, self.temperature_mode, self.pallets, self.weight_lbs,
            partner=self.partner_id,
            required_temperature_c=self.required_temperature_c if self.temperature_mode == "reefer" else None,
        )
        if result.available:
            self.rate_per_shipment = result.calculated_price

    @api.model
    def _generate_due_bookings(self):
        """Cron: generate bookings for active recurring agreements due today.
        Idempotent — skips agreements that already have a booking for today's date.
        Uses canonical BookingOrchestrationService (V4)."""
        today = date.today()
        agreements = self.search([
            ("state", "=", "active"), ("active", "=", True),
            ("start_date", "<=", today), ("end_date", ">=", today),
        ])
        created = 0
        failures = 0
        _logger = __import__("logging").getLogger(__name__)

        for agreement in agreements:
            # Determine next shipment date based on frequency + preferred_weekday
            next_date = agreement._compute_next_shipment_date(today)
            if not next_date:
                continue
            # Only generate if the next date is today or within 1 day (grace window)
            if abs((next_date - today).days) > 1:
                continue

            # Idempotency: check for existing booking for this agreement + date
            existing = self.env["logistics.booking"].sudo().search([
                ("recurring_agreement_id", "=", agreement.id),
                ("pickup_date", "=", next_date),
            ], limit=1)
            if existing:
                continue

            try:
                # Use canonical BookingOrchestrationService
                from ..services.booking_orchestration_service import BookingOrchestrationService

                svc = BookingOrchestrationService(self.env)
                idempotency_key = f"recurring:{agreement.id}:{next_date.isoformat()}"

                norm = svc.normalize_request({
                    "partner_id": agreement.partner_id.id,
                    "pickup_stops": [{
                        "postal_code": agreement.pickup_fsa_id.fsa if agreement.pickup_fsa_id else "",
                    }],
                    "delivery_stops": [{
                        "postal_code": agreement.delivery_fsa_id.fsa if agreement.delivery_fsa_id else "",
                    }],
                    "pallets": agreement.pallets,
                    "weight_lbs": agreement.weight_lbs,
                    "load_type": agreement.load_type,
                    "equipment_type": agreement.temperature_mode,
                    "required_temperature_c": (
                        agreement.required_temperature_c
                        if agreement.temperature_mode == "reefer" else None
                    ),
                    "requested_pickup_date": next_date,
                    "pricing_method": "rate_plan",
                    "recurring_agreement_id": agreement.id,
                    "idempotency_key": idempotency_key,
                }, source_channel="recurring")

                booking = svc.confirm_from_internal(norm, skip_invoice=False)
                if booking:
                    created += 1
                    # Advance next shipment date only after successful confirmation
                    agreement._advance_next_shipment_date()

            except Exception as e:
                failures += 1
                _logger.warning(
                    "Recurring agreement %s (ID:%s): failed to generate booking: %s",
                    agreement.name, agreement.id, e,
                )
                # Create activity for administrator to review
                if hasattr(agreement, "activity_schedule"):
                    try:
                        agreement.activity_schedule(
                            "mail.mail_activity_data_warning",
                            summary="Booking generation failed",
                            note=f"Failed to generate booking for {next_date}: {e}",
                            user_id=agreement.create_uid.id or self.env.user.id,
                        )
                    except Exception:
                        pass

        if failures:
            _logger.warning(
                "Recurring bookings cron: %d created, %d failed",
                created, failures,
            )

        return created

    def _advance_next_shipment_date(self):
        """Advance to the next shipment date after successful booking generation."""
        self.ensure_one()
        if not self.preferred_weekday:
            return
        today = date.today()
        if self.frequency == "weekly":
            self.next_shipment_date = today + timedelta(days=7)
        elif self.frequency == "biweekly":
            self.next_shipment_date = today + timedelta(days=14)
        else:
            self.next_shipment_date = today + timedelta(days=7)

    def _compute_next_shipment_date(self, today):
        """Compute the next shipment date for this agreement from a given reference date."""
        if not self.start_date:
            return None
        if self.preferred_weekday is None:
            return None
        days_ahead = self.preferred_weekday - today.weekday()
        if days_ahead < 0:
            if self.frequency == "weekly":
                days_ahead += 7
            elif self.frequency == "biweekly":
                days_ahead += 14
            else:
                days_ahead += 7  # default to weekly
        return today + timedelta(days=days_ahead)

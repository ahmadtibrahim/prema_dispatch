"""Customer recurring agreements with up to ten independent route jobs."""

import logging
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

FREQUENCY = [
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 Weeks"),
    ("monthly", "Monthly"),
]
WEEKDAYS = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
    ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"),
]
MONTHLY_WEEKS = [
    ("1", "First"), ("2", "Second"), ("3", "Third"),
    ("4", "Fourth"), ("last", "Last"),
]
STATES = [
    ("draft", "Draft"), ("quoted", "Quoted"), ("active", "Active"),
    ("paused", "Paused"), ("expired", "Expired"), ("cancelled", "Cancelled"),
]


class LogisticsRecurringAgreement(models.Model):
    _name = "logistics.recurring.agreement"
    _description = "Recurring Shipment Agreement"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin",
                "logistics.temperature.mixin"]

    name = fields.Char(compute="_compute_name", store=True)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True, tracking=True)
    agreement_reference = fields.Char(string="Customer Contract / PO")
    account_manager_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    billing_notes = fields.Text()
    service_notes = fields.Text()
    start_date = fields.Date(required=True, default=fields.Date.context_today)
    end_date = fields.Date(required=True)
    contract_months = fields.Integer(compute="_compute_contract_months", store=True)
    state = fields.Selection(STATES, default="draft", tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    job_ids = fields.One2many(
        "logistics.recurring.job", "agreement_id", string="Recurring Jobs",
        copy=True,
    )
    job_count = fields.Integer(compute="_compute_counts")
    booking_ids = fields.One2many("logistics.booking", "recurring_agreement_id", string="Bookings")
    booking_count = fields.Integer(compute="_compute_counts")

    pallets = fields.Integer(default=1)
    weight_lbs = fields.Float(default=500.0)
    temperature_mode = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")
    required_temperature_c = fields.Float()
    load_type = fields.Selection([("ltl", "LTL"), ("ftl", "FTL")], default="ltl")
    commodity = fields.Char()
    frequency = fields.Selection(FREQUENCY, default="weekly")
    preferred_weekday = fields.Integer(default=0)
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    committed_pallets = fields.Integer(default=0)
    committed_weight_lbs = fields.Float(default=0.0)

    @api.depends("partner_id", "agreement_reference")
    def _compute_name(self):
        for agreement in self:
            agreement.name = " — ".join(filter(None, (
                agreement.partner_id.name or _("Customer"),
                agreement.agreement_reference or _("Recurring LTL"),
            )))

    @api.depends("start_date", "end_date")
    def _compute_contract_months(self):
        for agreement in self:
            if agreement.start_date and agreement.end_date:
                delta = relativedelta(agreement.end_date, agreement.start_date)
                agreement.contract_months = delta.years * 12 + delta.months
            else:
                agreement.contract_months = 0

    @api.depends("job_ids", "booking_ids")
    def _compute_counts(self):
        for agreement in self:
            agreement.job_count = len(agreement.job_ids)
            agreement.booking_count = len(agreement.booking_ids)

    @api.constrains("job_ids")
    def _check_maximum_jobs(self):
        for agreement in self:
            if len(agreement.job_ids) > 10:
                raise ValidationError(_("A customer agreement may contain at most 10 recurring jobs."))

    @api.constrains("start_date", "end_date")
    def _check_dates(self):
        for agreement in self:
            if agreement.start_date and agreement.end_date and agreement.end_date < agreement.start_date:
                raise ValidationError(_("End Date must be on or after Start Date."))

    def action_activate(self):
        for agreement in self:
            today = fields.Date.context_today(agreement)
            if agreement.end_date and agreement.end_date < today:
                raise UserError(_("This agreement has already ended. Extend the End Date before activating it."))
            if not agreement.job_ids.filtered("active"):
                raise UserError(_("Add at least one recurring job before activating the agreement."))
            jobs = agreement.job_ids.filtered("active")
            jobs._validate_activation()
            for job in jobs:
                job.next_shipment_date = job._next_occurrence(today)
            agreement.write({"state": "active", "active": True})

    def action_pause(self):
        self.write({"state": "paused"})

    def action_expire(self):
        self.write({"state": "expired", "active": False})

    def action_cancel(self):
        self.write({"state": "cancelled", "active": False})

    @api.model
    def _generate_due_bookings(self):
        today = fields.Date.context_today(self)
        expired = self.search([
            ("state", "=", "active"), ("end_date", "<", today),
        ])
        if expired:
            expired.write({"state": "expired", "active": False})
        jobs = self.env["logistics.recurring.job"].search([
            ("active", "=", True),
            ("auto_generate", "=", True),
            ("agreement_id.state", "=", "active"),
            ("agreement_id.active", "=", True),
        ])
        created = 0
        for job in jobs:
            try:
                if job._generate_if_due():
                    created += 1
            except Exception as exc:
                _logger.exception("Recurring job %s failed", job.display_name)
                job.agreement_id.activity_schedule(
                    "mail.mail_activity_data_warning",
                    summary=_("Recurring booking failed"),
                    note=_("Job %(job)s could not generate: %(error)s", job=job.display_name, error=exc),
                    user_id=job.agreement_id.account_manager_id.id or self.env.user.id,
                )
        return created


class LogisticsRecurringJob(models.Model):
    _name = "logistics.recurring.job"
    _description = "Recurring Agreement Job"
    _order = "agreement_id, sequence, id"
    _inherit = ["logistics.temperature.mixin"]

    agreement_id = fields.Many2one(
        "logistics.recurring.agreement", required=True, ondelete="cascade", index=True,
    )
    partner_id = fields.Many2one(related="agreement_id.partner_id", store=True, index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, default="Recurring Route")
    active = fields.Boolean(default=True)

    pickup_kind = fields.Selection([
        ("location", "Google-Verified Address"), ("region", "Region"),
    ], default="location", required=True)
    pickup_location_id = fields.Many2one(
        "prema.dispatch.location", string="Pickup Address",
        domain="[('active','=',True), '|', ('partner_id','=',partner_id), ('partner_id','=',False)]",
    )
    pickup_region_id = fields.Many2one(
        "logistics.region", string="Pickup Region",
        domain=[("is_official_ltl_region", "=", True)],
    )
    delivery_kind = fields.Selection([
        ("location", "Google-Verified Address"), ("region", "Region"),
    ], default="location", required=True)
    delivery_location_id = fields.Many2one(
        "prema.dispatch.location", string="Delivery Address",
        domain="[('active','=',True), '|', ('partner_id','=',partner_id), ('partner_id','=',False)]",
    )
    delivery_region_id = fields.Many2one(
        "logistics.region", string="Delivery Region",
        domain=[("is_official_ltl_region", "=", True)],
    )

    frequency = fields.Selection(FREQUENCY, default="weekly", required=True)
    preferred_weekday = fields.Selection(WEEKDAYS, default="0", required=True)
    monthly_week = fields.Selection(
        MONTHLY_WEEKS, default="1", required=True,
        help="For monthly jobs, choose which occurrence of the weekday to use.",
    )
    pickup_time_from = fields.Float(default=8.0)
    pickup_time_to = fields.Float(default=12.0)
    delivery_time_from = fields.Float(default=8.0)
    delivery_time_to = fields.Float(default=17.0)
    next_shipment_date = fields.Date(copy=False)
    auto_generate = fields.Boolean(
        default=False,
        help="Create the booking automatically. This requires Google-verified pickup and delivery addresses.",
    )

    load_type = fields.Selection([("ltl", "LTL"), ("ftl", "FTL")], default="ltl", required=True)
    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float(string="Weight per Shipment (lb)", default=500.0, required=True)
    temperature_mode = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")
    required_temperature_c = fields.Float(string="Required Temperature °C")
    temperature_confirmed = fields.Boolean(
        string="Temperature Confirmed",
        help="Confirms that the numeric Reefer temperature was intentionally entered; 0°C is valid.",
    )
    commodity = fields.Char()
    stackable = fields.Boolean(default=True)
    hazmat = fields.Boolean()
    food_grade = fields.Boolean()
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    po_number = fields.Char()
    customer_reference = fields.Char()
    instructions = fields.Text()

    booking_ids = fields.One2many("logistics.booking", "recurring_job_id", string="Generated Bookings")
    booking_count = fields.Integer(compute="_compute_booking_count")

    @api.depends("booking_ids")
    def _compute_booking_count(self):
        for job in self:
            job.booking_count = len(job.booking_ids)

    @api.model_create_multi
    def create(self, values_list):
        jobs = super().create(values_list)
        for agreement in jobs.mapped("agreement_id"):
            if len(agreement.job_ids) > 10:
                raise ValidationError(_("A customer agreement may contain at most 10 recurring jobs."))
        return jobs

    def write(self, values):
        previous = self.mapped("agreement_id")
        result = super().write(values)
        for agreement in previous | self.mapped("agreement_id"):
            if len(agreement.job_ids) > 10:
                raise ValidationError(_("A customer agreement may contain at most 10 recurring jobs."))
        return result

    @api.constrains(
        "pickup_kind", "pickup_location_id", "pickup_region_id",
        "delivery_kind", "delivery_location_id", "delivery_region_id",
        "pallets", "weight_lbs", "temperature_mode",
        "pickup_time_from", "pickup_time_to", "delivery_time_from", "delivery_time_to",
    )
    def _check_job_values(self):
        for job in self:
            if job.pickup_kind == "location" and not job.pickup_location_id:
                raise ValidationError(_("Choose a Pickup Address."))
            if job.pickup_kind == "region" and not job.pickup_region_id:
                raise ValidationError(_("Choose a Pickup Region."))
            if job.delivery_kind == "location" and not job.delivery_location_id:
                raise ValidationError(_("Choose a Delivery Address."))
            if job.delivery_kind == "region" and not job.delivery_region_id:
                raise ValidationError(_("Choose a Delivery Region."))
            if job.pallets < 1 or job.weight_lbs < 0:
                raise ValidationError(_("Pallets must be at least 1 and weight cannot be negative."))
            if job.pickup_time_to < job.pickup_time_from:
                raise ValidationError(_("Pickup window end must be after its start."))
            if job.delivery_time_to < job.delivery_time_from:
                raise ValidationError(_("Delivery window end must be after its start."))

    def _validate_activation(self):
        for job in self:
            if job.auto_generate and (job.pickup_kind != "location" or job.delivery_kind != "location"):
                raise ValidationError(_(
                    "Job %(job)s uses a Region. Region-only jobs can be tracked in the agreement, "
                    "but automatic booking needs exact Google-verified addresses.", job=job.name,
                ))
            for label, location in (
                (_("Pickup"), job.pickup_location_id if job.pickup_kind == "location" else False),
                (_("Delivery"), job.delivery_location_id if job.delivery_kind == "location" else False),
            ):
                if location and (not location.google_verified or not location.google_place_id):
                    raise ValidationError(_(
                        "%(label)s address for %(job)s must be selected and verified through Google Places.",
                        label=label, job=job.name,
                    ))
            if job.temperature_mode == "reefer" and not job.temperature_confirmed:
                raise ValidationError(_("Enter and confirm the numeric temperature for Reefer job %s.") % job.name)
        return True

    def _next_occurrence(self, reference=None):
        self.ensure_one()
        reference = fields.Date.to_date(reference or fields.Date.context_today(self))
        start = max(reference, self.agreement_id.start_date or reference)
        weekday = int(self.preferred_weekday)
        if self.frequency == "weekly":
            return start + timedelta(days=(weekday - start.weekday()) % 7)

        if self.frequency == "biweekly":
            agreement_start = self.agreement_id.start_date or start
            anchor = agreement_start + timedelta(
                days=(weekday - agreement_start.weekday()) % 7,
            )
            if start <= anchor:
                return anchor
            weeks_since = ((start - anchor).days + 6) // 7
            if weeks_since % 2:
                weeks_since += 1
            return anchor + timedelta(weeks=weeks_since)

        # Monthly jobs use an explicit first/second/third/fourth/last weekday
        # rule. Search month by month so a paused or overdue agreement catches
        # up to its next future occurrence instead of becoming permanently stuck.
        month_cursor = start.replace(day=1)
        while True:
            if self.monthly_week == "last":
                next_month = month_cursor + relativedelta(months=1)
                candidate = next_month - timedelta(days=1)
                candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
            else:
                candidate = month_cursor + timedelta(
                    days=(weekday - month_cursor.weekday()) % 7,
                    weeks=int(self.monthly_week or "1") - 1,
                )
            if candidate >= start:
                return candidate
            month_cursor += relativedelta(months=1)

    def _location_stop_values(self, location, pickup=True):
        self.ensure_one()
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
            "pallet_count": self.pallets if pickup else 0,
            "weight_lbs": self.weight_lbs if pickup else 0.0,
            "requested_time_from": self.pickup_time_from if pickup else self.delivery_time_from,
            "requested_time_to": self.pickup_time_to if pickup else self.delivery_time_to,
            "liftgate_required": self.liftgate_pickup if pickup else self.liftgate_delivery,
            "instructions": self.instructions or "",
        }

    def _generate_if_due(self):
        self.ensure_one()
        self._validate_activation()
        today = fields.Date.context_today(self)
        due = self.next_shipment_date or self._next_occurrence(today)
        if due < today:
            due = self._next_occurrence(today)
        if due > self.agreement_id.end_date:
            self.next_shipment_date = False
            return False
        if due != today:
            if self.next_shipment_date != due:
                self.next_shipment_date = due
            return False
        existing = self.env["logistics.booking"].sudo().search([
            ("recurring_job_id", "=", self.id), ("pickup_date", "=", due),
            ("state", "!=", "cancelled"),
        ], limit=1)
        if existing:
            return False

        from ..services.booking_orchestration_service import BookingOrchestrationService
        service = BookingOrchestrationService(self.env)
        request = service.normalize_request({
            "partner_id": self.partner_id.id,
            "pickup_stops": [self._location_stop_values(self.pickup_location_id, pickup=True)],
            "delivery_stops": [self._location_stop_values(self.delivery_location_id, pickup=False)],
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "load_type": self.load_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": self.required_temperature_c if self.temperature_mode == "reefer" else None,
            "commodity": self.commodity or "",
            "stackable": self.stackable,
            "hazmat": self.hazmat,
            "food_grade": self.food_grade,
            "liftgate_pickup": self.liftgate_pickup,
            "liftgate_delivery": self.liftgate_delivery,
            "appointment": self.appointment,
            "residential": self.residential,
            "po_number": self.po_number or "",
            "customer_reference": self.customer_reference or "",
            "instructions": self.instructions or "",
            "requested_pickup_date": due,
            "pricing_method": "corridor",
            "recurring_agreement_id": self.agreement_id.id,
            "recurring_job_id": self.id,
            "idempotency_key": f"recurring-job:{self.id}:{due.isoformat()}",
        }, source_channel="recurring")
        booking = service.confirm_from_internal(request)
        self.next_shipment_date = self._next_occurrence(due + timedelta(days=1))
        return booking

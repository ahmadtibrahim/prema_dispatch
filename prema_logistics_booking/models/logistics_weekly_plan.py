"""Phase 7 — Weekly Capacity Planner (spec §40-§47).

Operational truck/day allocation for recurring customers. Three models:

- logistics.weekly.plan          — one planning week (container + settings)
- logistics.weekly.plan.day      — truck × date grid cell (capacity board)
- logistics.weekly.plan.reservation — draggable recurring card (one occurrence)

The commercial contract stays in logistics.recurring.agreement / .job (§43).
Each reservation is an OPERATIONAL snapshot of one occurrence — moving it,
resizing it, or cancelling it never touches the agreement (§45). Actual
logistics.booking records are generated a configurable number of days before
departure (§44) through the same BookingOrchestrationService the recurring
job generator uses, with idempotency keys and recurring_job_id set so the
two generators can never duplicate.

Capacity authority: planned reservations flow into
CapacityEngine.compute_departure_peak (see services/capacity_engine.py), so
VehicleCapacityService.reserved_pallets / evaluate / for_pickup_date /
check_and_reserve all see them — the booking portal cannot overbook the
positions a weekly plan has committed (§42) and no capacity number is ever
hardcoded (§47, §48).
"""

import logging
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

WEEKDAY_LABELS = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

PLAN_STATES = [
    ("draft", "Draft"),
    ("confirmed", "Confirmed"),
]

RESERVATION_STATES = [
    ("planned", "Planned"),
    ("booking_generated", "Booking Generated"),
    ("cancelled", "Cancelled"),
]


class LogisticsWeeklyPlan(models.Model):
    _name = "logistics.weekly.plan"
    _description = "Weekly Capacity Plan"
    _order = "week_start desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(compute="_compute_name", store=True)
    week_start = fields.Date(
        required=True, index=True,
        default=lambda self: self._default_week_start(),
        help="Monday of the planning week. Cards and the capacity grid cover "
             "the seven days starting here.",
    )
    state = fields.Selection(PLAN_STATES, default="draft", tracking=True)
    generate_days_before = fields.Integer(
        string="Generate Booking (Days Before)", default=5,
        help="Actual logistics.booking records are created this many days "
             "before each planned occurrence's pickup date (spec §44).",
    )
    corridor_ids = fields.Many2many(
        "logistics.corridor", string="Service Routes",
        help="Corridors whose trucks/days this plan allocates. Leave empty to "
             "include every active corridor's default truck on its operating days.",
    )
    day_ids = fields.One2many("logistics.weekly.plan.day", "plan_id",
                              string="Truck-Day Cells")
    reservation_ids = fields.One2many(
        "logistics.weekly.plan.reservation", "plan_id",
        string="Recurring Cards")
    day_count = fields.Integer(compute="_compute_counts")
    reservation_count = fields.Integer(compute="_compute_counts")
    committed_pallets = fields.Integer(compute="_compute_counts",
                                       string="Committed Pallets")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company",
                                 default=lambda self: self.env.company)

    @api.model
    def _default_week_start(self):
        today = date.today()
        return today - timedelta(days=today.weekday())

    @api.depends("week_start")
    def _compute_name(self):
        for plan in self:
            if plan.week_start:
                plan.name = "Week of %s" % plan.week_start.strftime("%b %d, %Y")
            else:
                plan.name = "Weekly Capacity Plan"

    @api.depends("day_ids", "reservation_ids")
    def _compute_counts(self):
        for plan in self:
            plan.day_count = len(plan.day_ids)
            active = plan.reservation_ids.filtered(
                lambda r: r.state != "cancelled")
            plan.reservation_count = len(active)
            plan.committed_pallets = sum(active.mapped("pallets"))

    @api.constrains("week_start")
    def _check_week_start(self):
        for plan in self:
            if plan.week_start and plan.week_start.weekday() != 0:
                raise ValidationError(_("Week Start must be a Monday."))

    @api.constrains("generate_days_before")
    def _check_generate_days(self):
        for plan in self:
            if plan.generate_days_before < 0:
                raise ValidationError(
                    _("Generate-days-before cannot be negative."))

    # ── week assembly ──────────────────────────────────────────────

    def _operating_days(self):
        """(plan_date, vehicle, corridor) tuples for this week — one per
        corridor operating day that has a truck to run it (deduplicated by
        vehicle+date).

        The truck is the corridor's own scheduled departure on that date
        (the normal production state — the horizon generator already made
        the row), falling back to the default truck when the corridor has
        no row yet but its default is free that day. A corridor whose
        default truck is occupied elsewhere gets no cell: its horizon row
        is deliberately unassigned and cannot accept bookings.
        """
        self.ensure_one()
        start = self.week_start
        corridors = self.corridor_ids
        if not corridors:
            corridors = self.env["logistics.corridor"].search(
                [("active", "=", True)])
        Departure = self.env["logistics.corridor.departure"]
        days = []
        seen = set()
        for corridor in corridors:
            for weekday in corridor._operating_weekdays():
                day_date = start + timedelta(days=weekday)
                scheduled = Departure.search([
                    ("corridor_id", "=", corridor.id),
                    ("departure_date", "=", day_date),
                    ("status", "!=", "cancelled"),
                    ("active", "=", True),
                    ("vehicle_id", "!=", False),
                ], order="departure_time, id", limit=1)
                vehicle = (scheduled.vehicle_id
                           if scheduled else corridor._default_vehicle_for_date(
                               day_date))
                if not vehicle:
                    continue
                key = (vehicle.id, day_date)
                if key in seen:
                    continue
                seen.add(key)
                days.append((day_date, vehicle, corridor))
        return days

    def action_refresh_grid(self):
        """Idempotently (re)create the truck × day grid cells for the week."""
        self.ensure_one()
        Day = self.env["logistics.weekly.plan.day"]
        for day_date, vehicle, corridor in self._operating_days():
            existing = Day.search([
                ("plan_id", "=", self.id),
                ("plan_date", "=", day_date),
                ("vehicle_id", "=", vehicle.id),
            ], limit=1)
            if not existing:
                Day.create({
                    "plan_id": self.id,
                    "plan_date": day_date,
                    "vehicle_id": vehicle.id,
                    "corridor_id": corridor.id,
                })
        return True

    def action_generate_week(self):
        """Create the week's recurring cards and capacity grid, idempotently.

        One reservation per occurrence of every active recurring job whose
        agreement is active and still running through the end of the week.
        Cards start unassigned (no truck); the dispatcher assigns trucks and
        days by dragging on the card boards (§41).
        """
        self.ensure_one()
        self.action_refresh_grid()
        Job = self.env["logistics.recurring.job"]
        Reservation = self.env["logistics.weekly.plan.reservation"]
        start = self.week_start
        end = start + timedelta(days=6)
        jobs = Job.search([
            ("active", "=", True),
            ("agreement_id.state", "=", "active"),
            ("agreement_id.active", "=", True),
            ("agreement_id.end_date", ">=", end),
        ])
        created = 0
        for job in jobs:
            occurrence = job._next_occurrence(start)
            while occurrence <= end:
                existing = Reservation.search([
                    ("plan_id", "=", self.id),
                    ("recurring_job_id", "=", job.id),
                    ("plan_date", "=", occurrence),
                ], limit=1)
                if not existing:
                    Reservation.create({
                        "plan_id": self.id,
                        "recurring_job_id": job.id,
                        "plan_date": occurrence,
                    })
                    created += 1
                occurrence = job._next_occurrence(occurrence + timedelta(days=1))
        if created:
            self.message_post(body=_(
                "Week plan assembled: %(cards)d recurring card(s), "
                "%(days)d truck-day cell(s).",
                cards=self.reservation_count, days=self.day_count,
            ))
        return True

    def action_confirm(self):
        self.write({"state": "confirmed"})

    # ── booking generation (§44) ──────────────────────────────────

    def action_generate_due_bookings(self):
        """Generate logistics.booking records for every due occurrence."""
        self.ensure_one()
        generated = skipped = blocked = 0
        for reservation in self.reservation_ids:
            result = reservation._generate_booking()
            if result is True:
                generated += 1
            elif result is None:
                blocked += 1
            else:
                skipped += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Weekly Plan Bookings"),
                "message": _(
                    "%(generated)d generated, %(blocked)d blocked, "
                    "%(skipped)d not due or already booked.",
                    generated=generated, blocked=blocked, skipped=skipped,
                ),
                "type": "success" if generated else "warning",
                "sticky": False,
            },
        }

    @api.model
    def _generate_due_bookings(self):
        """Cron entry — every active plan generates its due bookings."""
        generated = 0
        for plan in self.search([("active", "=", True)]):
            for reservation in plan.reservation_ids:
                if reservation._generate_booking() is True:
                    generated += 1
        if generated:
            _logger.info("Weekly capacity plans: %d booking(s) generated",
                         generated)
        return generated

    # ── board shortcuts ───────────────────────────────────────────

    def action_open_cards_by_day(self):
        self.ensure_one()
        return self._cards_window(
            "weekly_plan_reservation_kanban_day",
            "Recurring Cards — By Day",
            [("plan_id", "=", self.id), ("state", "!=", "cancelled")],
        )

    def action_open_cards_by_truck(self):
        self.ensure_one()
        return self._cards_window(
            "weekly_plan_reservation_kanban_truck",
            "Recurring Cards — By Truck",
            [("plan_id", "=", self.id), ("state", "!=", "cancelled")],
        )

    def _cards_window(self, view_xmlid, title, domain):
        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": "logistics.weekly.plan.reservation",
            "view_mode": "kanban,list,form",
            "view_id": self.env.ref(
                "prema_logistics_booking.%s" % view_xmlid).id,
            "domain": domain,
            "context": {"default_plan_id": self.id},
        }


class LogisticsWeeklyPlanDay(models.Model):
    _name = "logistics.weekly.plan.day"
    _description = "Weekly Plan Truck-Day Cell"
    _order = "plan_date, vehicle_id"

    plan_id = fields.Many2one("logistics.weekly.plan", required=True,
                              ondelete="cascade", index=True)
    plan_date = fields.Date(required=True, index=True)
    weekday_label = fields.Char(compute="_compute_weekday_label")
    vehicle_id = fields.Many2one("fleet.vehicle", required=True,
                                 string="Truck", index=True)
    corridor_id = fields.Many2one("logistics.corridor",
                                  string="Service Route")
    capacity_pallets = fields.Integer(compute="_compute_capacity",
                                      string="Capacity")
    committed_pallets = fields.Integer(compute="_compute_capacity",
                                       string="Committed")
    available_pallets = fields.Integer(compute="_compute_capacity",
                                       string="Available")
    is_holiday = fields.Boolean(compute="_compute_holiday",
                                string="Holiday / Blackout")

    @api.depends("plan_date")
    def _compute_weekday_label(self):
        for cell in self:
            cell.weekday_label = (
                WEEKDAY_LABELS[cell.plan_date.weekday()]
                if cell.plan_date else "")

    @api.depends("plan_date", "vehicle_id", "plan_id.reservation_ids")
    def _compute_capacity(self):
        from ..services.vehicle_capacity_service import VehicleCapacityService
        service = VehicleCapacityService(self.env)
        for cell in self:
            maximum = service.maximum_capacity(cell.vehicle_id)
            cell.capacity_pallets = maximum
            departures = self.env["logistics.corridor.departure"].search([
                ("departure_date", "=", cell.plan_date),
                ("vehicle_id", "=", cell.vehicle_id.id),
                ("status", "!=", "cancelled"),
            ])
            if departures:
                # The departure peak already includes weekly-plan
                # reservations (CapacityEngine) plus confirmed bookings —
                # the same number the booking portal sees.
                committed = max(
                    service.reserved_pallets(d) for d in departures)
            else:
                cards = self.env["logistics.weekly.plan.reservation"].search([
                    ("plan_id", "=", cell.plan_id.id),
                    ("plan_date", "=", cell.plan_date),
                    ("vehicle_id", "=", cell.vehicle_id.id),
                    ("state", "=", "planned"),
                ])
                committed = sum(cards.mapped("pallets"))
            cell.committed_pallets = committed
            cell.available_pallets = max(maximum - committed, 0)

    @api.depends("plan_date", "plan_id.corridor_ids", "corridor_id")
    def _compute_holiday(self):
        for cell in self:
            excluded = set()
            for corridor in (cell.plan_id.corridor_ids | cell.corridor_id):
                excluded |= corridor._excluded_departure_dates()
            cell.is_holiday = bool(cell.plan_date in excluded)


class LogisticsWeeklyPlanReservation(models.Model):
    _name = "logistics.weekly.plan.reservation"
    _description = "Weekly Plan Recurring Card (one occurrence)"
    _order = "plan_date, id"
    _inherit = ["mail.thread", "logistics.temperature.mixin"]

    plan_id = fields.Many2one("logistics.weekly.plan", required=True,
                              ondelete="cascade", index=True)
    recurring_job_id = fields.Many2one(
        "logistics.recurring.job", required=True,
        ondelete="restrict", index=True)
    agreement_id = fields.Many2one(
        related="recurring_job_id.agreement_id", store=True, index=True)
    partner_id = fields.Many2one(
        related="recurring_job_id.partner_id", store=True, index=True)
    name = fields.Char(compute="_compute_name", store=True)
    plan_date = fields.Date(required=True, index=True)
    weekday_label = fields.Char(compute="_compute_weekday_label")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck", index=True)
    corridor_departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Departure",
        help="Optional anchor: this card reserves capacity on THIS departure "
             "only. When empty, it reserves capacity on any scheduled "
             "departure for the assigned truck on the plan date.")
    pallets = fields.Integer(required=True)
    weight_lbs = fields.Float(required=True)
    load_type = fields.Selection(
        [("ltl", "LTL"), ("ftl", "FTL")], default="ltl", required=True)
    temperature_mode = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")], default="dry")
    required_temperature_c = fields.Float(string="Required Temperature °C")
    state = fields.Selection(RESERVATION_STATES, default="planned",
                             tracking=True)
    booking_id = fields.Many2one("logistics.booking",
                                 string="Generated Booking",
                                 readonly=True, copy=False)
    booking_number = fields.Char(related="booking_id.booking_number",
                                 readonly=True)
    is_due = fields.Boolean(compute="_compute_is_due",
                            string="Due for Booking")
    is_blocked = fields.Boolean(compute="_compute_blocked",
                                string="Blocked Date")
    blocked_reason = fields.Char(compute="_compute_blocked")
    change_note = fields.Char(
        string="One-Off Change Note",
        help="Why this occurrence differs from the recurring job (moved / "
             "resized / cancelled). The agreement itself is never modified "
             "(spec §45).")
    active = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            job = self.env["logistics.recurring.job"].browse(
                vals.get("recurring_job_id"))
            if job:
                vals.setdefault("pallets", job.pallets)
                vals.setdefault("weight_lbs", job.weight_lbs)
                vals.setdefault("load_type", job.load_type)
                vals.setdefault("temperature_mode", job.temperature_mode)
                vals.setdefault("required_temperature_c",
                                job.required_temperature_c)
        return super().create(vals_list)

    @api.depends("recurring_job_id.name", "plan_date", "pallets")
    def _compute_name(self):
        for rec in self:
            job_name = rec.recurring_job_id.name or "Recurring route"
            rec.name = "%s · %s · %d pallet(s)" % (
                job_name, rec.plan_date, rec.pallets)

    @api.depends("plan_date")
    def _compute_weekday_label(self):
        for rec in self:
            rec.weekday_label = (
                WEEKDAY_LABELS[rec.plan_date.weekday()]
                if rec.plan_date else "")

    @api.depends("plan_date", "plan_id.generate_days_before", "state")
    def _compute_is_due(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.state != "planned" or not rec.plan_date or not rec.plan_id:
                rec.is_due = False
                continue
            due_date = rec.plan_date - timedelta(
                days=rec.plan_id.generate_days_before or 0)
            rec.is_due = rec.plan_date >= today and due_date <= today

    @api.depends("plan_date", "plan_id.corridor_ids",
                 "corridor_departure_id.status")
    def _compute_blocked(self):
        today = fields.Date.context_today(self)
        for rec in self:
            reasons = []
            excluded = set()
            for corridor in rec.plan_id.corridor_ids:
                excluded |= corridor._excluded_departure_dates()
            if rec.plan_date in excluded:
                reasons.append(_(
                    "Holiday / blackout date on the plan's service routes"))
            if rec.corridor_departure_id and \
                    rec.corridor_departure_id.status == "cancelled":
                reasons.append(_("Anchored departure was cancelled"))
            if rec.plan_date and rec.plan_date < today:
                reasons.append(_("Date has passed"))
            rec.is_blocked = bool(reasons)
            rec.blocked_reason = "; ".join(reasons)

    # ── occurrence actions (§45 / §46) ────────────────────────────

    def action_attach_departure(self):
        """Anchor this card to the scheduled departure matching truck+date."""
        for rec in self:
            if not rec.vehicle_id or not rec.plan_date:
                raise UserError(
                    _("Assign a truck and plan date to the card first."))
            departure = self.env["logistics.corridor.departure"].search([
                ("departure_date", "=", rec.plan_date),
                ("vehicle_id", "=", rec.vehicle_id.id),
                ("status", "=", "scheduled"),
                ("active", "=", True),
            ], order="departure_time, id", limit=1)
            if not departure:
                raise UserError(_(
                    "No scheduled departure for %(truck)s on %(date)s. "
                    "Generate the corridor's departure horizon first.",
                    truck=rec.vehicle_id.name, date=rec.plan_date))
            rec.corridor_departure_id = departure.id
        return True

    def action_generate_booking(self):
        """Force-generate the logistics.booking for this occurrence now."""
        self.ensure_one()
        if self._generate_booking(force=True):
            return True
        if self.is_blocked:
            raise UserError(
                _("This occurrence is on a blocked date: %s")
                % self.blocked_reason)
        if self.state != "planned":
            raise UserError(
                _("A booking already exists for this occurrence."))
        raise UserError(_(
            "The booking is not due yet — bookings generate %(days)d day(s) "
            "before pickup.",
            days=self.plan_id.generate_days_before or 0))

    def action_cancel_occurrence(self):
        """One-off cancellation (§45/§46): this occurrence only; the
        agreement and the next occurrence continue normally."""
        for rec in self:
            if rec.state == "booking_generated":
                raise UserError(_(
                    "A booking already exists for %(card)s. Cancel the "
                    "booking itself, not the card.",
                    card=rec.name))
            if not rec.change_note:
                rec.change_note = _("Cancelled for this occurrence only")
            rec.state = "cancelled"
        return True

    def action_reactivate(self):
        self.write({"state": "planned", "change_note": False})

    # ── booking generation (§44) ──────────────────────────────────

    def _generate_booking(self, force=False):
        """Generate the actual logistics.booking for this occurrence.

        Returns True when a booking was created, False when the occurrence
        is not due / already booked / cancelled, None when it is blocked
        (holiday, blackout, cancelled departure, date passed — §46).
        """
        self.ensure_one()
        if self.state != "planned":
            return False
        if self.is_blocked:
            return None
        if not force and not self.is_due:
            return False
        job = self.recurring_job_id
        # Shared business key with the recurring job generator
        # (logistics.recurring.job._generate_if_due): (recurring_job_id,
        # pickup_date, state != cancelled). Whichever generator runs first
        # wins; the other deduplicates here and in the job generator.
        existing = self.env["logistics.booking"].sudo().search([
            ("recurring_job_id", "=", job.id),
            ("pickup_date", "=", self.plan_date),
            ("state", "!=", "cancelled"),
        ], limit=1)
        if existing:
            self.write({"booking_id": existing.id,
                        "state": "booking_generated"})
            return False
        if not job.pickup_location_id or not job.delivery_location_id:
            _logger.warning(
                "Weekly plan card %s has no Google-verified pickup/delivery "
                "address — booking not generated.", self.name)
            return False

        from ..services.booking_orchestration_service import (
            BookingOrchestrationService)
        service = BookingOrchestrationService(self.env)
        request = service.normalize_request({
            "partner_id": self.partner_id.id,
            "pickup_stops": [job._location_stop_values(
                job.pickup_location_id, pickup=True)],
            "delivery_stops": [job._location_stop_values(
                job.delivery_location_id, pickup=False)],
            # One-off values from the CARD, not the job (§45).
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "load_type": self.load_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": (
                self.required_temperature_c
                if self.temperature_mode == "reefer" else None),
            "commodity": job.commodity or "",
            "stackable": job.stackable,
            "hazmat": job.hazmat,
            "food_grade": job.food_grade,
            "liftgate_pickup": job.liftgate_pickup,
            "liftgate_delivery": job.liftgate_delivery,
            "appointment": job.appointment,
            "residential": job.residential,
            "po_number": job.po_number or "",
            "customer_reference": job.customer_reference or "",
            "instructions": job.instructions or "",
            "requested_pickup_date": self.plan_date,
            "pricing_method": "corridor",
            "recurring_agreement_id": self.agreement_id.id,
            "recurring_job_id": job.id,
            "idempotency_key": "weekly-plan:%d:%s" % (
                self.id, self.plan_date.isoformat()),
        }, source_channel="recurring")
        booking = service.confirm_from_internal(request)
        self.write({"booking_id": booking.id,
                    "state": "booking_generated"})
        # Keep the job's own generator in step so it never re-attempts (or
        # double-books) this date.
        job.next_shipment_date = job._next_occurrence(
            self.plan_date + timedelta(days=1))
        return True

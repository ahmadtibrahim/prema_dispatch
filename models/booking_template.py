import pytz
from datetime import date, datetime, timedelta

from odoo import api, exceptions, fields, models


class PremaDispatchBookingTemplate(models.Model):
    _name = "prema.dispatch.booking.template"
    _description = "Recurring Booking Template"
    _inherit = ["mail.thread"]
    _order = "name asc"

    name = fields.Char(string="Template Name", required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", required=True, tracking=True,
    )
    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order", ondelete="set null", tracking=True,
        help="Optional: link to a Sales Order to inherit on auto-generated bookings.",
    )
    service_type = fields.Selection([
        ("local",     "Local"),
        ("ltl",       "LTL"),
        ("ftl",       "FTL"),
        ("dedicated", "Dedicated"),
        ("other",     "Other"),
    ], default="ltl", tracking=True)
    equipment_type = fields.Selection([
        ("dry",     "Dry Van"),
        ("reefer",  "Reefer"),
        ("flatbed", "Flatbed"),
        ("other",   "Other"),
    ], default="dry", tracking=True)
    recurrence_type = fields.Selection([
        ("weekly",   "Weekly"),
        ("biweekly", "Biweekly (Every 2 Weeks)"),
        ("monthly",  "Monthly (Every 4 Weeks)"),
    ], string="Recurrence", default="weekly", required=True, tracking=True)
    day_of_week = fields.Selection([
        ("0", "Monday"),
        ("1", "Tuesday"),
        ("2", "Wednesday"),
        ("3", "Thursday"),
        ("4", "Friday"),
        ("5", "Saturday"),
        ("6", "Sunday"),
    ], string="Day of Week", required=True, tracking=True)
    advance_days = fields.Integer(
        string="Advance Days", default=3,
        help="Create the booking this many days before the scheduled delivery day.",
    )
    default_pickup_hour = fields.Float(
        string="Default Pickup Time",
        default=8.0,
        help="Default pickup hour for bookings from this template (e.g. 8.0 = 8:00 AM, 13.5 = 1:30 PM). "
             "Applied to ad-hoc bookings for this customer when no specific time is given.",
    )
    approximate_skids = fields.Integer(string="Approx. Skids")
    commodity = fields.Char(string="Commodity")
    requires_reefer = fields.Boolean(string="Reefer Required", tracking=True)
    requires_liftgate = fields.Boolean(string="Liftgate Required", tracking=True)
    notify_user_id = fields.Many2one(
        "res.users", string="Notify User",
        help="This user is notified in the job chatter when a booking is auto-generated.",
    )
    notes = fields.Text(string="Internal Notes")

    booking_ids = fields.One2many(
        "prema.dispatch.job", "template_id", string="Generated Bookings",
    )
    booking_count = fields.Integer(compute="_compute_booking_count")
    last_generated_date = fields.Date(
        string="Last Generated Date", readonly=True, copy=False,
        help="Delivery date of the most recently auto-generated booking. "
             "Prevents duplicate creation on repeated cron runs.",
    )

    @api.depends("booking_ids")
    def _compute_booking_count(self):
        for tmpl in self:
            tmpl.booking_count = len(tmpl.booking_ids)

    def action_view_bookings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Generated Bookings",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("template_id", "=", self.id)],
            "context": {"default_template_id": self.id},
        }

    def action_generate_now(self):
        """Manually create the next booking from this template (bypasses schedule check)."""
        self.ensure_one()
        today = self.env["prema.dispatch.job"]._user_today()
        target_dow = int(self.day_of_week)
        days_until = (target_dow - today.weekday()) % 7 or 7
        target_date = today + timedelta(days=days_until)
        if self.last_generated_date == target_date:
            raise exceptions.UserError(
                f"A booking for {target_date} was already generated from this template."
            )
        job = self._create_booking(target_date)
        return {
            "type": "ir.actions.act_window",
            "name": "Generated Booking",
            "res_model": "prema.dispatch.job",
            "res_id": job.id,
            "view_mode": "form",
        }

    @api.model
    def _generate_due_bookings(self):
        """Called daily by cron. Creates bookings whose trigger date equals today."""
        today = self.env["prema.dispatch.job"]._user_today()
        for tmpl in self.search([("active", "=", True)]):
            target_dow = int(tmpl.day_of_week)
            days_until = (target_dow - today.weekday()) % 7
            target_date = today + timedelta(days=days_until)

            # Biweekly / monthly: skip if last booking was too recent
            if tmpl.last_generated_date:
                delta = (target_date - tmpl.last_generated_date).days
                if tmpl.recurrence_type == "biweekly" and delta < 14:
                    continue
                if tmpl.recurrence_type == "monthly" and delta < 28:
                    continue

            trigger_date = target_date - timedelta(days=tmpl.advance_days or 3)
            if today != trigger_date:
                continue
            if tmpl.last_generated_date == target_date:
                continue

            tmpl._create_booking(target_date)

    def _pickup_datetime_utc(self, target_date):
        """Convert default_pickup_hour on target_date to UTC naive datetime."""
        tz_name = self.env.context.get("tz") or self.env.user.tz or "UTC"
        try:
            user_tz = pytz.timezone(tz_name)
        except Exception:
            user_tz = pytz.utc
        hour = int(self.default_pickup_hour or 8)
        minute = int(round((self.default_pickup_hour - hour) * 60))
        local_dt = user_tz.localize(
            datetime(target_date.year, target_date.month, target_date.day, hour, minute)
        )
        return local_dt.astimezone(pytz.utc).replace(tzinfo=None)

    def _create_booking(self, target_date):
        """Create a dispatch job from this template for the given delivery date."""
        self.ensure_one()
        draft_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        job = self.env["prema.dispatch.job"].create({
            "template_id": self.id,
            "partner_id": self.partner_id.id,
            "sale_order_id": self.sale_order_id.id if self.sale_order_id else False,
            "stage_id": draft_stage.id if draft_stage else False,
            "service_type": self.service_type,
            "equipment_type": self.equipment_type,
            "requires_reefer": self.requires_reefer,
            "requires_liftgate": self.requires_liftgate,
            "approximate_skids": self.approximate_skids,
            "commodity": self.commodity or "",
            "requested_delivery_date": target_date,
            "scheduled_pickup": self._pickup_datetime_utc(target_date),
            "source_model": "prema.dispatch.booking.template",
            "source_res_id": self.id,
            "internal_notes": self.notes or "",
        })
        self.last_generated_date = target_date
        if self.notify_user_id:
            job.message_post(
                body=f"Auto-generated from template: {self.name}. "
                     f"Review and assign truck before dispatching.",
                partner_ids=[self.notify_user_id.partner_id.id],
            )
        return job

from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    dispatch_job_ids = fields.One2many(
        "prema.dispatch.job", "invoice_id",
        string="Prema Dispatch Jobs", copy=False,
    )
    dispatch_job_count = fields.Integer(
        compute="_compute_dispatch_job_count", store=True,
    )
    dispatch_status = fields.Selection([
        ("none", "No Dispatch"),
        ("draft", "Draft"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("pod_ready", "POD Ready"),
        ("posted", "Posted"),
        ("error", "Error"),
    ], compute="_compute_dispatch_status", store=True, string="Dispatch Status")
    dispatch_auto_posted = fields.Boolean(
        string="Auto-Posted via Dispatch", readonly=True, copy=False,
    )

    @api.depends("dispatch_job_ids")
    def _compute_dispatch_job_count(self):
        for move in self:
            move.dispatch_job_count = len(move.dispatch_job_ids)

    @api.depends(
        "dispatch_job_ids.stage_id.is_completed",
        "dispatch_job_ids.stage_id.is_cancelled",
        "dispatch_job_ids.pod_complete",
        "dispatch_job_ids.auto_posted_invoice",
        "state",
    )
    def _compute_dispatch_status(self):
        for move in self:
            jobs = move.dispatch_job_ids
            if not jobs:
                move.dispatch_status = "none"
            elif move.state == "posted" and any(job.auto_posted_invoice for job in jobs):
                move.dispatch_status = "posted"
            elif all(job.stage_id and job.stage_id.is_completed for job in jobs):
                move.dispatch_status = (
                    "pod_ready" if all(job.pod_complete for job in jobs)
                    else "completed"
                )
            elif any(job.auto_post_error for job in jobs):
                move.dispatch_status = "error"
            elif all(
                not job.stage_id or job.stage_id.stage_type in ("draft", False)
                for job in jobs
            ):
                move.dispatch_status = "draft"
            else:
                move.dispatch_status = "in_progress"

    def action_open_dispatch_jobs_prema(self):
        """Open the Planner operation cards linked to this invoice."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Jobs",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("invoice_id", "=", self.id)],
            "context": {"default_invoice_id": self.id},
        }

    def action_book_load(self):
        """Open the one canonical invoice-to-booking workflow.

        A repeated click opens the existing booking or its Planner cards. It
        never creates a second load and never promotes an estimator directly
        into Dispatch Planner.
        """
        self.ensure_one()
        booking = getattr(self, "logistics_booking_id", False)
        if booking:
            return {
                "type": "ir.actions.act_window",
                "name": "Booking",
                "res_model": "logistics.booking",
                "res_id": booking.id,
                "view_mode": "form",
                "target": "current",
            }
        if self.dispatch_job_ids:
            return self.action_open_dispatch_jobs_prema()
        return {
            "type": "ir.actions.act_window",
            "name": "Book Load",
            "res_model": "prema.dispatch.book.load.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id, "default_move_id": self.id},
        }

    def _resolve_scheduled_pickup(self, estimator=None):
        """Return a safe local 08:00 default for the invoice booking wizard."""
        import pytz
        from datetime import time as dtime, timedelta

        self.ensure_one()
        job_model = self.env["prema.dispatch.job"]
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        invoice_date = self.invoice_date or self.date or job_model._user_today(user_tz)
        if estimator and estimator.scheduled_at:
            estimator_date = job_model._local_date_of(estimator.scheduled_at, user_tz)
            if estimator_date and estimator_date >= invoice_date - timedelta(days=3):
                return estimator.scheduled_at
        return job_model._local_date_time_to_utc(invoice_date, dtime(8, 0), user_tz)

    # Stable compatibility names for old server actions/RPC clients. Both
    # delegate to the verified wizard; neither contains a legacy create path.
    def _do_action_book_load(self):
        return self.action_book_load()

    def action_create_or_open_booking(self):
        return self.action_book_load()

    action_create_dispatch_job = action_book_load

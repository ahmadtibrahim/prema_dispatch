from odoo import _, api, fields, models
from odoo.exceptions import UserError


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
        ("pod_ready", "Ready for Dispatch Review"),
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
        "state",
    )
    def _compute_dispatch_status(self):
        for move in self:
            jobs = move.dispatch_job_ids
            if not jobs:
                move.dispatch_status = "none"
            elif move.state == "posted":
                move.dispatch_status = "posted"
            elif all(job.stage_id and job.stage_id.is_completed for job in jobs):
                # Every dispatch job complete: pod_ready = READY FOR
                # DISPATCH REVIEW. The invoice still needs a dispatcher's
                # manual approval (action_approve_dispatch_review) — the
                # review gate is never bypassed automatically.
                move.dispatch_status = (
                    "pod_ready" if all(job.pod_complete for job in jobs)
                    else "completed"
                )
            elif all(
                not job.stage_id or job.stage_id.stage_type in ("draft", False)
                for job in jobs
            ):
                move.dispatch_status = "draft"
            else:
                move.dispatch_status = "in_progress"

    def action_approve_dispatch_review(self):
        """Dispatcher's manual approval of a READY FOR DISPATCH REVIEW
        invoice: review the evidence, then post the invoice. This is the
        ONLY path that posts a dispatch-linked invoice — completion never
        auto-posts it.

        Access: the group gate below runs FIRST, in the caller's own
        environment, before any account.move record is touched — a driver
        or plain internal user gets a clean UserError here even though
        they have no accounting access. After the gate, the posting runs
        with sudo: the dispatcher's authorization is this method's gate
        (they are not necessarily an accounting user), mirroring the
        dispatch-manager pattern in fleet_vehicle.py."""
        self.ensure_one()
        user = self.env.user
        inv = self.sudo()
        if inv.dispatch_job_ids:
            if not (
                user.has_group("prema_dispatch.group_dispatch_manager")
                or user.has_group("prema_dispatch.group_dispatcher")
                or user.has_group("base.group_system")
            ):
                raise UserError(_(
                    "Only a Dispatcher or Dispatch Manager can approve a "
                    "dispatch invoice for posting."))
            if not all(
                j.stage_id and j.stage_id.is_completed for j in inv.dispatch_job_ids
            ):
                raise UserError(_(
                    "Not all dispatch jobs on this invoice are completed — "
                    "cannot approve yet."))
        if inv.state != "draft":
            raise UserError(_("Only draft invoices can be approved and posted."))
        inv.action_post()
        inv.message_post(
            body=(
                f"<b>Approved for posting</b> by {user.name} after "
                f"dispatch review. Evidence reviewed; invoice posted manually."
            )
        )
        return True

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

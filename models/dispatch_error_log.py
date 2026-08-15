"""Central error/diagnostic log for Prema Dispatch UAT and operations."""

from odoo import _, api, fields, models


class PremaDispatchErrorLog(models.Model):
    _name = "prema.dispatch.error.log"
    _description = "Prema Dispatch Error Log"
    _order = "id desc"
    _rec_name = "reference"

    reference = fields.Char(
        string="Reference", readonly=True, copy=False, index=True,
        default=lambda self: self._generate_reference(),
    )
    timestamp = fields.Datetime(
        default=fields.Datetime.now, readonly=True, index=True,
    )
    severity = fields.Selection([
        ("info", "INFO — Minor / UI"),
        ("warning", "WARNING — Unexpected but completed"),
        ("error", "ERROR — Action failed"),
        ("critical", "CRITICAL — Data integrity / production"),
    ], default="error", required=True, index=True)

    source = fields.Selection([
        ("portal", "Customer Portal"),
        ("booking_board", "Booking Board"),
        ("dispatch_planner", "Dispatch Planner"),
        ("dispatch_job", "Dispatch Job"),
        ("load_plan", "Load Plan"),
        ("driver_app", "Driver App"),
        ("pricing", "Pricing / Get Price"),
        ("routing", "Routing"),
        ("capacity", "Capacity Engine"),
        ("invoice", "Invoice"),
        ("autocomplete", "Autocomplete"),
        ("saved_locations", "Saved Locations"),
        ("booking_confirm", "Booking Confirmation"),
        ("api", "API / Integration"),
        ("other", "Other"),
    ], default="other", index=True)

    action = fields.Char(string="Operation / Action")
    user_id = fields.Many2one("res.users", string="User", default=lambda self: self.env.user)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    url = fields.Char(string="URL / Route")

    model_name = fields.Char(string="Model")
    record_id = fields.Integer(string="Record ID")
    record_name = fields.Char(string="Record Name")

    booking_id = fields.Many2one("logistics.booking", string="Booking")
    # Computed (not related=) — logistics.booking lives in
    # prema_logistics_booking, which depends on THIS module, so a related
    # field here breaks the registry when prema_dispatch is upgraded alone.
    booking_number = fields.Char(string="Booking #", compute="_compute_booking_number")
    dispatch_job_id = fields.Many2one("prema.dispatch.job", string="Dispatch Job")
    dispatch_job_number = fields.Char(related="dispatch_job_id.name", string="Job #")
    invoice_id = fields.Many2one("account.move", string="Invoice")

    error_type = fields.Char(string="Error Type")
    error_message = fields.Text(string="Error Message")
    traceback = fields.Text(string="Traceback")

    request_summary = fields.Text(string="Request / Input Summary")
    response_summary = fields.Text(string="Response Summary")

    is_frontend = fields.Boolean(string="Browser / Frontend Error")
    is_server = fields.Boolean(string="Server Error", default=True)

    state = fields.Selection([
        ("new", "New"),
        ("investigating", "Investigating"),
        ("resolved", "Resolved"),
        ("ignored", "Ignored"),
    ], default="new", required=True, index=True)

    resolution_notes = fields.Text(string="Resolution Notes")
    resolved_by = fields.Many2one("res.users", string="Resolved By")
    resolved_date = fields.Datetime(string="Resolved Date")

    uat_notes = fields.Text(string="UAT Notes")

    active = fields.Boolean(default=True)

    def _generate_reference(self):
        today = fields.Date.today().strftime("%Y%m%d")
        seq = self.env["ir.sequence"].sudo().next_by_code("prema.dispatch.error.log") or "1"
        return f"ERR-{today}-{int(seq):04d}"

    @api.depends("booking_id")
    def _compute_booking_number(self):
        # Runtime read only (no dotted depends) — booking_number is
        # immutable once the booking is created, so re-triggering on the
        # booking record itself is unnecessary.
        for rec in self:
            rec.booking_number = rec.booking_id.booking_number or False

    @api.model
    def log_error(self, source, action, error_message, **kwargs):
        """Convenience method: create an error-log entry from anywhere.
        Accepts keyword overrides for any model field.
        """
        vals = {
            "source": source,
            "action": action,
            "error_message": error_message,
            "is_server": True,
            "state": "new",
        }
        allowed = {
            "severity", "user_id", "url", "model_name", "record_id",
            "record_name", "booking_id", "dispatch_job_id", "invoice_id",
            "error_type", "traceback", "request_summary", "response_summary",
            "is_frontend", "uat_notes",
        }
        vals.update({k: v for k, v in kwargs.items() if k in allowed and v is not None})
        return self.sudo().create(vals)

    def action_investigate(self):
        self.write({"state": "investigating"})

    def action_resolve(self):
        self.write({
            "state": "resolved",
            "resolved_by": self.env.user.id,
            "resolved_date": fields.Datetime.now(),
        })

    def action_ignore(self):
        self.write({"state": "ignored"})

    def action_reopen(self):
        self.write({"state": "new"})

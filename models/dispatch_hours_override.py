"""Authorized Saved Location hours overrides for operational stops.

A dispatcher can authorize servicing a facility outside its operating
hours (e.g. a dairy that closes at 16:00 but agreed to a 17:00 pickup).
Each override records who authorized it, when, and why — and it only
applies to the specific stop it was issued for.
"""
from odoo import api, fields, models


class PremaDispatchHoursOverride(models.Model):
    _name = "prema.dispatch.hours.override"
    _description = "Authorized Operating Hours Override"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Override", compute="_compute_name", store=True)
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Route Job", required=True,
        ondelete="cascade", index=True)
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", required=True,
        ondelete="cascade", index=True,
        domain="[('job_id', '=', job_id)]")
    reason = fields.Text(string="Reason", required=True)
    user_id = fields.Many2one(
        "res.users", string="Authorized By", required=True,
        default=lambda self: self.env.user)
    authorized_at = fields.Datetime(
        string="Authorized At", default=fields.Datetime.now, required=True)
    active = fields.Boolean(default=True)

    @api.depends("stop_id", "user_id")
    def _compute_name(self):
        for record in self:
            record.name = "Hours override — %s (%s)" % (
                record.stop_id.display_name or "?",
                record.user_id.name or "?",
            )

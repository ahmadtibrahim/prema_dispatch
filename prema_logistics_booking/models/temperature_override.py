# -*- coding: utf-8 -*-
"""Temperature override — 18-section work order §5.

An authorized dispatcher override for a job whose onboard reefer
requirements conflict (or whose setpoint must be changed for any reason).
Records the FULL original picture (selected setpoint, reason, authorizing
user, timestamp, affected pallets/bookings, original requirements) so the
conflict audit trail is never lost. The driver acknowledgment is stored
here and mirrored on the job for the Driver Home surface.
"""

import json

from odoo import _, api, fields, models


class PremaDispatchTemperatureOverride(models.Model):
    _name = "prema.dispatch.temperature.override"
    _description = "Temperature Override (dispatch-authorized)"
    _order = "id desc"

    name = fields.Char(string="Reference", readonly=True, copy=False)
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Route/Job", required=True,
        ondelete="cascade", index=True, copy=False)
    vehicle_id = fields.Many2one(
        related="job_id.vehicle_id", string="Truck", readonly=True)
    driver_id = fields.Many2one(
        related="job_id.driver_id", string="Driver", readonly=True)
    selected_setpoint_c = fields.Float(
        string="Authorized Setpoint (°C)", required=True,
        help="Canonical Celsius setpoint the driver must set the reefer to. "
             "0.0 is a valid setpoint.")
    reason = fields.Text(string="Reason", required=True)
    override_user_id = fields.Many2one(
        "res.users", string="Authorized By", required=True,
        readonly=True, copy=False)
    override_at = fields.Datetime(string="Authorized At", readonly=True, copy=False)
    state = fields.Selection([
        ("draft", "Draft"),
        ("applied", "Applied"),
        ("cancelled", "Cancelled"),
    ], default="draft", string="State")
    affected_item_ids = fields.Many2many(
        "prema.dispatch.item", string="Affected Pallets")
    affected_booking_count = fields.Integer(
        string="Affected Bookings", compute="_compute_affected_booking_count",
        store=False)
    original_requirements_json = fields.Text(
        string="Original Requirements (snapshot)", readonly=True, copy=False,
        help="JSON snapshot of every affected item's booking temperature "
             "requirements BEFORE the override — the audit trail.")
    driver_acknowledged = fields.Boolean(
        string="Driver Acknowledged", readonly=True, copy=False)
    driver_ack_at = fields.Datetime(
        string="Acknowledged At", readonly=True, copy=False)
    driver_ack_user_id = fields.Many2one(
        "res.users", string="Acknowledged By", readonly=True, copy=False)

    _sql_constraints = [
        ("setpoint_valid", "CHECK(selected_setpoint_c >= -40 AND selected_setpoint_c <= 60)",
         "Setpoint must be within -40°C..60°C."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name"):
                seq = self.env["ir.sequence"].next_by_code(
                    "prema.dispatch.temperature.override") or "TMP"
                vals["name"] = f"OVR-{seq}"
        return super().create(vals_list)

    def _compute_affected_booking_count(self):
        for rec in self:
            rec.affected_booking_count = len(
                rec.affected_item_ids.mapped("logistics_booking_id"))

    def action_driver_acknowledged(self, user_id=False):
        """Driver tapped 'Reefer setpoint acknowledged' in the app."""
        user = user_id or self.env.uid
        now = fields.Datetime.now()
        self.write({
            "driver_acknowledged": True,
            "driver_ack_at": now,
            "driver_ack_user_id": user,
        })
        if self.job_id:
            self.job_id.write({
                "reefer_acknowledged": True,
                "reefer_ack_at": now,
                "reefer_ack_user_id": user,
            })
            self.job_id._post_timeline(
                self.job_id, "temperature",
                notes=_("Driver acknowledged reefer setpoint %s",
                        f"{self.selected_setpoint_c:g} °C"),
            )
        return True

    def action_acknowledge_reefer_off(self, user_id=False):
        """Driver tapped 'Reefer switched off'."""
        user = user_id or self.env.uid
        now = fields.Datetime.now()
        if self.job_id:
            self.job_id.write({
                "reefer_off_acknowledged": True,
                "reefer_off_ack_at": now,
                "reefer_off_ack_user_id": user,
            })
            self.job_id._post_timeline(
                self.job_id, "temperature",
                notes=_("Driver acknowledged: reefer switched off"),
            )
        return True

    def action_apply(self):
        """Authorize & apply: snapshot the ORIGINAL requirements FIRST,
        record the authorizer, then drive the engine to the new setpoint.
        The engine reads THIS record as the active override — never a
        second record."""
        self.ensure_one()
        if not self.reason:
            raise models.ValidationError(
                _("A reason is required for a temperature override."))
        from odoo.addons.prema_logistics_booking.services.temperature_engine import (
            TemperatureEngine)
        # Original picture BEFORE the engine moves anything.
        affected_items = self.affected_item_ids or self.job_id.item_ids
        orig = {
            "target": self.job_id.temperature_instruction_c,
            "range_min": self.job_id.temperature_range_min_c,
            "range_max": self.job_id.temperature_range_max_c,
            "requirements": [
                {
                    "item_id": it.id,
                    "item_name": it.name,
                    "booking_id": it.logistics_booking_id.id,
                    "booking_number": it.logistics_booking_id.booking_number or "",
                    "target_c": it.logistics_booking_id.target_temperature_c,
                    "min_c": it.logistics_booking_id.minimum_temperature_c,
                    "max_c": it.logistics_booking_id.maximum_temperature_c,
                }
                for it in affected_items
                if it.logistics_booking_id
            ],
        }
        self.write({
            "state": "applied",
            "override_user_id": self.env.user.id,
            "override_at": fields.Datetime.now(),
            "original_requirements_json": json.dumps(orig),
        })
        TemperatureEngine(self.env).recalc(self.job_id)
        self.job_id._post_timeline(
            self.job_id, "temperature_override",
            notes=_("Temperature override: %(sp)s — %(reason)s (by %(user)s)",
                    sp=f"{self.selected_setpoint_c:g} °C",
                    reason=self.reason,
                    user=self.override_user_id.name or "dispatcher"),
        )
        return {"type": "ir.actions.act_window_close"}

    def action_cancel(self):
        self.write({"state": "cancelled"})
        if self.job_id:
            self.job_id._post_timeline(
                self.job_id, "temperature_override",
                notes=_("Temperature override %(name)s cancelled",
                        name=self.name),
            )
        return True

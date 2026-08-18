"""No Access / Sealed Load override (spec §22).

A documented, audited exception to the POPP requirement: when the driver
cannot enter a warehouse/DC or photograph freight, they record WHY (one of
the spec's reasons), optionally a seal number + seal photo, and the event
is timestamped with driver + GPS and posted to the job timeline. ONLY this
documented override lets Pickup Confirmation bypass the per-pallet POPP
requirement (spec §21/§23).

This is genuine new data (audit metadata with no home on the stop or item),
so a small model is justified — see spec §63.
"""
from odoo import fields, models


class PremaDispatchPoppOverride(models.Model):
    _name = "prema.dispatch.popp.override"
    _description = "POPP No Access / Sealed Load Override"
    _order = "overridden_at desc, id desc"

    REASONS = [
        ("dock_prohibited", "Driver prohibited from dock"),
        ("security_restriction", "Security restriction"),
        ("preloaded_sealed", "Preloaded sealed truck"),
        ("sealed_before_access", "Freight sealed before driver access"),
        ("policy_no_photography", "Customer policy prevents photography"),
        ("other", "Other"),
    ]

    stop_id = fields.Many2one("prema.dispatch.stop", required=True, ondelete="cascade", index=True)
    job_id = fields.Many2one(
        "prema.dispatch.job", related="stop_id.job_id", store=True, index=True)
    reason = fields.Selection(REASONS, required=True)
    reason_other = fields.Char("Other reason detail")
    seal_number = fields.Char("Seal Number", help="Optional — required for sealed loads.")
    seal_photo_id = fields.Many2one(
        "ir.attachment", string="Seal Photo", ondelete="set null",
        help="Optional photo of the seal (stamped by the driver app).")
    overridden_by = fields.Many2one("res.users", string="Overridden By", required=True, index=True)
    overridden_at = fields.Datetime(string="Overridden At", required=True)
    lat = fields.Float(digits=(16, 7), string="GPS Latitude")
    lng = fields.Float(digits=(16, 7), string="GPS Longitude")
    active = fields.Boolean(default=True)

    def _ensure_single_active(self):
        """Only ONE active override per stop — a new override supersedes an
        older one (they are still kept for audit)."""
        self.ensure_one()
        others = self.search([
            ("stop_id", "=", self.stop_id.id),
            ("active", "=", True),
        ]) - self
        if others:
            others.write({"active": False})

    def action_audit_message(self):
        """Post the audit event to the job timeline (spec §22)."""
        self.ensure_one()
        reason = dict(self.REASONS).get(self.reason, self.reason)
        if self.reason == "other" and self.reason_other:
            reason += f" — {self.reason_other}"
        msg = (
            f"POPP requirement overridden — Reason: {reason}. "
            f"Driver: {self.overridden_by.name or self.overridden_by.login} at "
            f"{self.overridden_at.strftime('%Y-%m-%d %H:%M')}."
        )
        if self.seal_number:
            msg += f" Seal number: {self.seal_number}."
        if self.lat and self.lng:
            msg += f" GPS: {self.lat:.6f}, {self.lng:.6f}."
        self.job_id.message_post(body=msg)
        return True

"""Proof-override wizard — authorize completing a stop without its
required POP/POD. Reason, user and timestamp are recorded on the stop
and an audit event is posted to the job timeline."""
from odoo import _, fields, models


class PremaDispatchProofOverrideWizard(models.TransientModel):
    _name = "prema.dispatch.proof.override.wizard"
    _description = "Authorize Proof Override"

    stop_id = fields.Many2one(
        "prema.dispatch.stop", required=True, ondelete="cascade")
    reason = fields.Text(string="Reason", required=True)

    def apply_override(self):
        self.ensure_one()
        if not self.reason.strip():
            return {"type": "ir.actions.act_window_close"}
        stop = self.stop_id
        stop.write({
            "proof_override_reason": self.reason,
            "proof_override_by": self.env.user.id,
            "proof_override_at": fields.Datetime.now(),
        })
        stop.job_id._post_timeline(
            stop.job_id, "proof_override",
            notes=_("Proof override authorized by %(user)s: %(reason)s") % {
                "user": self.env.user.name,
                "reason": self.reason,
            },
            stop=stop,
        )
        return {"type": "ir.actions.act_window_close"}

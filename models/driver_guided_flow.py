from markupsafe import Markup, escape

from odoo import api, fields, models


_DEFER_REASONS = [
    ("customer_closed", "Customer Not Open Yet"),
    ("appointment_later", "Appointment Later"),
    ("dock_unavailable", "Dock Unavailable"),
    ("long_wait", "Long Wait"),
    ("dispatcher_instructed", "Dispatcher Instructed"),
    ("other", "Other"),
]

_EXCEPTION_REASONS = [
    ("customer_closed", "Customer Closed"),
    ("refused_freight", "Freight Refused"),
    ("damaged_freight", "Damaged Freight"),
    ("short_shipment", "Short Shipment"),
    ("extra_freight", "Extra Freight"),
    ("wrong_freight", "Wrong Freight"),
    ("dock_inaccessible", "Dock Inaccessible"),
    ("long_wait", "Long Wait"),
    ("appointment_issue", "Appointment Issue"),
    ("address_issue", "Address Issue"),
    ("temperature_issue", "Temperature Issue"),
    ("other", "Other"),
]


class PremaDispatchStopGuidedFlow(models.Model):
    _inherit = "prema.dispatch.stop"

    status = fields.Selection(
        selection_add=[
            ("deferred", "Come Back Later"),
            ("exception", "Exception / Needs Resolution"),
        ],
        ondelete={"deferred": "set default", "exception": "set default"},
    )

    driver_deferred_reason = fields.Selection(_DEFER_REASONS, copy=False, tracking=True)
    driver_deferred_reason_other = fields.Char(copy=False)
    driver_deferred_until = fields.Datetime(copy=False, index=True)
    driver_deferred_at = fields.Datetime(copy=False, readonly=True)
    driver_deferred_by = fields.Many2one("res.users", copy=False, readonly=True)
    driver_deferred_original_sequence = fields.Integer(copy=False, readonly=True)

    driver_exception_reason = fields.Selection(_EXCEPTION_REASONS, copy=False, tracking=True)
    driver_exception_notes = fields.Text(copy=False)
    driver_exception_opened_at = fields.Datetime(copy=False, readonly=True)
    driver_exception_opened_by = fields.Many2one("res.users", copy=False, readonly=True)
    driver_exception_previous_status = fields.Char(copy=False, readonly=True)

    driver_sequence_override_reason = fields.Char(copy=False)
    driver_sequence_override_at = fields.Datetime(copy=False, readonly=True)
    driver_sequence_override_by = fields.Many2one("res.users", copy=False, readonly=True)


class PremaDispatchJobGuidedFlow(models.Model):
    _inherit = "prema.dispatch.job"

    @api.model
    def driver_update_stop(self, stop_id, action, data=None):
        """Handle guided Driver App transitions before core stop actions.

        Deferred and Exception are deliberately *open* operational states.
        They never mean delivered, picked up, skipped, cancelled, or
        commercially completed.
        """
        guided_actions = {"defer", "resume_deferred", "report_problem", "resume_exception", "make_next"}
        if action not in guided_actions:
            return super().driver_update_stop(stop_id, action, data)

        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access

        data = data or {}
        stop = self.env["prema.dispatch.stop"].browse(int(stop_id))
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        if stop.status in ("completed", "cancelled", "skipped"):
            return {"success": False, "error": "Closed stops cannot be changed by the driver"}

        if action == "defer":
            return self._driver_defer_stop(stop, data)
        if action == "resume_deferred":
            return self._driver_resume_deferred_stop(stop, data)
        if action == "report_problem":
            return self._driver_report_problem(stop, data)
        if action == "resume_exception":
            return self._driver_resume_exception(stop, data)
        return self._driver_make_stop_next(stop, data)

    def _guided_stop_result(self, stop, message=None):
        return {
            "success": True,
            "stop_id": stop.id,
            "status": stop.status,
            "sequence": stop.sequence,
            "deferred_reason": stop.driver_deferred_reason or "",
            "deferred_reason_other": stop.driver_deferred_reason_other or "",
            "deferred_until": fields.Datetime.to_string(stop.driver_deferred_until) if stop.driver_deferred_until else "",
            "exception_reason": stop.driver_exception_reason or "",
            "exception_notes": stop.driver_exception_notes or "",
            "message": message or "",
        }

    def _post_driver_audit(self, job, body):
        """Write operational audit notes without emailing job followers."""
        job.message_post(body=body, subtype_xmlid="mail.mt_note")

    def _driver_defer_stop(self, stop, data):
        reason = data.get("reason") or "other"
        allowed = dict(_DEFER_REASONS)
        if reason not in allowed:
            reason = "other"
        reason_other = (data.get("reason_other") or "").strip()[:240]
        until = False
        if data.get("return_at"):
            try:
                until = fields.Datetime.to_datetime(data["return_at"])
            except (TypeError, ValueError):
                until = False

        siblings = stop.job_id.stop_ids.filtered(lambda s: s.id != stop.id and s.status != "cancelled")
        max_sequence = max(siblings.mapped("sequence") or [stop.sequence or 10])
        original_sequence = stop.driver_deferred_original_sequence or stop.sequence or 10
        stop.write({
            "status": "deferred",
            "driver_deferred_reason": reason,
            "driver_deferred_reason_other": reason_other if reason == "other" else False,
            "driver_deferred_until": until,
            "driver_deferred_at": fields.Datetime.now(),
            "driver_deferred_by": self.env.user.id,
            "driver_deferred_original_sequence": original_sequence,
            # Deferred stays visible/open but moves behind serviceable stops.
            "sequence": max_sequence + 10,
        })
        label = reason_other or allowed.get(reason) or "Come Back Later"
        when = f" · return after {fields.Datetime.to_string(until)}" if until else ""
        self._post_driver_audit(
            stop.job_id,
            Markup("Driver deferred stop <b>%s</b>: %s%s") % (
                escape(stop.name or stop.address or str(stop.id)), escape(label), escape(when)
            ),
        )
        return self._guided_stop_result(stop, "Stop saved for later. Continue to the next eligible stop.")

    def _driver_resume_deferred_stop(self, stop, data):
        if stop.status != "deferred":
            return self._guided_stop_result(stop, "Stop is already active")
        siblings = stop.job_id.stop_ids.filtered(
            lambda s: s.id != stop.id and s.status not in ("completed", "cancelled", "skipped", "deferred", "exception")
        )
        if data.get("make_current", True) and siblings:
            new_sequence = max(0, min(siblings.mapped("sequence")) - 1)
        else:
            new_sequence = stop.driver_deferred_original_sequence or stop.sequence
        stop.write({
            "status": "pending",
            "sequence": new_sequence,
            "driver_deferred_reason": False,
            "driver_deferred_reason_other": False,
            "driver_deferred_until": False,
            "driver_deferred_at": False,
            "driver_deferred_by": False,
            "driver_deferred_original_sequence": 0,
        })
        self._post_driver_audit(
            stop.job_id,
            Markup("Driver returned stop <b>%s</b> to the active route") % escape(stop.name or stop.address or str(stop.id)),
        )
        return self._guided_stop_result(stop, "Stop returned to the active route")

    def _driver_report_problem(self, stop, data):
        reason = data.get("reason") or "other"
        allowed = dict(_EXCEPTION_REASONS)
        if reason not in allowed:
            reason = "other"
        notes = (data.get("notes") or "").strip()[:2000]
        previous = stop.driver_exception_previous_status if stop.status == "exception" else stop.status
        stop.write({
            "status": "exception",
            "driver_exception_reason": reason,
            "driver_exception_notes": notes,
            "driver_exception_opened_at": fields.Datetime.now(),
            "driver_exception_opened_by": self.env.user.id,
            "driver_exception_previous_status": previous or "pending",
        })
        note_html = Markup("<br/>%s") % escape(notes) if notes else Markup("")
        self._post_driver_audit(
            stop.job_id,
            Markup("Driver reported a stop exception at <b>%s</b>: %s%s") % (
                escape(stop.name or stop.address or str(stop.id)),
                escape(allowed.get(reason) or "Other"),
                note_html,
            ),
        )
        return self._guided_stop_result(stop, "Problem reported to Dispatch. The stop remains open.")

    def _driver_resume_exception(self, stop, data):
        if stop.status != "exception":
            return self._guided_stop_result(stop, "No open exception on this stop")
        previous = stop.driver_exception_previous_status or "pending"
        if previous not in ("pending", "en_route", "arrived", "deferred"):
            previous = "arrived" if stop.actual_arrival_time else "pending"
        stop.write({
            "status": previous,
            "driver_exception_reason": False,
            "driver_exception_notes": False,
            "driver_exception_opened_at": False,
            "driver_exception_opened_by": False,
            "driver_exception_previous_status": False,
        })
        self._post_driver_audit(
            stop.job_id,
            Markup("Driver resumed stop <b>%s</b> after resolving its exception") % escape(stop.name or stop.address or str(stop.id)),
        )
        return self._guided_stop_result(stop, "Exception cleared; stop resumed")

    def _driver_make_stop_next(self, stop, data):
        reason = (data.get("reason") or "Driver selected an out-of-sequence stop").strip()[:500]
        siblings = stop.job_id.stop_ids.filtered(
            lambda s: s.id != stop.id and s.status not in ("completed", "cancelled", "skipped", "deferred", "exception")
        )
        new_sequence = max(0, min(siblings.mapped("sequence")) - 1) if siblings else stop.sequence
        old_sequence = stop.sequence
        stop.write({
            "sequence": new_sequence,
            "driver_sequence_override_reason": reason,
            "driver_sequence_override_at": fields.Datetime.now(),
            "driver_sequence_override_by": self.env.user.id,
        })
        self._post_driver_audit(
            stop.job_id,
            Markup("Driver changed stop sequence for <b>%s</b> (%s → %s): %s") % (
                escape(stop.name or stop.address or str(stop.id)), old_sequence, new_sequence, escape(reason)
            ),
        )
        return self._guided_stop_result(stop, "Stop moved to the front of this route")

import logging

from markupsafe import Markup, escape

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


_UPDATE_TYPES = [
    ("pickup_variance", "Pickup Pallet Variance"),
    ("stop_exception", "Stop Problem / Exception"),
    ("stop_deferred", "Come Back Later / Delay"),
]

_SEVERITIES = [
    ("info", "Info"),
    ("warning", "Warning"),
    ("urgent", "Urgent"),
]

_STATUSES = [
    ("open", "Open"),
    ("acknowledged", "Acknowledged"),
    ("resolved", "Resolved"),
    ("dismissed", "Dismissed"),
]

_EXCEPTION_TITLES = {
    "customer_closed": "Customer Closed",
    "refused_freight": "Freight Refused",
    "damaged_freight": "Damaged Freight",
    "short_shipment": "Short Shipment",
    "extra_freight": "Extra Freight",
    "wrong_freight": "Wrong Freight",
    "dock_inaccessible": "Dock Inaccessible",
    "long_wait": "Long Wait",
    "appointment_issue": "Appointment Issue",
    "address_issue": "Address Issue",
    "temperature_issue": "Temperature Issue",
    "other": "Driver Reported Problem",
}

_DEFER_TITLES = {
    "customer_closed": "Customer Not Open Yet",
    "appointment_later": "Appointment Later",
    "dock_unavailable": "Dock Unavailable",
    "long_wait": "Long Wait",
    "dispatcher_instructed": "Dispatcher Instructed",
    "other": "Come Back Later",
}

_URGENT_REASONS = {
    "temperature_issue",
    "damaged_freight",
    "wrong_freight",
    "refused_freight",
}


class PremaDispatchDriverUpdate(models.Model):
    """Structured operational messages from Driver App to Dispatch.

    This is deliberately NOT an approval queue. Driver work continues under
    the existing stop/pickup/capacity rules; these rows only make operational
    notes visible and auditable for Dispatch. Driver-facing visibility reuses
    the already-working per-driver Discuss channel instead of creating a
    second messaging system.
    """

    _name = "prema.dispatch.driver.update"
    _description = "Driver Operational Update"
    _rec_name = "title"
    _order = "reported_at desc, id desc"

    event_key = fields.Char(required=True, copy=False, index=True)
    job_id = fields.Many2one(
        "prema.dispatch.job", required=True, ondelete="cascade", index=True
    )
    stop_id = fields.Many2one(
        "prema.dispatch.stop", ondelete="set null", index=True
    )
    item_id = fields.Many2one(
        "prema.dispatch.item", string="Pallet / Item", ondelete="set null", index=True
    )
    driver_id = fields.Many2one(
        "res.partner", string="Driver", ondelete="set null", index=True
    )
    update_type = fields.Selection(_UPDATE_TYPES, required=True, index=True)
    reason_code = fields.Char(index=True)
    title = fields.Char(required=True)
    severity = fields.Selection(_SEVERITIES, default="info", required=True, index=True)
    message = fields.Text(required=True)
    expected_pallets = fields.Integer()
    actual_pallets = fields.Integer()
    reported_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    status = fields.Selection(_STATUSES, default="open", required=True, index=True)

    acknowledged_by = fields.Many2one("res.users", readonly=True, copy=False)
    acknowledged_at = fields.Datetime(readonly=True, copy=False)
    dismissed_by = fields.Many2one("res.users", readonly=True, copy=False)
    dismissed_at = fields.Datetime(readonly=True, copy=False)
    last_reply = fields.Text(readonly=True, copy=False)
    replied_by = fields.Many2one("res.users", readonly=True, copy=False)
    replied_at = fields.Datetime(readonly=True, copy=False)
    invoice_logged = fields.Boolean(default=False, copy=False)

    _sql_constraints = [
        (
            "driver_update_event_key_unique",
            "unique(event_key)",
            "This driver operational update was already recorded.",
        ),
    ]

    # ------------------------------------------------------------------
    # Security / formatting helpers
    # ------------------------------------------------------------------

    @api.model
    def _is_dispatch_staff(self):
        return any(
            self.env.user.has_group(group)
            for group in (
                "prema_dispatch.group_dispatcher",
                "prema_dispatch.group_dispatch_manager",
                "base.group_system",
            )
        )

    @api.model
    def _require_dispatch_staff(self):
        if not self._is_dispatch_staff():
            raise exceptions.AccessError("Only dispatch staff can manage Driver Updates.")

    def _stop_label(self):
        self.ensure_one()
        stop = self.stop_id
        if not stop:
            return ""
        loc = stop.saved_location_id
        company = (
            (loc.business_name or loc.name) if loc else ""
        ) or (stop.partner_id.name if stop.partner_id else "") or stop.contact_name or ""
        if company and stop.address:
            return f"{company} — {stop.address}"
        return company or stop.address or stop.name or "Stop"

    def _summary_html(self, heading=None):
        self.ensure_one()
        heading = heading or f"DRIVER UPDATE — {self.title}"
        lines = [Markup("<b>%s</b>") % escape(heading)]
        lines.append(Markup("Job: %s") % escape(self.job_id.name or ""))
        if self.driver_id:
            lines.append(Markup("Driver: %s") % escape(self.driver_id.name or ""))
        stop_label = self._stop_label()
        if stop_label:
            lines.append(Markup("Stop: %s") % escape(stop_label))
        if self.update_type == "pickup_variance":
            lines.append(
                Markup("Expected pallets: %s | Actual pallets: %s")
                % (self.expected_pallets, self.actual_pallets)
            )
        lines.append(Markup("Driver note: %s") % escape(self.message or ""))
        lines.append(Markup("Status: %s") % escape(dict(_STATUSES).get(self.status, self.status or "")))
        if self.last_reply:
            lines.append(Markup("Latest dispatcher reply: %s") % escape(self.last_reply))
        if self.reported_at:
            local = fields.Datetime.context_timestamp(self, self.reported_at)
            lines.append(Markup("Reported: %s") % escape(local.strftime("%b %d, %Y %I:%M %p")))
        return Markup("<br/>").join(lines)

    def _safe_internal_note(self, record, body, author_partner=None):
        """Internal-only chatter; never let messaging break driver work."""
        if not record:
            return False
        try:
            kwargs = {
                "body": body,
                "message_type": "comment",
                "subtype_xmlid": "mail.mt_note",
            }
            if author_partner:
                kwargs["author_id"] = author_partner.id
            record.sudo().message_post(**kwargs)
            return True
        except Exception:
            _logger.warning(
                "Could not post internal Driver Update note on %s,%s",
                record._name,
                record.id,
                exc_info=True,
            )
            return False

    @api.model
    def _driver_channel_for_job(self, job):
        """Reuse the existing `Driver: <name>` Discuss channel convention."""
        driver = job.driver_id
        if not driver:
            return False
        Channel = self.env["discuss.channel"].sudo()
        name = f"Driver: {driver.name}"
        channel = Channel.search([("name", "=", name)], limit=1)
        if not channel:
            manager_group = self.env.ref(
                "prema_dispatch.group_dispatch_manager", raise_if_not_found=False
            )
            dispatcher_group = self.env.ref(
                "prema_dispatch.group_dispatcher", raise_if_not_found=False
            )
            users = (
                (manager_group.users if manager_group else self.env["res.users"])
                | (dispatcher_group.users if dispatcher_group else self.env["res.users"])
            )
            dispatch_partners = users.mapped("partner_id").filtered(
                lambda partner: partner.id != driver.id
            )
            channel = Channel.create({
                "name": name,
                "channel_type": "channel",
                "description": f"Direct communication with driver {driver.name}",
            })
            channel.add_members((dispatch_partners | driver).ids)
        elif driver.id not in channel.channel_partner_ids.ids:
            channel.add_members([driver.id])
        return channel

    def _safe_driver_chat(self, body, author_partner=None):
        self.ensure_one()
        try:
            channel = self._driver_channel_for_job(self.job_id)
            if not channel:
                return False
            kwargs = {
                "body": body,
                "message_type": "comment",
                "subtype_xmlid": "mail.mt_comment",
            }
            if author_partner:
                kwargs["author_id"] = author_partner.id
            channel.sudo().message_post(**kwargs)
            return True
        except Exception:
            _logger.warning(
                "Could not echo Driver Update %s to driver chat", self.id, exc_info=True
            )
            return False

    def _mirror_to_invoice(self):
        """Mirror each operational update into invoice INTERNAL notes only."""
        for update in self:
            if update.invoice_logged:
                continue
            invoice = update.job_id.invoice_id
            if not invoice:
                continue
            if update._safe_internal_note(invoice, update._summary_html()):
                update.sudo().write({"invoice_logged": True})
        return True

    def _audit_action(self, label, detail="", echo_driver=False):
        self.ensure_one()
        actor = self.env.user.partner_id
        body = Markup("<b>DRIVER UPDATE — %s</b><br/>Job: %s<br/>Update: %s") % (
            escape(label),
            escape(self.job_id.name or ""),
            escape(self.title or ""),
        )
        if detail:
            body += Markup("<br/>%s") % escape(detail)
        self._safe_internal_note(self.job_id, body, actor)
        if self.job_id.invoice_id:
            self._safe_internal_note(self.job_id.invoice_id, body, actor)
        if echo_driver:
            self._safe_driver_chat(body, actor)

    # ------------------------------------------------------------------
    # Creation / resolution bridges called by existing driver flows
    # ------------------------------------------------------------------

    @api.model
    def _upsert_operational_update(self, event_key, vals, author_partner_id=False):
        Update = self.sudo()
        existing = Update.search([("event_key", "=", event_key)], limit=1)
        author = self.env["res.partner"].browse(author_partner_id).exists() if author_partner_id else False
        vals = dict(vals)
        vals.setdefault("reported_at", fields.Datetime.now())
        vals.setdefault("status", "open")

        if existing:
            material_fields = (
                "job_id", "stop_id", "item_id", "driver_id", "update_type",
                "reason_code", "title", "severity", "message",
                "expected_pallets", "actual_pallets",
            )
            changed = any(
                (getattr(existing, field).id if getattr(existing, field, False) and field.endswith("_id") else getattr(existing, field, False))
                != vals.get(field, False)
                for field in material_fields
                if field in vals
            )
            # A dismissed alert stays dismissed on an identical network retry.
            # A resolved pickup variance may legitimately recur later in the
            # same pickup, so resolved rows are allowed to reopen.
            if not changed and existing.status in ("open", "acknowledged", "dismissed"):
                return existing
            vals.update({
                "status": "open",
                "reported_at": fields.Datetime.now(),
                "acknowledged_by": False,
                "acknowledged_at": False,
                "dismissed_by": False,
                "dismissed_at": False,
                "invoice_logged": False,
            })
            existing.write(vals)
            update = existing
        else:
            vals["event_key"] = event_key
            update = Update.create(vals)

        # Every operational update is visible in three places without ever
        # gating the driver: Job internal audit, existing Driver Chat, and —
        # when one exists — the invoice's internal chatter.
        update._safe_internal_note(update.job_id, update._summary_html(), author)
        update._safe_driver_chat(
            update._summary_html(
                f"Driver Update sent to Dispatch — {update.title}"
            ),
            author,
        )
        update._mirror_to_invoice()
        return update

    @api.model
    def record_pickup_variance(self, job, stop, expected, actual, notes, author_partner_id=False):
        event_key = f"pickup_variance:{job.id}:{stop.id}"
        if int(actual or 0) == int(expected or 0):
            return self.resolve_event(event_key, "Pallet count returned to the expected quantity.")
        delta = int(actual or 0) - int(expected or 0)
        title = "Extra Pallet" if delta == 1 else "Extra Pallets" if delta > 1 else "Short Shipment"
        message = (notes or "").strip() or (
            f"Pallet count changed from {int(expected or 0)} to {int(actual or 0)}."
        )
        return self._upsert_operational_update(
            event_key,
            {
                "job_id": job.id,
                "stop_id": stop.id,
                "driver_id": (job.driver_id.id or author_partner_id or False),
                "update_type": "pickup_variance",
                "reason_code": "extra_freight" if delta > 0 else "short_shipment",
                "title": title,
                "severity": "warning",
                "message": message,
                "expected_pallets": int(expected or 0),
                "actual_pallets": int(actual or 0),
            },
            author_partner_id=author_partner_id,
        )

    @api.model
    def record_stop_exception(self, stop, author_partner_id=False):
        active = self.sudo().search([
            ("job_id", "=", stop.job_id.id),
            ("stop_id", "=", stop.id),
            ("update_type", "=", "stop_exception"),
            ("status", "in", ("open", "acknowledged")),
        ], order="reported_at desc, id desc", limit=1)
        reason = stop.driver_exception_reason or "other"
        title = _EXCEPTION_TITLES.get(reason, "Driver Reported Problem")
        notes = (stop.driver_exception_notes or "").strip()
        message = notes or title
        event_key = active.event_key if active else (
            f"stop_exception:{stop.job_id.id}:{stop.id}:"
            f"{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        severity = "urgent" if reason in _URGENT_REASONS else "warning"
        return self._upsert_operational_update(
            event_key,
            {
                "job_id": stop.job_id.id,
                "stop_id": stop.id,
                "driver_id": (stop.job_id.driver_id.id or author_partner_id or False),
                "update_type": "stop_exception",
                "reason_code": reason,
                "title": title,
                "severity": severity,
                "message": message,
            },
            author_partner_id=author_partner_id,
        )

    @api.model
    def record_stop_deferred(self, stop, author_partner_id=False):
        active = self.sudo().search([
            ("job_id", "=", stop.job_id.id),
            ("stop_id", "=", stop.id),
            ("update_type", "=", "stop_deferred"),
            ("status", "in", ("open", "acknowledged")),
        ], order="reported_at desc, id desc", limit=1)
        reason = stop.driver_deferred_reason or "other"
        title = _DEFER_TITLES.get(reason, "Come Back Later")
        other = (stop.driver_deferred_reason_other or "").strip()
        message = other or title
        event_key = active.event_key if active else (
            f"stop_deferred:{stop.job_id.id}:{stop.id}:"
            f"{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        return self._upsert_operational_update(
            event_key,
            {
                "job_id": stop.job_id.id,
                "stop_id": stop.id,
                "driver_id": (stop.job_id.driver_id.id or author_partner_id or False),
                "update_type": "stop_deferred",
                "reason_code": reason,
                "title": title,
                "severity": "warning" if reason != "dispatcher_instructed" else "info",
                "message": message,
            },
            author_partner_id=author_partner_id,
        )

    @api.model
    def resolve_event(self, event_key, detail="Resolved by driver workflow."):
        update = self.sudo().search([
            ("event_key", "=", event_key),
            ("status", "in", ("open", "acknowledged")),
        ], limit=1)
        if not update:
            return False
        update.write({"status": "resolved"})
        update._audit_action("Resolved", detail)
        return update

    @api.model
    def resolve_stop_updates(self, stop, update_type, detail):
        updates = self.sudo().search([
            ("job_id", "=", stop.job_id.id),
            ("stop_id", "=", stop.id),
            ("update_type", "=", update_type),
            ("status", "in", ("open", "acknowledged")),
        ])
        for update in updates:
            update.write({"status": "resolved"})
            update._audit_action("Resolved", detail)
        return True

    # ------------------------------------------------------------------
    # Dispatcher Live Map API
    # ------------------------------------------------------------------

    def _live_payload(self):
        self.ensure_one()
        reported = ""
        if self.reported_at:
            reported = fields.Datetime.context_timestamp(self, self.reported_at).strftime(
                "%b %d, %I:%M %p"
            )
        return {
            "id": self.id,
            "status": self.status,
            "severity": self.severity,
            "update_type": self.update_type,
            "title": self.title or "Driver Update",
            "job_id": self.job_id.id,
            "job_name": self.job_id.name or "",
            "driver_name": self.driver_id.name or "",
            "stop_id": self.stop_id.id if self.stop_id else False,
            "stop_name": self._stop_label(),
            "message": self.message or "",
            "expected_pallets": self.expected_pallets,
            "actual_pallets": self.actual_pallets,
            "reported_at": reported,
            "acknowledged_by": self.acknowledged_by.name or "",
            "last_reply": self.last_reply or "",
        }

    @api.model
    def get_live_updates(self):
        self._require_dispatch_staff()
        Update = self.sudo()
        updates = Update.search([
            ("status", "in", ("open", "acknowledged")),
        ], order="reported_at desc, id desc", limit=100)
        return {
            "updates": [update._live_payload() for update in updates],
            "open_count": len(updates.filtered(lambda update: update.status == "open")),
        }

    @api.model
    def acknowledge_update(self, update_id):
        self._require_dispatch_staff()
        update = self.sudo().browse(int(update_id)).exists()
        if not update:
            return {"success": False, "error": "Driver Update not found."}
        if update.status in ("resolved", "dismissed"):
            return {"success": True, "status": update.status}
        if update.status == "open":
            update.write({
                "status": "acknowledged",
                "acknowledged_by": self.env.user.id,
                "acknowledged_at": fields.Datetime.now(),
            })
            update._audit_action(
                "Acknowledged",
                f"Read by {self.env.user.name}.",
                echo_driver=True,
            )
        return {"success": True, "status": update.status}

    @api.model
    def dismiss_update(self, update_id):
        self._require_dispatch_staff()
        update = self.sudo().browse(int(update_id)).exists()
        if not update:
            return {"success": False, "error": "Driver Update not found."}
        if update.status == "dismissed":
            return {"success": True, "status": "dismissed"}
        if update.status == "resolved":
            return {"success": True, "status": "resolved"}
        update.write({
            "status": "dismissed",
            "dismissed_by": self.env.user.id,
            "dismissed_at": fields.Datetime.now(),
        })
        update._audit_action("Dismissed", f"Dismissed from the active alert list by {self.env.user.name}.")
        return {"success": True, "status": "dismissed"}

    @api.model
    def reply_update(self, update_id, body):
        self._require_dispatch_staff()
        update = self.sudo().browse(int(update_id)).exists()
        if not update:
            return {"success": False, "error": "Driver Update not found."}
        text = (body or "").strip()[:2000]
        if not text:
            return {"success": False, "error": "Write a reply first."}
        now = fields.Datetime.now()
        vals = {
            "last_reply": text,
            "replied_by": self.env.user.id,
            "replied_at": now,
        }
        if update.status == "open":
            vals.update({
                "status": "acknowledged",
                "acknowledged_by": self.env.user.id,
                "acknowledged_at": now,
            })
        update.write(vals)
        update._audit_action(
            "Dispatcher Reply",
            f"{self.env.user.name}: {text}",
            echo_driver=True,
        )
        return {"success": True, "status": update.status}


class PremaDispatchJobDriverUpdateBridge(models.Model):
    _inherit = "prema.dispatch.job"

    driver_update_ids = fields.One2many(
        "prema.dispatch.driver.update", "job_id", string="Driver Updates", copy=False
    )

    @api.model
    def driver_confirm_pickup_actuals(self, stop_id, values=None):
        """Observe the existing pickup-confirm path; never change its rules."""
        stop = self.env["prema.dispatch.stop"].browse(int(stop_id)).exists()
        job = stop.job_id if stop else self.env["prema.dispatch.job"]
        # The driver-facing "Expected" count comes from job.expected_pallet_count
        # (see driver_app pickupSummary), so the update must compare against it.
        # stop.pallets_in is NOT stable: the variance's downstream pallets_out
        # write re-triggers _estimate_pickup_pallets and drifts it 1 -> 2, which
        # would make a repeated identical confirmation resolve (not record) the
        # variance. Fall back to it, then to the physical item count, only for
        # jobs without a planned expectation.
        expected = 0
        if job and job.expected_pallet_count:
            expected = int(job.expected_pallet_count)
        if not expected and stop:
            expected = int(stop.pallets_in or 0)
            if not expected:
                expected = len(stop._items_picked_here())
        author_partner_id = self.env.user.partner_id.id

        result = super().driver_confirm_pickup_actuals(stop_id, values=values)

        try:
            if stop and job:
                actual = int((values or {}).get("actual_received_pallet_count") or 0)
                # Only announce a count the canonical method actually saved.
                # pickup_gate_blocked is expected during early guided steps: the
                # count IS saved even though photos/position/proof come later.
                accepted_save = bool(result and (
                    result.get("success") or result.get("code") == "pickup_gate_blocked"
                ))
                saved = accepted_save and int(job.actual_received_pallet_count or 0) == actual
                Update = self.env["prema.dispatch.driver.update"].sudo()
                if saved and actual != expected:
                    Update.record_pickup_variance(
                        job, stop, expected, actual,
                        (values or {}).get("variance_notes") or "",
                        author_partner_id=author_partner_id,
                    )
                elif saved and actual == expected:
                    Update.resolve_event(
                        f"pickup_variance:{job.id}:{stop.id}",
                        "Driver confirmed the expected pallet quantity.",
                    )
        except Exception:
            # Driver Updates are observability only: they must NEVER roll back
            # the already-working pallet/capacity/pickup transaction.
            _logger.warning(
                "Could not record pickup Driver Update for stop %s", stop_id, exc_info=True
            )
        return result

    def _completion_invoice(self):
        inv = super()._completion_invoice()
        try:
            self.sudo().mapped("driver_update_ids")._mirror_to_invoice()
        except Exception:
            _logger.warning(
                "Could not mirror Driver Updates to completion invoice for jobs %s",
                self.ids,
                exc_info=True,
            )
        return inv


class PremaDispatchJobGuidedUpdateBridge(models.Model):
    _inherit = "prema.dispatch.job"

    def _driver_report_problem(self, stop, data):
        result = super()._driver_report_problem(stop, data)
        if result and result.get("success"):
            try:
                self.env["prema.dispatch.driver.update"].sudo().record_stop_exception(
                    stop,
                    author_partner_id=self.env.user.partner_id.id,
                )
            except Exception:
                _logger.warning(
                    "Could not record stop-exception Driver Update for stop %s",
                    stop.id,
                    exc_info=True,
                )
        return result

    def _driver_resume_exception(self, stop, data):
        result = super()._driver_resume_exception(stop, data)
        if result and result.get("success"):
            try:
                self.env["prema.dispatch.driver.update"].sudo().resolve_stop_updates(
                    stop,
                    "stop_exception",
                    "Driver resumed the stop after the problem was resolved.",
                )
            except Exception:
                _logger.warning(
                    "Could not resolve stop-exception Driver Update for stop %s",
                    stop.id,
                    exc_info=True,
                )
        return result

    def _driver_defer_stop(self, stop, data):
        result = super()._driver_defer_stop(stop, data)
        if result and result.get("success"):
            try:
                self.env["prema.dispatch.driver.update"].sudo().record_stop_deferred(
                    stop,
                    author_partner_id=self.env.user.partner_id.id,
                )
            except Exception:
                _logger.warning(
                    "Could not record deferred-stop Driver Update for stop %s",
                    stop.id,
                    exc_info=True,
                )
        return result

    def _driver_resume_deferred_stop(self, stop, data):
        result = super()._driver_resume_deferred_stop(stop, data)
        if result and result.get("success"):
            try:
                self.env["prema.dispatch.driver.update"].sudo().resolve_stop_updates(
                    stop,
                    "stop_deferred",
                    "Driver returned the deferred stop to the active route.",
                )
            except Exception:
                _logger.warning(
                    "Could not resolve deferred-stop Driver Update for stop %s",
                    stop.id,
                    exc_info=True,
                )
        return result

import logging
import pytz

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


def _local_8am_utc(env, date_obj):
    """Return 8:00 AM on date_obj in the user's timezone, stored as UTC (naive) for Odoo."""
    from datetime import datetime
    tz_name = env.context.get("tz") or env.user.tz or "UTC"
    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.utc
    local_dt = user_tz.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 8, 0))
    return local_dt.astimezone(pytz.utc).replace(tzinfo=None)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    dispatch_job_ids = fields.One2many(
        "prema.dispatch.job", "sale_order_id",
        string="Dispatch Bookings", copy=False,
    )
    dispatch_job_count = fields.Integer(
        compute="_compute_dispatch_job_count", store=True
    )
    x_so_text_input = fields.Text(
        string="Customer Text / WhatsApp",
        help="Paste a WhatsApp message, SMS, or email from the customer. "
             "AI will extract pickup/delivery stops, pallet counts per stop, route, date, "
             "reefer/liftgate requirements and create a dispatch booking.",
    )

    @api.depends("dispatch_job_ids")
    def _compute_dispatch_job_count(self):
        for order in self:
            order.dispatch_job_count = len(order.dispatch_job_ids)

    def action_open_dispatch_jobs_prema(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Bookings",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("sale_order_id", "=", self.id)],
            "context": {"default_sale_order_id": self.id},
        }

    @staticmethod
    def _so_job_advisory_lock(env, so_id):
        """Serialize concurrent 'create a dispatch job for this SO' clicks.

        The dispatch_job_ids check below is not atomic by itself: two
        simultaneous clicks (double-click, two tabs) can both pass it and
        create two jobs. The advisory transaction lock forces the second
        request to wait until the first commits, after which its re-check
        finds the job and reuses it instead of duplicating it.
        """
        env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("dispatch_so_job_%d" % so_id,),
        )

    def _open_existing_job_action(self):
        """Anti-duplication: open the existing booking(s) instead of creating."""
        if len(self.dispatch_job_ids) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": "Dispatch Booking",
                "res_model": "prema.dispatch.job",
                "res_id": self.dispatch_job_ids.id,
                "view_mode": "form",
            }
        return self.action_open_dispatch_jobs_prema()

    def action_book_load(self):
        """Book Load button — create a Prema Dispatch booking from this Sales Order."""
        self.ensure_one()

        self._so_job_advisory_lock(self.env, self.id)
        # Anti-duplication: open existing booking if one exists
        if self.dispatch_job_ids:
            return self._open_existing_job_action()

        draft_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )

        # Pull partner — prefer invoice address if set
        partner = self.partner_invoice_id or self.partner_id

        job = self.env["prema.dispatch.job"].create({
            "sale_order_id": self.id,
            "partner_id": partner.id,
            "ref": self.client_order_ref or self.name,
            "stage_id": draft_stage.id if draft_stage else False,
            "company_id": self.company_id.id,
            "dispatcher_id": self.env.uid,
            "source_model": "sale.order",
            "source_res_id": self.id,
        })

        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Booking",
            "res_model": "prema.dispatch.job",
            "res_id": job.id,
            "view_mode": "form",
        }

    @staticmethod
    def _so_text_fingerprint(text):
        """Stable dedupe key for 'generate from text': same pasted text on
        the same SO is the same shipment, never a second booking."""
        import hashlib
        return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:12]

    def action_generate_dispatch_from_text(self):
        """AI: Parse pasted customer text and create a dispatch booking with stops."""
        self.ensure_one()
        if not self.x_so_text_input or not self.x_so_text_input.strip():
            raise exceptions.UserError(
                "Paste a customer message in the 'Generate from Text' tab first."
            )
        fingerprint = self._so_text_fingerprint(self.x_so_text_input)

        from odoo.addons.premafirm_ai_engine.services.invoice_ai_service import InvoiceAIService

        try:
            service = InvoiceAIService(self.env)
            result = service.analyze_from_text(self, self.x_so_text_input, "")
        except ValueError as exc:
            raise exceptions.UserError(str(exc))
        except Exception as exc:
            _logger.exception("AI text parsing failed for SO %s", self.name)
            raise exceptions.UserError(
                f"AI parsing failed: {type(exc).__name__}: {exc}"
            )

        if not result:
            raise exceptions.UserError(
                "AI returned no usable result. Please check the text and try again."
            )

        stops_data = result.get("stops") or []
        if len(stops_data) < 2:
            raise exceptions.UserError(
                "AI could not find enough stop information. "
                "Include at least a pickup address and a delivery address in the text."
            )

        # Determine service date
        from datetime import date as _date, datetime as _dt

        sched_date = None
        sdate_raw = result.get("scheduled_date")
        if sdate_raw and sdate_raw not in ("null", "", None):
            try:
                sched_date = _date.fromisoformat(str(sdate_raw))
            except Exception:
                pass
        if not sched_date:
            sched_date = self.env["prema.dispatch.job"]._user_today()

        draft_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        partner = self.partner_invoice_id or self.partner_id

        # Detect reefer/liftgate from AI result and text keywords
        requires_reefer = bool(result.get("requires_reefer"))
        requires_liftgate = bool(result.get("requires_liftgate")) or any(
            s.get("liftgate") for s in stops_data
        )
        text_lower = self.x_so_text_input.lower()
        if any(kw in text_lower for kw in ("reefer", "refrigerat", "frozen", "chilled", "temp control")):
            requires_reefer = True
        if any(kw in text_lower for kw in ("liftgate", "lift gate", "tailgate", "no dock")):
            requires_liftgate = True

        temp_val = result.get("temp_requirement") or ""
        if temp_val and not temp_val.endswith("°C"):
            import re as _re
            if _re.match(r"^-?\d+(\.\d+)?$", temp_val.strip()):
                temp_val = temp_val.strip() + " °C"

        # Idempotency: same SO + same pasted text = the same shipment.
        # Serialize concurrent clicks, then reuse the job created by the
        # first request instead of duplicating it. Different text on the
        # same SO may still create a separate job (legit second load).
        self._so_job_advisory_lock(self.env, self.id)
        existing = self.env["prema.dispatch.job"].search([
            ("sale_order_id", "=", self.id),
            ("source_model", "=", "sale.order"),
            ("source_res_id", "=", self.id),
            ("internal_notes", "=like", "%[fp:%s]%" % fingerprint),
        ], limit=1)
        if existing:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Dispatch Booking Reused",
                    "message": (
                        f"{existing.name} already exists for this customer "
                        "text — opening it instead of creating a duplicate."
                    ),
                    "type": "warning",
                    "sticky": False,
                    "next": {
                        "type": "ir.actions.act_window",
                        "name": "Dispatch Booking",
                        "res_model": "prema.dispatch.job",
                        "res_id": existing.id,
                        "view_mode": "form",
                    },
                },
            }

        job = self.env["prema.dispatch.job"].create({
            "sale_order_id": self.id,
            "partner_id": partner.id,
            "ref": result.get("reference") or self.client_order_ref or self.name,
            "stage_id": draft_stage.id if draft_stage else False,
            "company_id": self.company_id.id,
            "dispatcher_id": self.env.uid,
            "source_model": "sale.order",
            "source_res_id": self.id,
            "scheduled_pickup": _local_8am_utc(self.env, sched_date),
            "requires_reefer": requires_reefer,
            "requires_liftgate": requires_liftgate,
            "commodity": result.get("commodity") or "",
            "temp_requirement": temp_val,
            "approximate_skids": int(result.get("approximate_skids") or 0),
            "internal_notes": (
                f"AI generated from customer text on {self.name}. "
                f"[fp:{fingerprint}]"
            ),
        })

        # Create stops with correct pallets_in / pallets_out
        self.env["prema.dispatch.job"]._create_stops_from_ai_data(job, stops_data, sched_date)

        n_stops = len(stops_data)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Dispatch Booking Created",
                "message": (
                    f"Booking {job.name} created with {n_stops} stops. "
                    "Opening the dispatch form now."
                ),
                "type": "success",
                "sticky": False,
                "next": {
                    "type": "ir.actions.act_window",
                    "name": "Dispatch Booking",
                    "res_model": "prema.dispatch.job",
                    "res_id": job.id,
                    "view_mode": "form",
                },
            },
        }

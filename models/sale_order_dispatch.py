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
    booking_count = fields.Integer(
        compute="_compute_booking_count",
        string="Prema Bookings",
    )
    x_so_text_input = fields.Text(
        string="Customer Text / WhatsApp",
        help="Paste a WhatsApp message, SMS, or email from the customer. "
             "On a quotation, AI Generate fills the quotation from it. Once "
             "the quotation is confirmed, the same text books the load "
             "through the canonical booking engine.",
    )

    # ═════════════════════════════════════════════════════════════════════
    # D-C1 (master §1/§14): Sale Order → canonical booking entry.
    #
    # Both SO entry flows (Book Load button, Generate from Text) now route
    # through BookingOrchestrationService.confirm_from_internal with the
    # "sale_order" source channel — the SAME canonical path as phone,
    # invoice, WhatsApp and custom-quote channels. The logistics booking's
    # dispatch-job bridge (logistics.booking._create_dispatch_job /
    # _create_dispatch_operation) creates the Planner card(s) and back-links
    # job.sale_order_id + job.logistics_booking_id at creation time.
    #
    # No code path in this module may create prema.dispatch.job directly —
    # direct source_model "sale.order" creates are rejected by the orphan
    # guard in prema.dispatch.job.create() (see models/dispatch_job.py).
    # ═════════════════════════════════════════════════════════════════════

    @api.depends("dispatch_job_ids")
    def _compute_dispatch_job_count(self):
        for order in self:
            order.dispatch_job_count = len(order.dispatch_job_ids)

    def _compute_booking_count(self):
        """The same fact as `_linked_booking`, in the form a smart button
        needs. Not stored: the booking is created by another module's flow,
        so a stored value would need that flow to remember to invalidate it."""
        for order in self:
            order.booking_count = 1 if order._linked_booking() else 0

    # ── Small helpers ────────────────────────────────────────────────────

    @staticmethod
    def _logistics_loaded(env):
        """True when prema_logistics_booking is installed (dispatch itself
        never declares the dependency — cross-repo rule)."""
        return "logistics.booking" in env.registry

    def _linked_booking(self):
        """The canonical logistics.booking created for this Sale Order, if
        any (oldest first — the Book Load button is one booking per SO,
        mirroring the pre-canonical one-job-per-SO behavior)."""
        self.ensure_one()
        if not self._logistics_loaded(self.env):
            return self.env["logistics.booking"].sudo()
        return self.env["logistics.booking"].sudo().search(
            [("sale_order_id", "=", self.id)], order="id", limit=1)

    @staticmethod
    def _open_record_action(model, res_id):
        return {
            "type": "ir.actions.act_window",
            "name": "Booking",
            "res_model": model,
            "res_id": res_id,
            "view_mode": "form",
            "target": "current",
        }

    @staticmethod
    def _notify_open(title, message, action, warning=True):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": "warning" if warning else "success",
                "sticky": False,
                "next": action,
            },
        }

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

    def action_open_booking(self):
        """The canonical Prema booking this Sales Order was booked as."""
        self.ensure_one()
        booking = self._linked_booking()
        if not booking:
            return False
        return self._open_record_action("logistics.booking", booking.id)

    def action_open_crm_opportunity(self):
        """The opportunity this quotation was written for."""
        self.ensure_one()
        lead = getattr(self, "opportunity_id", False)
        if not lead:
            return False
        return {
            "type": "ir.actions.act_window",
            "name": "Opportunity",
            "res_model": "crm.lead",
            "res_id": lead.id,
            "view_mode": "form",
            "target": "current",
        }

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

    @staticmethod
    def _so_job_advisory_lock(env, so_id):
        """Serialize concurrent 'book this Sale Order' clicks.

        The linked-booking checks below are not atomic by themselves: two
        simultaneous clicks (double-click, two tabs) can both pass them and
        create two bookings. The advisory transaction lock forces the second
        request to wait until the first commits, after which its re-check
        finds the booking and reuses it instead of duplicating it.
        """
        env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("dispatch_so_job_%d" % so_id,),
        )

    # ── Book Load button ─────────────────────────────────────────────────

    def action_book_load(self):
        """Book Load button — canonical entry from this Sales Order.

        Priority order (never duplicates):
          1. An existing canonical logistics.booking for this SO (created by
             any D-C1 entry flow) → open it.
          2. Legacy pre-canonical dispatch jobs (no linked booking) → open
             them; nothing new is created under them.
          3. Otherwise → open the Book Load wizard, which mirrors the
             invoice wizard and confirms ONE logistics.booking through
             BookingOrchestrationService (channel "sale_order",
             idempotency key f"sale.order:{id}:{booking_mode}").

        A Rate Confirmation on this order's opportunity is deliberately NOT
        consulted any more. It used to divert the click into the retired
        Rate Confirmation workflow, which meant the customer's accepted
        Sales quotation could not reach a booking at all while an old
        pre-quotation draft sat on the opportunity — the quotation the
        customer actually agreed to has to be the one that books.
        """
        self.ensure_one()

        self._so_job_advisory_lock(self.env, self.id)

        # 1. Canonical booking already exists (created by this or the
        #    Generate-from-Text flow) — open it, never duplicate.
        booking = self._linked_booking()
        if booking:
            return self._open_record_action("logistics.booking", booking.id)

        # 2. Legacy pre-canonical dispatch jobs (created before D-C1) —
        #    preserved and still openable, but no new jobs are ever created
        #    under them.
        legacy_jobs = self.dispatch_job_ids.filtered(
            lambda j: not j.logistics_booking_id)
        if legacy_jobs:
            return self._notify_open(
                "Legacy Dispatch Booking(s)",
                "This Sales Order predates the canonical booking flow: its "
                "dispatch job(s) have no logistics booking behind them. "
                "Open them below; new loads go through the booking flow.",
                self._open_existing_job_action())

        # 3. New canonical booking via the wizard.
        if not self._logistics_loaded(self.env):
            raise exceptions.UserError(
                "Prema Logistics Booking must be installed before a Sales "
                "Order can be booked."
            )
        return {
            "type": "ir.actions.act_window",
            "name": "Book Load from Sales Order",
            "res_model": "prema.dispatch.so.book.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id},
        }

    # ── Generate from Text (AI) ──────────────────────────────────────────

    @staticmethod
    def _so_text_fingerprint(text):
        """Stable dedupe key for 'generate from text': same pasted text on
        the same SO is the same shipment, never a second booking.
        Whitespace runs are collapsed so paste layout differences (extra
        spaces, line breaks) do not defeat the dedupe."""
        import hashlib
        import re as _re
        normalized = _re.sub(r"\s+", " ", (text or "").strip())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _so_text_idempotency_key(so_id, fingerprint):
        """Canonical idempotency key for an AI-text booking. Distinct from
        the wizard key so a legitimately different second shipment on the
        same SO may still create its own booking (pre-canonical behavior)."""
        return f"sale.order:{so_id}:text:{fingerprint}"

    @staticmethod
    def _postal_from_address(address):
        """First Canadian postal code found in an address string ("" if
        none) — cheap enrichment so FSA/corridor tooling can pick the load
        up later; manual pricing never depends on it."""
        import re as _re
        match = _re.search(
            r"\b([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)\b", address or "")
        return (match.group(1) + " " + match.group(2)).upper() if match else ""

    def action_ai_generate_quotation(self):
        """AI Generate — the pasted customer text fills whatever this IS.

        This single box has to serve two different jobs, and the difference
        is entirely a matter of state. While the record is a QUOTATION it is
        a commercial document: the text describes what we are offering, and
        the click must fill the quotation — load details, freight product,
        rate and its tax treatment, and the AI summary — and create nothing
        operational. Confirming the order is how a human records the
        customer's acceptance; a paste into a rate box must never be able to
        dispatch a truck on its own.

        Once the order IS confirmed the same text means what it always did:
        this is a shipment to book, so the click creates the canonical
        booking through the existing flow, unchanged.

        So the split is not a new permission model — it is the same rule the
        customer sees, enforced where the button is.
        """
        self.ensure_one()
        if self.state not in ("draft", "sent"):
            return self.action_generate_dispatch_from_text()

        if "x_ai_summary_instruction" not in self._fields:
            raise exceptions.UserError(
                "PremaFirm AI Engine must be installed for AI Generate on a "
                "quotation."
            )
        text = (self.x_so_text_input or "").strip()
        attachments = self._get_order_attachments()
        if not text and not attachments:
            raise exceptions.UserError(
                "Paste the customer's shipment details into the text box "
                "above (or attach their rate confirmation) before clicking "
                "AI Generate."
            )
        if text:
            # `x_ai_summary_instruction` is the field the quotation AI flow
            # reads; the paste box is the same text under its dispatch-side
            # name. Copying it across keeps ONE record of what the AI was
            # told — the AI Summary tab shows it back to whoever asks why
            # the quotation says what it says.
            self.x_ai_summary_instruction = text
        return self.action_ai_generate_quote()

    def action_generate_dispatch_from_text(self):
        """AI: parse pasted customer text and create ONE canonical
        logistics.booking with stops (channel "sale_order", per-text
        idempotency key). The booking's dispatch-job bridge creates the
        Planner card. Same text on the same SO always reuses the same
        booking; different text may legitimately create another.

        Reachable only from a CONFIRMED order — the button that calls it
        renders on `state in ('sale',)` alone. A quotation's text box goes
        to `action_ai_generate_quotation` instead, because a document the
        customer has not accepted yet must not be able to book a truck."""
        self.ensure_one()
        if not self.x_so_text_input or not self.x_so_text_input.strip():
            raise exceptions.UserError(
                "Paste a customer message in the 'Generate from Text' tab first."
            )
        if not self._logistics_loaded(self.env):
            raise exceptions.UserError(
                "Prema Logistics Booking must be installed before a Sales "
                "Order can generate a dispatch booking."
            )
        fingerprint = self._so_text_fingerprint(self.x_so_text_input)
        key = self._so_text_idempotency_key(self.id, fingerprint)

        self._so_job_advisory_lock(self.env, self.id)

        # Idempotency: the canonical booking for this exact text already
        # exists (service idempotency key is the backstop for race windows).
        existing = self.env["logistics.booking"].sudo().search([
            ("source_channel", "=", "sale_order"),
            ("idempotency_key", "=", key),
        ], limit=1)
        if existing:
            return self._notify_open(
                "Dispatch Booking Reused",
                f"{existing.booking_number} already exists for this customer "
                "text — opening it instead of creating a duplicate.",
                self._open_record_action("logistics.booking", existing.id))

        # Legacy dedupe: a pre-D-C1 dispatch job may already exist for this
        # exact text (fingerprint was stored in its internal notes). Reuse
        # it — never stack a canonical booking on top of it.
        legacy = self.env["prema.dispatch.job"].search([
            ("sale_order_id", "=", self.id),
            ("source_model", "=", "sale.order"),
            ("source_res_id", "=", self.id),
            ("internal_notes", "=like", "%[fp:" + fingerprint + "]%"),
        ], limit=1)
        if legacy:
            return self._notify_open(
                "Legacy Dispatch Booking Reused",
                f"{legacy.name} already exists for this customer text "
                "(created before the canonical booking flow) — opening it.",
                self._open_record_action("prema.dispatch.job", legacy.id))

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

        from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
            BookingOrchestrationService,
        )

        partner = self.partner_invoice_id or self.partner_id
        if not partner:
            raise exceptions.UserError(
                "This Sales Order has no customer — set one before booking."
            )

        # Price basis: the customer agreed the order amount at sale time —
        # the same basis as the invoice Book Load channel. A zero-priced
        # order can never confirm a manual-priced booking silently.
        agreed_rate = self.amount_untaxed or self.amount_total
        if not agreed_rate:
            raise exceptions.UserError(
                "This Sales Order has no amount — price the order first "
                "(the booking carries the agreed order value as its rate)."
            )

        # Detect reefer/liftgate from AI result and text keywords
        requires_reefer = bool(result.get("requires_reefer"))
        text_lower = self.x_so_text_input.lower()
        if any(kw in text_lower for kw in ("reefer", "refrigerat", "frozen", "chilled", "temp control")):
            requires_reefer = True

        # Canonical per-stop mapping. AI stops are single-pickup /
        # multi-delivery; multi-pickup and return-to-origin rounds belong to
        # the movement-v1 architecture (Internal Booking / Rate Confirmation
        # flow) and are refused here with a clear pointer instead of being
        # silently mangled by the legacy bridge.
        pickup_stops = []
        delivery_stops = []
        for stop_data in stops_data:
            raw_type = (stop_data.get("type") or "dropoff").lower()
            if raw_type in ("delivery", "drop-off", "drop_off", "dropoff"):
                delivery_stops.append(stop_data)
            elif raw_type == "pickup":
                pickup_stops.append(stop_data)
            elif raw_type == "return":
                raise exceptions.UserError(
                    "This customer text describes a return-to-origin / "
                    "multi-round load. Create it through the Internal "
                    "Booking / Rate Confirmation flow instead (movement "
                    "bookings), which models that load type exactly."
                )
        if len(pickup_stops) != 1 or not delivery_stops:
            if len(pickup_stops) > 1:
                raise exceptions.UserError(
                    "This customer text has more than one pickup. Multi-pickup "
                    "loads are booked through the Internal Booking / Rate "
                    "Confirmation flow (movement bookings) — 'Generate from "
                    "Text' covers single-pickup loads."
                )
            raise exceptions.UserError(
                "AI could not separate one pickup and at least one delivery "
                "stop. Include a pickup address and delivery address(es)."
            )

        try:
            total_pallets = int(result.get("approximate_skids") or 0)
        except (TypeError, ValueError):
            total_pallets = 0
        def _as_int(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        declared = [
            _as_int(s.get("pallets_out") or s.get("pallets_in") or s.get("pallets") or 0)
            for s in delivery_stops
        ]
        declared_sum = sum(declared)
        if total_pallets <= 0 and declared_sum > 0:
            # AI gave per-stop pallets but no total — the total is their sum.
            total_pallets = declared_sum
        if total_pallets > 0 and declared_sum == 0:
            # AI gave a total but no per-stop split: distribute evenly so
            # every delivery stop survives the booking bridge (stops with
            # zero pallets and no master facility are not operationalized).
            n = len(delivery_stops)
            per_stop = total_pallets // n
            declared = [per_stop] * n
            declared[0] += total_pallets - per_stop * n

        def _canonical_stop(stop_data, pallets):
            address = stop_data.get("address") or ""
            stop = {
                "company_name": stop_data.get("company_name")
                or stop_data.get("location_name") or "",
                "street": address,
                "formatted_address": address,
                "city": stop_data.get("city") or "",
                "postal_code": stop_data.get("postal_code")
                or self._postal_from_address(address),
                "latitude": stop_data.get("latitude") or 0.0,
                "longitude": stop_data.get("longitude") or 0.0,
                "pallet_count": pallets,
                "weight_lbs": stop_data.get("weight_lbs") or 0.0,
                "liftgate_required": bool(
                    stop_data.get("liftgate")
                    or (stop_data.get("requires_liftgate"))
                    or any(kw in text_lower for kw in ("liftgate", "lift gate", "tailgate", "no dock"))
                ),
                "instructions": stop_data.get("instructions") or "",
                "timing_type": "flexible",
                "reference": stop_data.get("dock_door") or "",
            }
            return stop

        # Origin stop carries the full load; each delivery stop its split.
        pickup_stop = _canonical_stop(pickup_stops[0], total_pallets)
        delivery_maps = [
            _canonical_stop(s, declared[idx])
            for idx, s in enumerate(delivery_stops)
        ]

        # Scheduled date: AI date, else today (dispatcher-local).
        from datetime import date as _date
        sched_date = None
        sdate_raw = result.get("scheduled_date")
        if sdate_raw and sdate_raw not in ("null", "", None):
            try:
                sched_date = _date.fromisoformat(str(sdate_raw))
            except Exception:
                pass
        if not sched_date:
            sched_date = self.env["prema.dispatch.job"]._user_today()

        # Reefer setpoint normalization (same service the phone wizard uses).
        temp_val = result.get("temp_requirement") or ""
        required_temperature_c = None
        if requires_reefer:
            import re as _re
            # Parse "<number> <unit>" out of the AI string ("0 °F", "-18 °C",
            # "frozen at -4 Fahrenheit"); anything unparseable stays unset
            # and the raw string is kept for human review on the booking.
            match = _re.search(
                r"(-?\d+(?:\.\d+)?)\s*(fahrenheit|°?f|°?c)\b",
                str(temp_val), flags=_re.IGNORECASE)
            if match:
                try:
                    from odoo.addons.prema_logistics_booking.services.temperature_service import (
                        parse_temperature)
                    unit = "f" if match.group(2).lower().startswith("f") else "c"
                    required_temperature_c = parse_temperature(
                        float(match.group(1)), unit=unit)
                except Exception:
                    required_temperature_c = None

        service = BookingOrchestrationService(self.env)
        request = service.normalize_request({
            "partner_id": partner.id,
            "source_model": "sale.order",
            "source_res_id": self.id,
            "source_reference": self.name,
            "pickup_stops": [pickup_stop],
            "delivery_stops": delivery_maps,
            "pallets": max(total_pallets, 1),
            "weight_lbs": float(result.get("weight_lbs") or 0.0),
            "load_type": "ltl",
            "equipment_type": "reefer" if requires_reefer else "dry",
            "required_temperature_c": required_temperature_c,
            "submitted_temperature_unit": "c",
            "commodity": result.get("commodity") or "",
            "customer_reference": result.get("reference")
            or self.client_order_ref or self.name,
            "instructions": "",
            "requested_pickup_date": sched_date,
            # Manual pricing: the SO amount is the agreed customer rate —
            # corridor pricing needs verified facility postcodes + an
            # available scheduled departure, which this free-text flow does
            # not re-resolve (Book Load wizard does corridor mode).
            "pricing_method": "manual",
            "agreed_rate": agreed_rate,
            "existing_sale_order_id": self.id,
            "idempotency_key": key,
        }, source_channel="sale_order")
        booking = service.confirm_from_internal(request)

        return self._notify_open(
            "Dispatch Booking Created",
            f"Booking {booking.booking_number} created with "
            f"{len(delivery_maps) + 1} stops from the customer text — "
            "opening it now.",
            self._open_record_action("logistics.booking", booking.id),
            warning=False,
        )

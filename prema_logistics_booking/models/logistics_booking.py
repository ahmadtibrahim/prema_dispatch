import datetime
import logging
import secrets

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.availability_bridge import AvailabilityBridge
from ..services.pricing_service import PricingService

_logger = logging.getLogger(__name__)

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("chilled", "Chilled"), ("frozen", "Frozen")]
BILLING_RELATIONSHIP_SELECTION = [
    ("direct", "Direct Shipper / Consignee"),
    ("interlining", "Interlining Carrier / Subcontract Customer"),
    ("manual_review", "Manual Review"),
]
TAX_TREATMENT_SELECTION = [
    ("automatic", "Automatic"),
    ("zero_rated_interlining", "Zero Rated Interlining"),
    ("manual_review", "Manual Review"),
]
BOOKING_CHANNEL_SELECTION = [
    ("customer_portal", "Customer Portal"),
    ("staff", "Staff"),
    ("phone", "Phone"),
    ("internal", "Internal Staff"),
    ("whatsapp", "WhatsApp"),
    ("wa_negotiation", "WA Negotiation"),
    ("invoice", "Invoice"),
    ("custom_quote", "Custom Quote"),
    ("recurring", "Recurring Agreement"),
    ("email", "Email"),
    ("api", "API"),
    ("imported", "Imported"),
]


class LogisticsBooking(models.Model):
    """WHAT THE CUSTOMER BOOKED — not a sale.order, not an invoice, not a
    quotation, not a dispatch job. Always created already-confirmed by
    `confirm_from_session`; there is no draft-booking UI state in Phase 1.
    """

    _name = "logistics.booking"
    _description = "Confirmed customer LTL/FTL booking"
    _order = "id desc"

    booking_number = fields.Char(readonly=True, copy=False, index=True)
    partner_id = fields.Many2one("res.partner", required=True, index=True)
    commercial_partner_id = fields.Many2one(
        related="partner_id.commercial_partner_id", store=True, index=True
    )
    state = fields.Selection(
        [("draft", "Draft"), ("quoted", "Quoted"),
         ("confirmed", "Confirmed"), ("planned", "Planned"),
         ("in_execution", "In Execution"), ("delivered", "Delivered"),
         ("completed", "Completed"), ("cancelled", "Cancelled"),
         ("exception", "Exception")],
        default="confirmed", required=True
    )

    pickup_fsa_id = fields.Many2one("logistics.fsa", index=True)
    delivery_fsa_id = fields.Many2one("logistics.fsa", index=True)

    pickup_company = fields.Char()
    pickup_address = fields.Char()
    pickup_contact_name = fields.Char()
    pickup_phone = fields.Char()
    pickup_instructions = fields.Text()

    delivery_company = fields.Char()
    delivery_address = fields.Char()
    delivery_contact_name = fields.Char()
    delivery_phone = fields.Char()
    delivery_instructions = fields.Text()

    service_offering_id = fields.Many2one("logistics.service.offering")
    rate_plan_id = fields.Many2one("logistics.rate.plan")
    equipment_profile_id = fields.Many2one("logistics.equipment.profile")

    pickup_date = fields.Date()
    estimated_delivery_date = fields.Date()

    # ── Phase 3: Canonical service selections ────────────────────────
    service_mode = fields.Selection([
        ("dedicated", "Dedicated"), ("expedited", "Expedited"),
    ], default="dedicated", required=True, string="Service Mode")
    load_type = fields.Selection([
        ("ltl", "LTL"), ("ftl", "FTL"),
    ], default="ltl", required=True, string="Load Type")
    equipment_requirement = fields.Selection([
        ("dry", "Dry"), ("reefer", "Reefer"),
    ], default="dry", required=True, string="Equipment")

    # Legacy fields — kept for migration compatibility
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, required=True)
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, required=True)
    pallets = fields.Integer(required=True)
    weight_lbs = fields.Float(required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean()

    calculated_price = fields.Float(readonly=True)
    price_snapshot = fields.Json(readonly=True)
    cost_snapshot = fields.Json(readonly=True, help="Frozen cost breakdown from Prema AI Estimator at confirmation time.")
    estimated_cost = fields.Float(readonly=True, help="Total estimated cost from Prema AI Estimator.")
    calculated_margin = fields.Float(readonly=True, compute="_compute_margin", store=True)
    margin_pct = fields.Float(readonly=True, compute="_compute_margin", store=True)

    pricing_session_token = fields.Char(readonly=True, copy=False, index=True)

    # ── Phase 8: Capacity Override ───────────────────────────────────
    capacity_override = fields.Boolean(
        string="Capacity Override",
        default=False,
        help="Dispatcher manually overrode the standard 12-pallet capacity limit."
    )
    override_by = fields.Many2one(
        "res.users", string="Override By", readonly=True,
        help="Dispatcher who authorized the capacity override."
    )
    override_reason = fields.Text(
        string="Override Reason",
        help="Reason for exceeding standard capacity (e.g., pinwheel layout, light freight)."
    )

    dispatch_job_id = fields.Many2one("prema.dispatch.job", readonly=True, copy=False)
    invoice_id = fields.Many2one("account.move", readonly=True, copy=False, string="Draft Invoice")
    wa_negotiation_id = fields.Many2one("premafirm.wa.negotiation", readonly=True, copy=False, string="WA Negotiation")
    sale_order_id = fields.Many2one("sale.order", readonly=True, copy=False, string="Sale Order")
    route_run_id = fields.Many2one("logistics.route.run", readonly=True, copy=False, string="Route Run [DEPRECATED]",
                                    help="DEPRECATED — use departure_id instead.")
    departure_id = fields.Many2one("logistics.corridor.departure", readonly=True, copy=False,
                                    string="Corridor Departure",
                                    help="The scheduled corridor departure this booking is assigned to.")
    corridor_id = fields.Many2one("logistics.corridor", readonly=True, copy=False,
                                   string="Corridor", related="departure_id.corridor_id", store=True)

    # ── Phase 5: Multi-Leg Bookings ───────────────────────────────────
    is_multi_leg = fields.Boolean(
        string="Multi-Leg Shipment", default=False,
        help="This booking requires multiple operational legs (e.g., hub transfer)."
    )
    leg_ids = fields.One2many("logistics.booking.leg", "booking_id", string="Legs")

    recurring_agreement_id = fields.Many2one("logistics.recurring.agreement", readonly=True, copy=False, string="Recurring Agreement")
    confirmed_at = fields.Datetime(readonly=True, default=fields.Datetime.now)

    booking_channel = fields.Selection(BOOKING_CHANNEL_SELECTION, default="customer_portal", string="Booking Channel")
    source_channel = fields.Char(
        string="Source Channel", readonly=True, copy=False, index=True,
        help="Canonical source channel: portal, phone, internal, invoice, custom_quote, recurring, whatsapp, api."
    )
    source_model = fields.Char(
        string="Source Model", readonly=True, copy=False,
        help="Model name of the originating record (e.g., account.move, premafirm.wa.negotiation)."
    )
    source_res_id = fields.Integer(
        string="Source Record ID", readonly=True, copy=False, index=True,
        help="Database ID of the originating record."
    )
    source_reference = fields.Char(
        string="Source Reference", readonly=True, copy=False,
        help="Human-readable reference of the originating record."
    )
    idempotency_key = fields.Char(
        string="Idempotency Key", readonly=True, copy=False, index=True,
        help="Unique key preventing duplicate booking creation. Format: {channel}:{unique_id}."
    )
    tracking_token = fields.Char(
        string="Tracking Token", readonly=True, copy=False, index=True,
        default=lambda self: secrets.token_urlsafe(32),
        help="High-entropy random token for public tracking. Must be provided alongside booking_number to view shipment status."
    )
    required_temperature = fields.Char(string="Required Temperature (display)", help="e.g. '-18°C', '+4°C'. For backward compatibility.")
    required_temperature_c = fields.Float(string="Required Temperature °C", help="Numeric temperature in Celsius. e.g. -18.0, 4.0")
    po_number = fields.Char(string="PO Number")
    customer_reference = fields.Char(string="Customer Reference")
    commodity = fields.Char(string="Commodity")
    # Tax snapshot (populated at confirmation, immutable afterward)
    billing_relationship = fields.Selection(BILLING_RELATIONSHIP_SELECTION, readonly=True, string="Billing Relationship")
    tax_treatment = fields.Selection(TAX_TREATMENT_SELECTION, readonly=True, string="Tax Treatment")
    tax_reason = fields.Char(readonly=True, string="Tax Reason")
    tax_rule_id = fields.Many2one("account.tax", readonly=True, string="Applied Tax Rule")
    tax_rule_name = fields.Char(readonly=True, string="Tax Rule Name")
    tax_review_required = fields.Boolean(readonly=True, string="Tax Review Required")
    amount_untaxed = fields.Float(readonly=True, string="Subtotal")
    amount_tax = fields.Float(readonly=True, string="Tax")
    amount_total = fields.Float(readonly=True, string="Total")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id, readonly=True)
    fiscal_position_id = fields.Many2one("account.fiscal.position", readonly=True, string="Fiscal Position")
    invoicing_carrier_partner_id = fields.Many2one("res.partner", readonly=True, string="Invoicing Carrier")
    final_customer_partner_id = fields.Many2one("res.partner", readonly=True, string="Final Customer")

    invoice_created_at = fields.Datetime(readonly=True, copy=False)

    stop_ids = fields.One2many("logistics.booking.stop", "booking_id", string="Stops")
    line_ids = fields.One2many("logistics.booking.line", "booking_id")

    _sql_constraints = [
        ("pricing_session_token_uniq", "unique(pricing_session_token)",
         "A booking already exists for this pricing session (idempotency guard)."),
        ("booking_idempotency_uniq", "unique(source_channel, idempotency_key)",
         "A booking already exists for this idempotency key (duplicate prevention)."),
    ]

    @api.depends("calculated_price", "estimated_cost")
    def _compute_margin(self):
        for rec in self:
            rec.calculated_margin = rec.calculated_price - (rec.estimated_cost or 0.0)
            rec.margin_pct = (rec.calculated_margin / rec.calculated_price * 100.0) if rec.calculated_price > 0 else 0.0

    # ------------------------------------------------------------------
    # The atomic confirmation transaction (steps mirror the approved plan).
    # ------------------------------------------------------------------
    @api.model
    def confirm_from_session(self, token, address_vals):
        """address_vals keys: pickup_company, pickup_postal_code, pickup_address,
        pickup_contact_name, pickup_phone, pickup_instructions, and the
        delivery_* equivalents. Returns the confirmed logistics.booking.
        Raises UserError/AccessError with a customer-safe message otherwise.
        """
        user_partner = self.env.user.partner_id
        commercial = user_partner.commercial_partner_id

        # 1/2. authenticate + pricing-approval status (group membership is
        # the technical gate at the controller; this is the belt-and-suspenders
        # business-state check inside the transaction itself).
        if commercial.logistics_pricing_status != "approved":
            raise AccessError(_("Your account is not approved for booking."))

        Session = self.env["logistics.pricing.session"].sudo()
        session = Session.search([("token", "=", token)], limit=1)
        if not session:
            raise UserError(_("This price is no longer available. Please get a new price."))

        # 3. ownership of the pricing session
        if session.partner_id.commercial_partner_id.id != commercial.id:
            raise AccessError(_("This pricing result does not belong to your account."))

        # 4. idempotency pre-check -- a repeat/duplicate confirm for the same
        # session must return the SAME booking, never create a second one.
        existing = self.sudo().search([("pricing_session_token", "=", token)], limit=1)
        if existing:
            return existing

        # 5. expiry
        if session.is_expired():
            raise UserError(_("This price has expired. Please get a new price."))

        # 6. re-resolve FSA from the FINAL entered addresses -- must match
        # what was quoted, or the customer must be forced to re-price.
        # sudo(): reference-table read, not a customer-data read -- see
        # PricingService for why this is the correct pattern here.
        Fsa = self.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.resolve_from_postal(address_vals.get("pickup_postal_code"))
        delivery_fsa = Fsa.resolve_from_postal(address_vals.get("delivery_postal_code"))
        if not pickup_fsa or pickup_fsa.id != session.pickup_fsa_id.id:
            raise UserError(_("Your pickup address doesn't match the quoted area. Please get a new price."))
        if not delivery_fsa or delivery_fsa.id != session.delivery_fsa_id.id:
            raise UserError(_("Your delivery address doesn't match the quoted area. Please get a new price."))

        # 7/8/9. Price integrity: use the STORED quote price, not a fresh
        # recalculation. In the Scheduled Shared LTL model, pricing is
        # calculated ONCE at quote time and stored immutably in the session.
        # Step 3 (quote) and Step 4 (confirm) MUST display the same price.
        #
        # Belt-and-suspenders: verify the rate plan from the session still
        # exists and is active. If it's been superseded by a newer version,
        # we still honor the quoted price — the version was valid at quote
        # time and customers are entitled to the price they were shown.
        if not session.rate_plan_id or not session.rate_plan_id.active:
            raise UserError(_(
                "The rate plan for this quote is no longer active. "
                "Please get a new price."
            ))
        if not session.service_offering_id:
            raise UserError(_("This quote is incomplete. Please get a new price."))

        # Build a lightweight result-like object from the session for the
        # remaining steps (capacity check, cost estimate, booking fields).
        # We do NOT re-run PricingService.calculate() — the price shown at
        # Step 3 IS the price charged at Step 4.
        lane = session.rate_plan_id.lane_id

        # Re-verify schedule/service is still available (operational check
        # only — this does NOT recalculate price).
        pricing = PricingService(self.env)
        verify = pricing.calculate(
            pickup_fsa, delivery_fsa, session.shipment_type,
            session.temperature_mode, session.pallets, session.weight_lbs,
            session.liftgate_pickup, session.liftgate_delivery,
            session.appointment, session.residential,
            session.same_day_requested, partner=user_partner,
        )
        if not verify.available:
            raise UserError(_(
                "This service is no longer available. "
                "Please get a new price."
            ))

        # 10. real operational capacity check (Layer 2)
        bridge = AvailabilityBridge(self.env)
        capacity = bridge.check_real_capacity(
            address_vals.get("pickup_address"), address_vals.get("delivery_address"),
            verify.pickup_date, session.pallets, session.weight_lbs,
            session.temperature_mode != "dry",
            session.liftgate_pickup or session.liftgate_delivery,
        )
        if not capacity["feasible"]:
            raise UserError(_(
                "The selected pickup date is no longer available. "
                "Please get a new price to see the next available date."
            ))

        # 11/12/13. idempotent create — DB unique constraint is the real
        # guard against a concurrent duplicate submit racing past the
        # pre-check above.
        booking_number = self._generate_booking_number()
        vals = {
            "booking_number": booking_number,
            "partner_id": user_partner.id,
            "pickup_fsa_id": pickup_fsa.id,
            "delivery_fsa_id": delivery_fsa.id,
            "pickup_company": address_vals.get("pickup_company"),
            "pickup_address": address_vals.get("pickup_address"),
            "pickup_contact_name": address_vals.get("pickup_contact_name"),
            "pickup_phone": address_vals.get("pickup_phone"),
            "pickup_instructions": address_vals.get("pickup_instructions"),
            "delivery_company": address_vals.get("delivery_company"),
            "delivery_address": address_vals.get("delivery_address"),
            "delivery_contact_name": address_vals.get("delivery_contact_name"),
            "delivery_phone": address_vals.get("delivery_phone"),
            "delivery_instructions": address_vals.get("delivery_instructions"),
            # Use session-stored values for pricing fields (immutable quote)
            "service_offering_id": session.service_offering_id.id,
            "rate_plan_id": session.rate_plan_id.id,
            "equipment_profile_id": lane.equipment_profile_id.id or False,
            "pickup_date": session.pickup_date,
            "estimated_delivery_date": session.delivery_date_estimate,
            "shipment_type": session.shipment_type,
            "temperature_mode": session.temperature_mode,
            "pallets": session.pallets,
            "weight_lbs": session.weight_lbs,
            "liftgate_pickup": session.liftgate_pickup,
            "liftgate_delivery": session.liftgate_delivery,
            "appointment": session.appointment,
            "residential": session.residential,
            "same_day_requested": session.same_day_requested,
            # THE KEY FIX: use session's stored price, not a recalculated one
            "calculated_price": session.calculated_price,
            "price_snapshot": session.price_snapshot,
            "pricing_session_token": token,
            "line_ids": [(0, 0, {
                "description": (
                    "LTL Shipment"
                    if session.shipment_type == "ltl"
                    else "FTL Shipment"
                ),
                "pallets": session.pallets,
                "weight_lbs": session.weight_lbs,
            })],
        }

        # Compute cost estimate via Prema AI Estimator (internal margin
        # tracking only — NEVER shown to customer)
        cost_info = self._estimate_cost(lane, address_vals, session)
        vals["cost_snapshot"] = cost_info.get("breakdown", {})
        vals["estimated_cost"] = cost_info.get("total_cost", 0.0)

        # 15-18. Single savepoint: booking + tax + invoice + dispatch
        # ALL must succeed or ALL roll back — no orphan bookings.
        # ORDER MATTERS: tax before invoice (so tax line is on the invoice),
        # invoice before dispatch (so job.invoice_id is populated at creation).
        try:
            with self.env.cr.savepoint():
                booking = self.sudo().create(vals)
                booking._apply_tax_decision()
                booking._create_draft_invoice()
                booking._create_dispatch_job()
                session.write({"state": "converted"})
        except UniqueViolation:
            existing = self.sudo().search([("pricing_session_token", "=", token)], limit=1)
            if existing:
                return existing
            raise

        return booking

    def _generate_booking_number(self):
        seq = self.env["ir.sequence"].sudo().next_by_code("logistics.booking") or "0"
        date_part = datetime.date.today().strftime("%y%m%d")
        return f"PF-{date_part}-{int(seq):06d}"

    def _estimate_cost_from_request(self, normalized_request, pickup_fsa=None, delivery_fsa=None):
        """Estimate cost from a NormalizedBookingRequest (used by BookingOrchestrationService).
        Gracefully degrades if estimator is unavailable."""
        try:
            from odoo.addons.premafirm_ai_engine.services.pricing_engine import PricingEngine
            vehicle = self.env["fleet.vehicle"].sudo().search([
                ("active", "=", True),
                ("x_operational_logistics", "=", True),
            ], limit=1)
            if not vehicle:
                return {"total_cost": 0.0, "breakdown": {"error": "no_vehicle_available"}}

            # Estimate distance from lane or fallback
            distance_km = 200.0
            if pickup_fsa and delivery_fsa:
                lane = self.env["logistics.lane"].sudo().search([
                    ("origin_region_id", "=", pickup_fsa.region_id.id),
                    ("destination_region_id", "=", delivery_fsa.region_id.id),
                ], limit=1)
                if lane:
                    distance_km = lane.road_km or 200.0

            duration_hrs = distance_km / 80.0
            engine = PricingEngine(self.env)
            costs = engine.calculate(
                vehicle.id, distance_km, duration_hrs,
                load_weight_lbs=normalized_request.pallets * 800.0 if hasattr(normalized_request, 'pallets') else 800.0,
            )
            return {
                "total_cost": costs["total_cost"],
                "breakdown": {
                    "fuel": costs["fuel_cost"],
                    "maintenance": costs["maintenance_cost"],
                    "insurance": costs["insurance_cost"],
                    "driver": costs["driver_cost"],
                    "vehicle": vehicle.name,
                    "distance_km": distance_km,
                },
            }
        except Exception:
            return {"total_cost": 0.0, "breakdown": {"error": "estimator_unavailable"}}

    def _estimate_cost(self, lane, address_vals, session):
        """Call Prema AI Estimator for route cost estimation.
        Gracefully degrades if estimator is unavailable.
        Internal margin tracking only — NEVER shown to customers."""
        try:
            from odoo.addons.premafirm_ai_engine.services.pricing_engine import PricingEngine
            vehicle = self.env["fleet.vehicle"].sudo().search([
                ("active", "=", True), ("equipment_profile_id", "!=", False),
            ], limit=1)
            if not vehicle:
                return {"total_cost": 0.0, "breakdown": {"error": "no_vehicle_available"}}

            distance_km = lane.road_km or 200.0
            duration_hrs = distance_km / 80.0  # ~80 km/h average

            engine = PricingEngine(self.env)
            costs = engine.calculate(
                vehicle.id, distance_km, duration_hrs,
                load_weight_lbs=session.pallets * 800.0,
            )
            return {
                "total_cost": costs["total_cost"],
                "breakdown": {
                    "fuel": costs["fuel_cost"],
                    "maintenance": costs["maintenance_cost"],
                    "insurance": costs["insurance_cost"],
                    "driver": costs["driver_cost"],
                    "vehicle": vehicle.name,
                    "distance_km": distance_km,
                },
            }
        except Exception:
            return {"total_cost": 0.0, "breakdown": {"error": "estimator_unavailable"}}

    def _create_dispatch_job(self):
        self.ensure_one()
        # Idempotency: never create a duplicate dispatch job
        if self.dispatch_job_id:
            return self.dispatch_job_id
        Job = self.env["prema.dispatch.job"].sudo()
        existing = Job.search([("source_model", "=", "logistics.booking"), ("source_res_id", "=", self.id)], limit=1)
        if existing:
            self.dispatch_job_id = existing.id
            return existing
        draft_stage = self.env["prema.dispatch.stage"].sudo().search([("stage_type", "=", "draft")], limit=1)
        job = Job.create({
            "partner_id": self.partner_id.id,
            "source_model": "logistics.booking",
            "source_res_id": self.id,
            "tracking_number": self.booking_number,
            "stage_id": draft_stage.id if draft_stage else False,
            "company_id": self.env.company.id,
            "invoice_id": self.invoice_id.id if self.invoice_id else False,
            "approximate_skids": self.pallets,
        })

        Stop = self.env["prema.dispatch.stop"].sudo()
        Item = self.env["prema.dispatch.item"].sudo()

        if self.stop_ids:
            # Multi-stop: create dispatch stops from booking stops in sequence
            created_stops = {}
            for bstop in self.stop_ids.sorted("sequence"):
                dispatch_stop_type = "pickup" if bstop.stop_type == "pickup" else "dropoff"
                addr = bstop.formatted_address or ", ".join(
                    p for p in [bstop.street, bstop.city, bstop.province_state] if p
                ) or ""
                dstop = Stop.create({
                    "job_id": job.id,
                    "stop_type": dispatch_stop_type,
                    "sequence": bstop.sequence,
                    "address": addr,
                    "contact_name": bstop.contact_name or "",
                    "contact_phone": bstop.phone or "",
                    "dispatcher_notes": bstop.instructions or "",
                })
                created_stops[bstop.id] = dstop

            # Create items linking pickup→delivery pairs
            pickups = [s for s in self.stop_ids if s.stop_type == "pickup"]
            deliveries = [s for s in self.stop_ids if s.stop_type == "delivery"]
            for line in self.line_ids:
                pu_stop = created_stops.get(pickups[0].id) if pickups else False
                del_stop = created_stops.get(deliveries[-1].id) if deliveries else False
                Item.create({
                    "job_id": job.id,
                    "name": line.description or "Skid",
                    "description": line.commodity or self.commodity or "",
                    "pallet_count": line.pallets,
                    "weight_lbs": line.weight_lbs,
                    "pickup_stop_id": pu_stop.id if pu_stop else False,
                    "delivery_stop_id": del_stop.id if del_stop else False,
                })
        else:
            # Legacy: single pickup + single delivery
            pickup_stop = Stop.create({
                "job_id": job.id,
                "stop_type": "pickup",
                "address": self.pickup_address,
                "sequence": 10,
                "contact_name": self.pickup_contact_name or "",
                "contact_phone": self.pickup_phone or "",
                "dispatcher_notes": self.pickup_instructions or "",
            })
            delivery_stop = Stop.create({
                "job_id": job.id,
                "stop_type": "dropoff",
                "address": self.delivery_address,
                "sequence": 20,
                "contact_name": self.delivery_contact_name or "",
                "contact_phone": self.delivery_phone or "",
                "dispatcher_notes": self.delivery_instructions or "",
            })
            for line in self.line_ids:
                Item.create({
                    "job_id": job.id,
                    "name": line.description or "Skid",
                    "description": line.commodity or self.commodity or "",
                    "pallet_count": line.pallets,
                    "weight_lbs": line.weight_lbs,
                    "pickup_stop_id": pickup_stop.id,
                    "delivery_stop_id": delivery_stop.id,
                })

        self.dispatch_job_id = job.id
        return job

    # ═══════════════════════════════════════════════════════════════════
    # Freight Tax Decision Engine
    # ═══════════════════════════════════════════════════════════════════

    def _resolve_freight_tax(self, partner, delivery_province):
        """Determine the correct freight tax based on billing relationship
        and final delivery province. Returns (account.tax record, reason string)."""
        ICP = self.env["ir.config_parameter"].sudo()

        # Step 1: Read contact's freight tax profile
        billing_rel = partner.x_freight_billing_relationship or "direct"
        tax_treatment = partner.x_freight_tax_treatment or "automatic"

        # Step 2: Manual review always wins
        if billing_rel == "manual_review" or tax_treatment == "manual_review":
            return None, "manual_review"

        # Step 3: Interlining → zero rated
        if billing_rel == "interlining" or tax_treatment == "zero_rated_interlining":
            tax_param = "logistics.freight_tax_zero_interlining_id"
            tax_id = int(ICP.get_param(tax_param, "0") or "0")
            if tax_id:
                tax = self.env["account.tax"].sudo().browse(tax_id)
                if tax.exists():
                    return tax, "zero_rated_interlining"
            return None, "zero_rated_interlining_no_tax_configured"

        # Step 4: Direct shipper → destination-based tax
        province = (delivery_province or "").strip().upper()
        tax_param_map = {
            "ON": "logistics.freight_tax_ontario_id",
            "QC": "logistics.freight_tax_quebec_id",
            "NS": "logistics.freight_tax_ns_id",
            "NB": "logistics.freight_tax_nb_id",
            "PE": "logistics.freight_tax_pei_id",
            "NL": "logistics.freight_tax_nl_id",
            "AB": "logistics.freight_tax_gst_id",
            "BC": "logistics.freight_tax_gst_id",
            "MB": "logistics.freight_tax_gst_id",
            "SK": "logistics.freight_tax_gst_id",
            "NT": "logistics.freight_tax_gst_id",
            "YT": "logistics.freight_tax_gst_id",
            "NU": "logistics.freight_tax_gst_id",
        }

        param_key = tax_param_map.get(province)
        if not param_key:
            # Try international / export tax
            int_tax_param = "logistics.freight_tax_zero_international_id"
            tax_id = int(ICP.get_param(int_tax_param, "0") or "0")
            if tax_id:
                tax = self.env["account.tax"].sudo().browse(tax_id)
                if tax.exists():
                    return tax, f"international_{province}"
            return None, f"unknown_province_{province}"

        tax_id = int(ICP.get_param(param_key, "0") or "0")
        if tax_id:
            tax = self.env["account.tax"].sudo().browse(tax_id)
            if tax.exists():
                return tax, f"destination_{province}"

        # No tax configured — create Accounting Review activity and keep invoice draft
        _logger.warning(
            "Booking %s: No freight tax configured for province %s (param %s). "
            "Invoice will remain draft pending manual tax review.",
            self.booking_number, province, param_key,
        )
        self._create_tax_missing_activity(province, param_key)
        return None, f"no_tax_configured_for_{province}"

    def _create_tax_missing_activity(self, province, param_key):
        """Create an Accounting Review activity when a required tax mapping is missing."""
        self.ensure_one()
        try:
            # Find users in Accounting or Logistics Manager groups
            managers = self.env["res.users"].sudo().search([
                ("groups_id", "in", [
                    self.env.ref("account.group_account_manager").id,
                    self.env.ref("prema_logistics_booking.group_logistics_booking_manager").id,
                ])
            ], limit=1)
            user_id = managers[0].id if managers else self.env.user.id

            self.activity_schedule(
                "mail.mail_activity_data_warning",
                summary=f"Freight Tax Not Configured: {province}",
                note=(
                    f"Booking {self.booking_number} requires freight tax for {province} "
                    f"(system parameter: {param_key}).\n\n"
                    f"Configure this tax in Settings → Prema Logistics → Freight Tax Configuration.\n"
                    f"The draft invoice has been created WITHOUT TAX and requires manual review."
                ),
                user_id=user_id,
            )
        except Exception as e:
            _logger.warning("Could not create tax-missing activity: %s", e)

    def _get_delivery_province(self):
        """Extract the final delivery province from booking stops or legacy fields."""
        self.ensure_one()
        # From multi-stop
        if self.stop_ids:
            deliveries = self.stop_ids.filtered(lambda s: s.stop_type == "delivery")
            last_del = deliveries.sorted("sequence")[-1] if deliveries else None
            if last_del and last_del.province_state:
                return last_del.province_state
        # From legacy field
        if self.delivery_address:
            import re
            match = re.search(r'\b(ON|QC|NS|NB|PE|NL|AB|BC|MB|SK|NT|YT|NU)\b', self.delivery_address, re.I)
            if match:
                return match.group(1).upper()
        return ""

    def _apply_tax_decision(self):
        """Run the freight tax decision engine and store the result on this booking.
        Must be called after booking creation, before invoice creation."""
        self.ensure_one()
        partner = self.commercial_partner_id
        province = self._get_delivery_province()

        tax, reason = self._resolve_freight_tax(partner, province)
        billing_rel = partner.x_freight_billing_relationship or "direct"

        # Check for multi-province deliveries (flag for manual review)
        if self.stop_ids:
            delivery_provinces = set(
                s.province_state.strip().upper()
                for s in self.stop_ids
                if s.stop_type == "delivery" and s.province_state
            )
            if len(delivery_provinces) > 1:
                self.write({
                    "tax_review_required": True,
                    "tax_reason": f"multi_province_deliveries_{','.join(sorted(delivery_provinces))}",
                })
                return

        # Quebec-only: flag for manual review
        if province == "QC" and billing_rel == "direct":
            if not self.stop_ids or len(self.stop_ids.filtered(lambda s: s.stop_type == "delivery")) <= 1:
                self.write({"tax_review_required": True, "tax_reason": "QC_manual_review"})
                return

        self.write({
            "billing_relationship": billing_rel,
            "tax_treatment": partner.x_freight_tax_treatment or "automatic",
            "tax_reason": reason,
            "tax_rule_id": tax.id if tax else False,
            "tax_rule_name": tax.name if tax else "",
            "tax_review_required": not bool(tax),
        })

    # ═══════════════════════════════════════════════════════════════════
    # Invoice Creation
    # ═══════════════════════════════════════════════════════════════════

    def _select_freight_product(self):
        """Read the configured freight product from system parameters.
        Returns (product, country_code) or (None, None) if mapping missing."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()

        # Determine country from pickup/delivery FSA regions
        lane = self.rate_plan_id.lane_id
        origin_region = lane.origin_region_id
        # Default to CA; flag as US if origin region indicates US
        country = "CA"
        if origin_region and hasattr(origin_region, "country_id") and origin_region.country_id:
            country = origin_region.country_id.code or "CA"

        is_reefer = self.temperature_mode in ("reefer", "chilled", "frozen")

        if country == "CA":
            param_key = "logistics.product_ca_reefer_ltl_id" if is_reefer else "logistics.product_ca_dry_ltl_id"
        else:
            param_key = "logistics.product_us_reefer_ltl_id" if is_reefer else "logistics.product_us_dry_ltl_id"

        product_id = int(ICP.get_param(param_key, "0") or "0")
        if product_id:
            product = self.env["product.product"].sudo().browse(product_id)
            if product.exists():
                return product, country
        return None, country

    def _generate_invoice_description(self):
        """Build a deterministic structured booking summary for the invoice.
        Uses stop_ids when available, falls back to legacy pickup/delivery fields."""
        self.ensure_one()
        lines = []
        lines.append("Freight / Delivery Service")
        temp_mode = self.temperature_mode or "Dry"
        lines.append(f"Service: {temp_mode.title()}")
        if self.required_temperature:
            lines.append(f"Required Temperature: {self.required_temperature}")
        if self.pickup_date:
            lines.append(f"Date: {self.pickup_date.strftime('%B %d, %Y')}")
        lines.append(f"Pallets: {self.pallets}")
        lines.append(f"Weight: {self.weight_lbs:,.0f} lb")
        if self.po_number:
            lines.append(f"PO: {self.po_number}")
        if self.customer_reference:
            lines.append(f"Reference: {self.customer_reference}")

        # Use stop_ids when available
        if self.stop_ids:
            pickups = self.stop_ids.filtered(lambda s: s.stop_type == "pickup")
            deliveries = self.stop_ids.filtered(lambda s: s.stop_type == "delivery")
            if pickups:
                lines.append("")
                for i, s in enumerate(pickups, 1):
                    lines.append(f"Pickup {i}:")
                    lines.append(f"{s.company_name or '—'}")
                    lines.append(f"{s.formatted_address or s.street or '—'}")
                    if s.unit:
                        lines.append(f"Unit: {s.unit}")
                    if s.pallet_count:
                        lines.append(f"Pallets: {s.pallet_count}")
                    if s.reference:
                        lines.append(f"Ref: {s.reference}")
                    if s.instructions:
                        lines.append(s.instructions)
            if deliveries:
                lines.append("")
                for i, s in enumerate(deliveries, 1):
                    lines.append(f"Delivery {i}:")
                    lines.append(f"{s.company_name or '—'}")
                    lines.append(f"{s.formatted_address or s.street or '—'}")
                    if s.unit:
                        lines.append(f"Unit: {s.unit}")
                    if s.pallet_count:
                        lines.append(f"Pallets: {s.pallet_count}")
                    lines.append(f"Liftgate: {'Yes' if s.liftgate_required else 'No'}")
                    if s.instructions:
                        lines.append(s.instructions)
        else:
            # Legacy single-pickup/single-delivery fallback
            lines.append("")
            lines.append("Pickup From:")
            lines.append(f"{self.pickup_company or '—'}")
            lines.append(f"{self.pickup_address}")
            if self.pickup_contact_name:
                lines.append(f"Contact: {self.pickup_contact_name}")
            lines.append(f"Pickup Liftgate: {'Yes' if self.liftgate_pickup else 'No'}")
            lines.append("")
            lines.append("Deliver To:")
            lines.append(f"{self.delivery_company or '—'}")
            lines.append(f"{self.delivery_address}")
            if self.delivery_contact_name:
                lines.append(f"Contact: {self.delivery_contact_name}")
            lines.append(f"Delivery Liftgate: {'Yes' if self.liftgate_delivery else 'No'}")
            if self.pickup_instructions:
                lines.append("")
                lines.append("Special Instructions:")
                lines.append(self.pickup_instructions)

        return "\n".join(lines)

    def _create_draft_invoice(self):
        """Create a draft customer invoice from this booking. Idempotent —
        returns existing invoice if one is already linked."""
        self.ensure_one()

        # Idempotency: never create a duplicate
        if self.invoice_id:
            return self.invoice_id
        existing = self.env["account.move"].sudo().search([
            ("logistics_booking_id", "=", self.id),
            ("move_type", "=", "out_invoice"),
        ], limit=1)
        if existing:
            self.invoice_id = existing.id
            return existing

        # Product mapping
        product, country = self._select_freight_product()
        if not product:
            # Missing product mapping — set booking for review, do NOT create zero-price invoice
            self.write({"state": "confirmed"})  # keep confirmed but flag
            _logger.error(
                "Booking %s (ID:%s): No product mapping configured for %s %s. "
                "Set the freight product in Settings → Prema AI → Logistics Settings.",
                self.booking_number, self.id,
                country, "Reefer" if self.temperature_mode in ("reefer", "chilled", "frozen") else "Dry",
            )
            return None

        # Build invoice description
        description = self._generate_invoice_description()

        # Create draft invoice using the resolved freight tax
        partner = self.commercial_partner_id
        fiscal_position = partner.property_account_position_id

        # Use the tax decided by the freight tax engine
        line_tax_ids = []
        if self.tax_rule_id:
            line_tax_ids = [(6, 0, [self.tax_rule_id.id])]

        Invoice = self.env["account.move"].sudo()
        invoice = Invoice.create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_origin": self.booking_number,
            "ref": self.po_number or self.customer_reference or "",
            "logistics_booking_id": self.id,
            "narration": description,
            "invoice_line_ids": [(0, 0, {
                "product_id": product.id,
                "name": description,
                "quantity": 1,
                "price_unit": self.calculated_price,
                "tax_ids": line_tax_ids,
            })],
        })

        # Store tax snapshot on booking
        invoice.invalidate_recordset()
        subtotal = self.calculated_price
        tax_amount = round(invoice.amount_total - subtotal, 2) if invoice.amount_total > 0 else 0.0

        self.write({
            "invoice_id": invoice.id,
            "invoice_created_at": fields.Datetime.now(),
            "amount_untaxed": subtotal,
            "amount_tax": tax_amount,
            "amount_total": invoice.amount_total or subtotal + tax_amount,
            "currency_id": invoice.currency_id.id,
            "fiscal_position_id": fiscal_position.id if fiscal_position else False,
        })

        # Try to apply AI description via the existing Generate from Text pipeline.
        # If it fails, the deterministic description is already valid.
        self._apply_ai_invoice_description(invoice, description)

        return invoice

    def _apply_ai_invoice_description(self, invoice, deterministic_description):
        """Use the existing Generate from Text pipeline to improve the invoice
        description.  AI failure must never break the invoice or change
        protected values.  Protected fields are restored after the AI call."""
        try:
            # Check that the AI service is reachable
            ICP = self.env["ir.config_parameter"].sudo()
            deepseek_key = ICP.get_param("deepseek.api_key", "")
            if not deepseek_key:
                _logger.info("Booking %s: AI key not configured, keeping deterministic description", self.booking_number)
                return

            # Capture protected values before AI
            line = invoice.invoice_line_ids[:1]
            protected = {
                "product_id": line.product_id.id if line else False,
                "price_unit": line.price_unit if line else 0,
                "quantity": line.quantity if line else 1,
                "tax_ids": [(6, 0, line.tax_ids.ids)] if line else [],
                "account_id": line.account_id.id if line and line.account_id else False,
                "partner_id": invoice.partner_id.id,
                "currency_id": invoice.currency_id.id,
                "logistics_booking_id": invoice.logistics_booking_id.id,
            }

            # Write the deterministic description into the x_whatsapp_text field
            # (the same field used by "Generate from Text")
            if hasattr(invoice, "x_whatsapp_text"):
                invoice.write({"x_whatsapp_text": deterministic_description})

            # Call the existing server-side AI invoice description generator
            if hasattr(invoice, "action_generate_from_whatsapp"):
                invoice.action_generate_from_whatsapp()

            # Restore protected values that AI may have changed
            restore_vals = {}
            line = invoice.invoice_line_ids[:1]
            if line:
                if protected["product_id"] and line.product_id.id != protected["product_id"]:
                    restore_vals["product_id"] = protected["product_id"]
                if abs(line.price_unit - protected["price_unit"]) > 0.005:
                    restore_vals["price_unit"] = protected["price_unit"]
                if line.quantity != protected["quantity"]:
                    restore_vals["quantity"] = protected["quantity"]
                # Restore tax_ids if changed
                current_tax_ids = line.tax_ids.ids
                saved_tax_ids = protected["tax_ids"][0][2] if protected["tax_ids"] and protected["tax_ids"][0][0] == 6 else []
                if set(current_tax_ids) != set(saved_tax_ids):
                    restore_vals["tax_ids"] = protected["tax_ids"]
                if protected["account_id"] and line.account_id.id != protected["account_id"]:
                    restore_vals["account_id"] = protected["account_id"]
                if restore_vals:
                    line.write(restore_vals)
            invoice_restore = {}
            if invoice.partner_id.id != protected["partner_id"]:
                invoice_restore["partner_id"] = protected["partner_id"]
            if invoice.logistics_booking_id.id != protected["logistics_booking_id"]:
                invoice_restore["logistics_booking_id"] = protected["logistics_booking_id"]
            if invoice.currency_id.id != protected["currency_id"]:
                invoice_restore["currency_id"] = protected["currency_id"]
            if invoice_restore:
                invoice.write(invoice_restore)

        except Exception as e:
            _logger.warning(
                "Booking %s: AI invoice description failed (%s), keeping deterministic description",
                self.booking_number, e,
            )

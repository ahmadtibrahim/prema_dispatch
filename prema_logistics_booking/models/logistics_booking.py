import datetime
import logging
import secrets

import pytz

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..constants import SERVICE_MODE, LOAD_TYPE, EQUIPMENT_REQUIREMENT
from ..services.pricing_service import PricingService

_logger = logging.getLogger(__name__)

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("reefer", "Reefer")]
# Legacy values preserved for historical compatibility
LEGACY_TEMPERATURE_MODE = [("dry", "Dry"), ("chilled", "Chilled"), ("frozen", "Frozen")]
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
    name = fields.Char(compute="_compute_name", store=True, string="Name")
    booking_name = fields.Char(
        compute="_compute_booking_name", store=True, string="Booking Name",
        help="Human-readable name from ordered stop cities (e.g. Brampton → Belleville).",
    )

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
    customer_status = fields.Char(
        compute="_compute_customer_status", store=False,
        string="Customer Status",
        help="Customer-facing status label (e.g. Confirmed, Scheduled, Driver Assigned).",
    )

    # ── Cancellation ──────────────────────────────────────────────
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    cancelled_by = fields.Many2one("res.users", readonly=True, copy=False)
    cancellation_source = fields.Selection([
        ("customer", "Customer"), ("company", "PremaFirm"), ("system", "System"),
    ], readonly=True, copy=False)
    cancellation_reason = fields.Text(readonly=True, copy=False)

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
    service_mode = fields.Selection(SERVICE_MODE, default="dedicated", required=True)
    load_type = fields.Selection(LOAD_TYPE, default="ltl", required=True)
    equipment_requirement = fields.Selection(EQUIPMENT_REQUIREMENT, default="dry", required=True)

    # Legacy fields — kept for migration compatibility
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, required=True)
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, required=True)
    pallets = fields.Integer(required=True)
    physical_pallets = fields.Integer(default=1)
    shared_pallet_mode = fields.Boolean(default=False)
    weight_lbs = fields.Float(required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean()

    calculated_price = fields.Float(readonly=True)
    price_snapshot = fields.Json(readonly=True)
    route_snapshot = fields.Json(readonly=True, string="Route Snapshot",
        help="Immutable route details frozen at confirmation: corridors, departures, trucks, distances, and prices.")
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

    # ── Booking architecture discriminator ──────────────────────────
    # legacy: booking created before/without the canonical pallet-movement
    #         architecture. Dispatch uses the legacy leg/snapshot bridge,
    #         EVEN IF compatibility pallet rows were backfilled by
    #         migration 18.0.11.0.0. Pallet rows alone are never a bridge
    #         selector.
    # movement_v1: booking created through the generalized milk-run flow
    #         (portal route builder / movement-aware channels). Dispatch
    #         uses the canonical movement bridge: ONE route job, ordered
    #         operational stops, per-pallet items with pickup/delivery
    #         stop references.
    route_model_version = fields.Selection(
        [("legacy", "Legacy"), ("movement_v1", "Movement V1")],
        string="Route Model Version", default="legacy", required=True, index=True,
        help="Architecture discriminator for dispatch creation. Existing "
             "bookings are legacy by default; only new generalized "
             "milk-run bookings are movement_v1. Compatibility pallet "
             "rows never change this on their own.",
    )
    pallet_ids = fields.One2many(
        "logistics.booking.pallet", "booking_id",
        string="Physical Pallet Movements",
        help="Canonical physical pallet movements (movement_v1). For "
             "legacy bookings these rows — when present — are a "
             "compatibility view only and never drive dispatch.",
    )

    dispatch_job_id = fields.Many2one("prema.dispatch.job", readonly=True, copy=False)
    dispatch_job_ids = fields.One2many(
        "prema.dispatch.job", "logistics_booking_id", string="Dispatch Planner Jobs", readonly=True,
    )
    invoice_id = fields.Many2one("account.move", readonly=True, copy=False, string="Draft Invoice")
    wa_negotiation_id = fields.Many2one("premafirm.wa.negotiation", readonly=True, copy=False, string="WA Negotiation")
    sale_order_id = fields.Many2one("sale.order", readonly=True, copy=False, string="Sale Order")
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
    recurring_job_id = fields.Many2one(
        "logistics.recurring.job", readonly=True, copy=False, string="Recurring Job", index=True,
    )
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

    @api.depends("booking_number")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.booking_number or f"Booking {rec.id}"

    @api.depends("stop_ids.sequence", "stop_ids.stop_type", "stop_ids.city")
    def _compute_booking_name(self):
        for rec in self:
            cities = []
            for stop in rec.stop_ids.filtered(
                lambda s: s.stop_type in ("pickup", "delivery")
            ).sorted("sequence"):
                city = (stop.city or "").strip()
                if city and (not cities or city != cities[-1]):
                    cities.append(city)
            rec.booking_name = " → ".join(cities) if cities else (rec.booking_number or f"Booking {rec.id}")

    def _compute_customer_status(self):
        for rec in self:
            status_map = {
                "draft": "Quote", "quoted": "Quote",
                "confirmed": "Confirmed", "planned": "Scheduled",
                "in_execution": "Out for Delivery", "delivered": "Completed",
                "completed": "Completed", "cancelled": "Cancelled",
                "exception": "Exception",
            }
            rec.customer_status = status_map.get(rec.state, rec.state)

    @api.depends("calculated_price", "estimated_cost")
    def _compute_margin(self):
        for rec in self:
            rec.calculated_margin = rec.calculated_price - (rec.estimated_cost or 0.0)
            rec.margin_pct = (rec.calculated_margin / rec.calculated_price * 100.0) if rec.calculated_price > 0 else 0.0

    # ------------------------------------------------------------------
    # The atomic confirmation transaction (steps mirror the approved plan).
    # ------------------------------------------------------------------

    def _get_pallet_allocations(self):
        """Extract pallet_allocations from price_snapshot (zero-migration approach).
        Returns list of {pallet, stops, shared} dicts, or empty list."""
        return self._extract_pallet_allocs_from_snapshot(self.price_snapshot)

    @staticmethod
    def _extract_pallet_allocs_from_snapshot(snapshot):
        ps = snapshot or []
        for entry in ps:
            if isinstance(entry, dict) and "_pallet_allocs" in entry:
                return entry["_pallet_allocs"] or []
        return []

    @staticmethod
    def _extract_pallet_movements_from_snapshot(snapshot):
        """Canonical pallet movements stored in the pricing snapshot by the
        generalized portal route builder. Presence (not merely pallet rows)
        is what marks a NEW booking as movement_v1. Sessions without this
        entry are legacy, period."""
        ps = snapshot or []
        for entry in ps:
            if isinstance(entry, dict) and "_pallet_movements" in entry:
                return entry["_pallet_movements"] or []
        return []

    # ── Booking Legs from Route Snapshot ──────────────────────────────
    # ------------------------------------------------------------------

    def _build_confirm_stops_from_session(self, session):
        """Build (pickup_stops, delivery_stops) from the ordered session
        stop list for generalized milk-run confirmations.

        Each stop keeps its stable stop_key so pallet movements resolve
        against persistent booking stops. Operating hours are SNAPSHOTTED
        NOW from the master saved location — later edits to the location's
        hours never change this booking's planning."""
        from ..services.itinerary_planner import snapshot_saved_location_hours
        pickups, deliveries = [], []
        for stop in session.stop_ids.sorted("sequence"):
            sl = stop.saved_location_id
            dispatch_loc_id = sl.dispatch_location_id.id if sl and sl.dispatch_location_id else None
            values = {
                "stop_key": stop.stop_key or "",
                "company_name": sl.business_name or sl.name if sl else stop.location_name or "",
                "street": sl.street if sl else stop.street or "",
                "city": sl.city if sl else stop.city or "",
                "province_state": sl.state_id.code if sl and sl.state_id else stop.state_code or "",
                "postal_code": sl.postal_code if sl else stop.postal_code or "",
                "formatted_address": sl.street if sl else stop.street or "",
                "latitude": sl.latitude if sl and sl.latitude else stop.latitude,
                "longitude": sl.longitude if sl and sl.longitude else stop.longitude,
                "google_place_id": sl.google_place_id if sl else "",
                "contact_name": sl.contact_name if sl else "",
                "phone": sl.contact_phone if sl else "",
                "instructions": stop.instructions or "",
                "pallet_count": stop.pallets or 1,
                "weight_lb": stop.weight_lbs or 0.0,
                # Stop-level requirements — the stop carries them, never
                # the booking-level legacy flags.
                "liftgate_required": stop.liftgate_required,
                "dock_available": stop.dock_available,
                "appointment_required": stop.appointment_required,
                "timing_type": stop.timing_type or "flexible",
                "window_start": stop.window_start,
                "window_end": stop.window_end,
                "appointment_time": stop.appointment_time,
                "service_time_minutes": stop.service_time_minutes or 15,
                "timezone": stop.timezone or (sl.timezone if sl else "America/Toronto"),
                # Fresh snapshot at confirmation (spec: freeze current
                # facility hours onto the persistent booking stop).
                "operating_hours_snapshot": snapshot_saved_location_hours(
                    self.env, sl, stop.stop_type,
                ),
                # CRITICAL: saved_location_id = prema.dispatch.location
                # (master facility); logistics_saved_location_id = customer
                # profile record.
                "saved_location_id": dispatch_loc_id,
                "logistics_saved_location_id": sl.id if sl else None,
            }
            if stop.stop_type == "pickup":
                pickups.append(values)
            else:
                deliveries.append(values)
        return pickups, deliveries

    def _build_confirm_delivery_stops(self, session, address_vals):
        """Build delivery_stops list for confirm_from_session, supporting
        multi-stop with per-stop pallets and shared-pallet mode.

        CRITICAL: logistics.booking.stop.saved_location_id points to
        prema.dispatch.location (the master facility), NOT to
        logistics.saved.location. We resolve dispatch_location_id from the
        saved location and snapshot the address/contact data at confirm time.
        """
        stops_data = address_vals.get("delivery_stops_data") or []
        if stops_data:
            stops = []
            for sd in stops_data:
                sl = self.env["logistics.saved.location"].browse(sd.get("saved_location_id") or 0)
                session_stop = session.delivery_stop_ids.filtered(
                    lambda s, seq=sd.get("sequence", 0): s.sequence == seq
                )[:1]
                # Resolve the master dispatch location from the customer saved location
                dispatch_loc_id = sl.dispatch_location_id.id if sl and sl.dispatch_location_id else None
                stops.append({
                    "company_name": sl.business_name or sl.name if sl else "",
                    "street": sl.street if sl else "",
                    "city": sl.city if sl else "",
                    "province_state": sl.state_id.code if sl and sl.state_id else "",
                    "postal_code": sl.postal_code if sl else "",
                    "formatted_address": sl.street if sl else "",
                    "latitude": sl.latitude if sl else 0.0,
                    "longitude": sl.longitude if sl else 0.0,
                    "google_place_id": sl.google_place_id if sl else "",
                    "contact_name": sd.get("contact_name") or (sl.contact_name if sl else ""),
                    "phone": sd.get("phone") or (sl.contact_phone if sl else ""),
                    "instructions": sd.get("instructions") or (sl.delivery_instructions if sl else ""),
                    "dock_available": bool(sl.dock_info) if sl else False,
                    "pallet_count": session_stop.pallets if session_stop else 1,
                    "weight_lb": session_stop.weight_lbs if session_stop else 500,
                    "liftgate_required": session.liftgate_delivery or (sl.liftgate_required if sl else False),
                    # CRITICAL: saved_location_id = prema.dispatch.location (master facility)
                    "saved_location_id": dispatch_loc_id,
                    # logistics_saved_location_id = logistics.saved.location (customer profile)
                    "logistics_saved_location_id": sl.id if sl else None,
                    "shared_pallet": bool(session_stop.shared_pallet) if session_stop else False,
                })
            return stops
        # Single-stop fallback
        de_loc = session.delivery_saved_location_id
        dispatch_loc_id = de_loc.dispatch_location_id.id if de_loc and de_loc.dispatch_location_id else None
        return [{
            "company_name": de_loc.business_name or de_loc.name if de_loc else "",
            "street": de_loc.street if de_loc else "",
            "city": de_loc.city if de_loc else "",
            "province_state": de_loc.state_id.code if de_loc and de_loc.state_id else "",
            "postal_code": de_loc.postal_code if de_loc else "",
            "formatted_address": de_loc.street if de_loc else "",
            "latitude": de_loc.latitude if de_loc else 0.0,
            "longitude": de_loc.longitude if de_loc else 0.0,
            "google_place_id": de_loc.google_place_id if de_loc else "",
            "contact_name": address_vals.get("delivery_contact_name") or (de_loc.contact_name if de_loc else ""),
            "phone": address_vals.get("delivery_phone") or (de_loc.contact_phone if de_loc else ""),
            "instructions": address_vals.get("delivery_instructions") or (de_loc.delivery_instructions if de_loc else ""),
            "dock_available": bool(de_loc.dock_info) if de_loc else False,
            "pallet_count": session.physical_pallets or session.pallets,
            "weight_lb": session.weight_lbs,
            "liftgate_required": session.liftgate_delivery or (de_loc.liftgate_required if de_loc else False),
            "saved_location_id": dispatch_loc_id,
            "logistics_saved_location_id": de_loc.id if de_loc else None,
            "shared_pallet": False,
        }]

    @api.model
    def confirm_from_session(self, token, address_vals):
        """Confirm a portal quote through the canonical orchestration service."""
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

        # A repeat/duplicate confirm for the same session must return the
        # same booking, never create a second one.
        existing = self.sudo().search([("pricing_session_token", "=", token)], limit=1)
        if existing:
            return existing

        # 5. expiry
        if session.is_expired():
            raise UserError(_("This price has expired. Please get a new price."))
        if (session.route_snapshot or {}).get("pricing_authority") != "corridor_per_km":
            raise UserError(_(
                "This quote used the retired pricing setup. Please get a new corridor-based price."
            ))

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

        if not (session.route_snapshot or {}).get("legs"):
            raise UserError(_("This quote has no scheduled departure. Please get a new price."))

        from ..services.booking_orchestration_service import BookingOrchestrationService

        svc = BookingOrchestrationService(self.env)
        movements = self._extract_pallet_movements_from_snapshot(session.price_snapshot)
        pu_loc = session.pickup_saved_location_id
        pu_dispatch_id = pu_loc.dispatch_location_id.id if pu_loc and pu_loc.dispatch_location_id else None
        if movements:
            # Generalized milk-run: pickup AND delivery stops come from the
            # ordered session stop list (stable stop keys, per-stop
            # requirements, operating-hours snapshot re-read from the
            # master saved locations at confirmation).
            pickup_stops, delivery_stops = self._build_confirm_stops_from_session(session)
        else:
            pickup_stops = [{
                "company_name": pu_loc.business_name or pu_loc.name if pu_loc else "",
                "street": pu_loc.street if pu_loc else "",
                "city": pu_loc.city if pu_loc else "",
                "province_state": pu_loc.state_id.code if pu_loc and pu_loc.state_id else "",
                "postal_code": pu_loc.postal_code if pu_loc else address_vals.get("pickup_postal_code") or "",
                "formatted_address": pu_loc.street if pu_loc else address_vals.get("pickup_address") or "",
                "latitude": pu_loc.latitude if pu_loc else 0.0,
                "longitude": pu_loc.longitude if pu_loc else 0.0,
                "google_place_id": pu_loc.google_place_id if pu_loc else "",
                "contact_name": address_vals.get("pickup_contact_name") or (pu_loc.contact_name if pu_loc else ""),
                "phone": address_vals.get("pickup_phone") or (pu_loc.contact_phone if pu_loc else ""),
                "instructions": address_vals.get("pickup_instructions") or (pu_loc.pickup_instructions if pu_loc else ""),
                "pallet_count": session.physical_pallets or session.pallets,
                "weight_lb": session.weight_lbs,
                "liftgate_required": session.liftgate_pickup or (pu_loc.liftgate_required if pu_loc else False),
                # CRITICAL: saved_location_id = prema.dispatch.location (master facility)
                "saved_location_id": pu_dispatch_id,
                # logistics_saved_location_id = logistics.saved.location (customer profile)
                "logistics_saved_location_id": pu_loc.id if pu_loc else None,
            }]
            delivery_stops = self._build_confirm_delivery_stops(session, address_vals)
        normalized = svc.normalize_request({
            "partner_id": user_partner.id,
            "pickup_stops": pickup_stops,
            "delivery_stops": delivery_stops,
            "load_type": session.shipment_type,
            "equipment_type": session.temperature_mode,
            "required_temperature_c": (
                session.required_temperature_c
                if session.temperature_mode == "reefer"
                else None
            ),
            "pallets": session.physical_pallets or session.pallets,
            "physical_pallets": session.physical_pallets or session.pallets,
            "shared_pallet_mode": session.shared_pallet_mode or False,
            "pallet_allocations": self._extract_pallet_allocs_from_snapshot(session.price_snapshot),
            # Architecture discriminator: only sessions built by the
            # generalized route builder carry canonical movements.
            "route_model_version": "movement_v1" if movements else "legacy",
            "pallet_movements": movements,
            "weight_lbs": session.weight_lbs,
            "liftgate_pickup": session.liftgate_pickup,
            "liftgate_delivery": session.liftgate_delivery,
            "appointment": session.appointment,
            "residential": session.residential,
            "same_day_requested": session.same_day_requested,
            "pricing_method": "corridor",
            "idempotency_key": f"portal:{token}",
        }, source_channel="portal")

        try:
            return svc.confirm_from_internal(normalized, pricing_session=session)
        except UniqueViolation:
            existing = self.sudo().search([("pricing_session_token", "=", token)], limit=1)
            if existing:
                return existing
            raise

    def action_cancel(self, reason="", source="customer"):
        self.ensure_one()
        if self.state == "cancelled":
            return True
        if self.state == "completed":
            raise UserError(_("Completed bookings cannot be cancelled."))
        if source == "customer":
            executed = self.dispatch_job_ids.mapped("stage_id").filtered(
                lambda s: s.stage_type in ("dispatched", "completed"))
            if executed:
                raise UserError(_("Shipment is already in progress. Please contact us to cancel."))
        self.write({
            "state": "cancelled", "cancelled_at": fields.Datetime.now(),
            "cancelled_by": self.env.user.id,
            "cancellation_source": source, "cancellation_reason": reason or "",
        })
        cancel_s = self.env["prema.dispatch.stage"].sudo().search(
            [("stage_type", "=", "cancelled")], limit=1)
        for job in self.dispatch_job_ids.filtered(
            lambda j: j.stage_id.stage_type not in ("dispatched", "completed", "cancelled")):
            if cancel_s:
                job.write({"stage_id": cancel_s.id})
        if self.invoice_id and self.invoice_id.state == "draft":
            self.invoice_id.button_cancel()
        for leg in self.leg_ids:
            if leg.reservation_state in ("reserved", "pending"):
                leg.write({"reservation_state": "released"})
        return True

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

            # Estimate from the same corridor topology used by booking.
            distance_km = 200.0
            if pickup_fsa and delivery_fsa:
                from ..services.route_resolver import RouteResolver
                route = RouteResolver(self.env).resolve_regions(
                    pickup_fsa.region_id, delivery_fsa.region_id,
                )
                if route.available:
                    distance_km = sum(leg["distance_km"] for leg in route.legs) or distance_km

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
                ("active", "=", True), ("x_operational_logistics", "=", True),
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

    def _dispatch_datetime(self, date_value, hour_value=8.0):
        """Convert a corridor-local date/float hour to Odoo's naive UTC."""
        self.ensure_one()
        date_value = fields.Date.to_date(date_value)
        hours = int(hour_value or 0.0)
        minutes = int(round(((hour_value or 0.0) - hours) * 60))
        local_naive = datetime.datetime.combine(date_value, datetime.time.min) + datetime.timedelta(
            hours=hours, minutes=minutes,
        )
        tz_name = self.env.company.partner_id.tz or self.env.user.tz or "America/Toronto"
        timezone = pytz.timezone(tz_name)
        return timezone.localize(local_naive).astimezone(pytz.UTC).replace(tzinfo=None)

    @staticmethod
    def _booking_stop_address(stop):
        return stop.formatted_address or ", ".join(
            value for value in (
                stop.street, stop.city, stop.province_state, stop.postal_zip,
            ) if value
        )

    def _operation_time(self, leg, booking_stop, operation_date, pickup=True):
        departure = leg.departure_id
        base_hour = departure.departure_time if departure else 8.0
        region = leg.origin_region_id if pickup else leg.destination_region_id
        if departure and region:
            corridor_stops = departure.corridor_id.stop_ids.filtered(
                lambda stop: stop.active and stop.region_id == region
            ).sorted("sequence")
            if corridor_stops:
                configured = (
                    corridor_stops[0].planned_departure_time if pickup
                    else corridor_stops[0].planned_arrival_time
                )
                if configured:
                    base_hour = configured
        return self._dispatch_datetime(operation_date, base_hour)

    def pallet_movements(self):
        """Canonical movement dicts for the itinerary planner — one per
        physical pallet with stable stop keys (never array indices)."""
        movements = []
        for pallet in self.env["logistics.booking.pallet"].search(
                [("booking_id", "=", self.id)]):
            movements.append({
                "key": "p%d" % pallet.id,
                "pallet_id": pallet.id,
                "label": pallet.label or ("P-%d" % pallet.sequence),
                "weight_lbs": pallet.weight_lbs or 0.0,
                "shared": pallet.shared,
                "pickup_stop_key": "s%d" % pallet.pickup_stop_id.id,
                "delivery_stop_keys": [
                    "s%d" % alloc.delivery_stop_id.id
                    for alloc in pallet.delivery_allocation_ids.filtered("active")
                ],
            })
        return movements

    def _create_dispatch_route_from_movements(self):
        """Milk-run bridge: one dispatch route job with ordered operational
        stops and canonical items derived from booking.stop_ids and booking
        pallet movements. Returns the created job or False when the legacy
        path should be used instead."""
        self.ensure_one()
        stops = self.stop_ids.sorted("sequence")
        pallets = self.env["logistics.booking.pallet"].search(
            [("booking_id", "=", self.id)])
        if not stops or not pallets:
            return False
        Job = self.env["prema.dispatch.job"].sudo()
        Stop = self.env["prema.dispatch.stop"].sudo()
        Item = self.env["prema.dispatch.item"].sudo()
        from ..services.itinerary_planner import ItineraryPlanner
        simulation = ItineraryPlanner(self.env).simulate_movements(
            ["s%d" % s.id for s in stops], self.pallet_movements())
        deltas = {d["stop_key"]: d for d in simulation["deltas"]}
        operation_date = self.pickup_date or fields.Date.context_today(self)
        job = Job.create({
            "name": "Milk Run — %s" % self.booking_number,
            "partner_id": self.partner_id.id,
            "ref": self.booking_number,
            "source_model": "logistics.booking",
            "source_res_id": self.id,
            "logistics_booking_id": self.id,
            "operation_date": operation_date,
            "operation_role": "combined",
            "tracking_number": "%s-01" % self.booking_number,
            "company_id": self.env.company.id,
            "service_type": "ltl" if self.shipment_type == "ltl" else "ftl",
            "equipment_type": self.temperature_mode,
            "requires_reefer": self.temperature_mode == "reefer",
            "requires_liftgate": self.liftgate_pickup or self.liftgate_delivery,
            "approximate_skids": self.physical_pallets or self.pallets,
            "planned_delivery_date": fields.Date.to_date(operation_date),
            "pickup_window_type": "flexible",
            "delivery_window_type": "flexible",
            "route_definition_mode": "exact_stops",
            "stops_confirmation_state": "confirmed",
            "planned_route_name": self.booking_number,
        })
        stop_records = {}
        for index, stop in enumerate(stops):
            delta = deltas.get("s%d" % stop.id, {})
            stop_records[stop.id] = Stop.create({
                "job_id": job.id,
                "sequence": index * 10,
                "stop_type": "pickup" if stop.stop_type == "pickup" else "dropoff",
                "saved_location_id": stop.saved_location_id.id
                if stop.saved_location_id else False,
                "logistics_booking_stop_id": stop.id,
                "pallets_in": delta.get("pickup", 0),
                "pallets_out": delta.get("delivery", 0),
                "weight_in_lbs": delta.get("weight_in") or 0.0,
                "weight_out_lbs": delta.get("weight_out") or 0.0,
                "pod_required": stop.stop_type == "delivery",
                "pop_required": stop.stop_type == "pickup",
                "tz_name": stop.timezone or "America/Toronto",
                # Stop-level operational requirements come from the booking
                # stop, not from booking-level legacy flags.
                "requires_liftgate": stop.liftgate_required,
                "appointment_required": stop.appointment_required,
                "exact_time": stop.timing_type == "exact_appointment",
                "service_time_minutes": stop.service_time_minutes or 15,
                "dispatcher_notes": stop.instructions or "",
            })
        for pallet in pallets:
            deliveries = pallet.delivery_allocation_ids.filtered("active")
            pickup_dispatch_stop = stop_records.get(
                pallet.pickup_stop_id.id) if pallet.pickup_stop_id else False
            first_delivery = deliveries[:1].delivery_stop_id
            item = Item.create({
                "job_id": job.id,
                "logistics_booking_pallet_id": pallet.id,
                "load_unit_type": "shared_pallet" if pallet.shared else "pallet",
                "pickup_stop_id": pickup_dispatch_stop.id if pickup_dispatch_stop else False,
                "delivery_stop_id": stop_records[first_delivery.id].id
                if first_delivery and first_delivery.id in stop_records else False,
                "weight_lbs": pallet.weight_lbs or 0.0,
                "shared_skid": pallet.shared,
            })
            item.stop_allocation_ids = [(0, 0, {
                "stop_id": stop_records[alloc.delivery_stop_id.id].id,
                "unload_sequence": alloc.unload_sequence or 10,
            }) for alloc in deliveries if alloc.delivery_stop_id.id in stop_records]
        return job

    def _create_dispatch_operation(self, leg, role, operation_date, origin_stop=None,
                                   destination_stop=None, sequence=1):
        self.ensure_one()
        Job = self.env["prema.dispatch.job"].sudo()
        Stop = self.env["prema.dispatch.stop"].sudo()
        Item = self.env["prema.dispatch.item"].sudo()
        departure = leg.departure_id if leg else self.env["logistics.corridor.departure"]
        key_suffix = f"leg:{leg.id}:{role}" if leg else "custom"
        operation_key = f"booking:{self.id}:{key_suffix}"
        existing = Job.search([("ltl_operation_key", "=", operation_key)], limit=1)
        if existing:
            return existing

        anchor_stop = origin_stop or destination_stop
        scheduled_at = self._operation_time(
            leg, anchor_stop, operation_date, pickup=bool(origin_stop),
        ) if leg else self._dispatch_datetime(operation_date, 8.0)
        delivery_at = scheduled_at
        if destination_stop and leg:
            delivery_at = self._operation_time(
                leg, destination_stop, leg.delivery_date or operation_date, pickup=False,
            )
            if delivery_at <= scheduled_at and fields.Date.to_date(leg.delivery_date or operation_date) == fields.Date.to_date(operation_date):
                delivery_at = scheduled_at + datetime.timedelta(hours=4)

        assigned_stage = self.env.ref("prema_dispatch.stage_assigned", raise_if_not_found=False)
        draft_stage = self.env["prema.dispatch.stage"].sudo().search(
            [("stage_type", "=", "draft")], limit=1,
        )
        vehicle = departure.vehicle_id if departure else self.env["fleet.vehicle"]
        corridor = departure.corridor_id if departure else self.env["logistics.corridor"]
        job = Job.create({
            "partner_id": self.partner_id.id,
            "source_model": "logistics.booking",
            "source_res_id": self.id,
            "logistics_booking_id": self.id,
            "booking_leg_id": leg.id if leg else False,
            "corridor_departure_id": departure.id if departure else False,
            "ltl_operation_key": operation_key,
            "operation_date": operation_date,
            "operation_role": role,
            "auto_scheduled_ltl": bool(departure),
            "tracking_number": f"{self.booking_number}-{sequence:02d}",
            "stage_id": (
                assigned_stage.id if vehicle and assigned_stage else draft_stage.id if draft_stage else False
            ),
            "company_id": self.env.company.id,
            "invoice_id": self.invoice_id.id if self.invoice_id else False,
            "vehicle_id": vehicle.id if vehicle else False,
            "assignment_locked": bool(vehicle),
            "scheduled_pickup": scheduled_at,
            "planned_delivery_date": fields.Date.to_date(operation_date),
            "requested_delivery_date": self.estimated_delivery_date or fields.Date.to_date(operation_date),
            "pickup_window_type": "flexible",
            "pickup_exact_time": False,
            "delivery_window_type": "flexible",
            "delivery_exact_time": False,
            "service_type": "ltl" if self.shipment_type == "ltl" else "ftl",
            "equipment_type": self.temperature_mode,
            "requires_reefer": self.temperature_mode == "reefer",
            "requires_liftgate": self.liftgate_pickup or self.liftgate_delivery,
            "temp_requirement": self.required_temperature or (
                f"{self.required_temperature_c:g} °C" if self.temperature_mode == "reefer" else ""
            ),
            "approximate_skids": self.pallets,
            "commodity": self.commodity or "",
            "po_number": self.po_number or "",
            "ref": self.customer_reference or self.booking_number,
            "route_definition_mode": "exact_stops",
            "stops_confirmation_state": "confirmed",
            "planned_route_name": corridor.name if corridor else "Custom / Expedited",
        })

        created_origin = created_destination = False
        if origin_stop:
            created_origin = Stop.create({
                "job_id": job.id,
                "stop_type": "pickup",
                "sequence": 10,
                "partner_id": self.partner_id.id,
                "saved_location_id": origin_stop.saved_location_id.id or False,
                "address": self._booking_stop_address(origin_stop),
                "contact_name": origin_stop.contact_name or "",
                "contact_phone": origin_stop.phone or "",
                "latitude": origin_stop.latitude,
                "longitude": origin_stop.longitude,
                "scheduled_time": scheduled_at,
                "time_window_type": "flexible",
                "exact_time": False,
                "pallets_in": self.pallets,
                "weight_in_lbs": self.weight_lbs,
                "dispatcher_notes": origin_stop.instructions or "",
            })
        if destination_stop:
            created_destination = Stop.create({
                "job_id": job.id,
                "stop_type": "dropoff",
                "sequence": 20,
                "partner_id": self.partner_id.id,
                "saved_location_id": destination_stop.saved_location_id.id or False,
                "address": self._booking_stop_address(destination_stop),
                "contact_name": destination_stop.contact_name or "",
                "contact_phone": destination_stop.phone or "",
                "latitude": destination_stop.latitude,
                "longitude": destination_stop.longitude,
                "scheduled_time": delivery_at,
                "time_window_type": "flexible",
                "exact_time": False,
                "pallets_out": self.pallets,
                "weight_out_lbs": self.weight_lbs,
                "dispatcher_notes": destination_stop.instructions or "",
            })
        # ── Create dispatch stops for each delivery (prema.dispatch.stop, NOT logistics.booking.stop) ──
        BStop = self.env["logistics.booking.stop"].sudo()
        booking_delivery_stops = BStop.search([
            ("booking_id", "=", self.id), ("stop_type", "=", "delivery"),
        ], order="sequence")

        # Filter out hub transfer / internal booking stops (pallet_count=0, no saved_location)
        customer_delivery_stops = booking_delivery_stops.filtered(
            lambda s: s.pallet_count > 0 or s.saved_location_id
        )
        dispatch_delivery_stops = self.env["prema.dispatch.stop"]
        for idx, bstop in enumerate(customer_delivery_stops):
            if created_destination and idx == 0:
                # First delivery: update the already-created destination stop
                created_destination.write({
                    "saved_location_id": bstop.saved_location_id.id if bstop.saved_location_id else False,
                    "address": bstop.formatted_address or bstop.street or "",
                    "contact_name": bstop.contact_name or "",
                    "contact_phone": bstop.phone or "",
                    "latitude": bstop.latitude,
                    "longitude": bstop.longitude,
                    "pallets_out": bstop.pallet_count,
                    "weight_out_lbs": bstop.weight_lb,
                    "dispatcher_notes": bstop.instructions or "",
                })
                dispatch_delivery_stops |= created_destination
            else:
                extra = Stop.create({
                    "job_id": job.id,
                    "stop_type": "dropoff",
                    "sequence": 20 + idx * 10,
                    "partner_id": self.partner_id.id,
                    "saved_location_id": bstop.saved_location_id.id if bstop.saved_location_id else False,
                    "address": bstop.formatted_address or bstop.street or "",
                    "contact_name": bstop.contact_name or "",
                    "contact_phone": bstop.phone or "",
                    "latitude": bstop.latitude,
                    "longitude": bstop.longitude,
                    "scheduled_time": delivery_at,
                    "time_window_type": "flexible",
                    "exact_time": False,
                    "pallets_out": bstop.pallet_count,
                    "weight_out_lbs": bstop.weight_lb,
                    "dispatcher_notes": bstop.instructions or "",
                })
                dispatch_delivery_stops |= extra

        # ── Create dispatch items: per-pallet allocations (canonical) or legacy shared/dedicated ──
        Alloc = self.env["prema.dispatch.pallet.stop.allocation"].sudo()
        physical_count = self.physical_pallets or self.pallets
        pallet_allocs = self._get_pallet_allocations()

        # Build backward-compat allocations from legacy shared_pallet_mode
        if not pallet_allocs and self.shared_pallet_mode and dispatch_delivery_stops:
            # Legacy: all pallets shared across all stops
            stop_indices = list(range(1, len(dispatch_delivery_stops) + 1))
            pallet_allocs = [
                {"pallet": p + 1, "stops": stop_indices,
                 "shared": len(stop_indices) > 1}
                for p in range(physical_count)
            ]

        if pallet_allocs and dispatch_delivery_stops:
            # Canonical per-pallet allocation: one dispatch item per physical pallet
            for pa in pallet_allocs:
                pallet_num = pa.get("pallet", 1)
                stop_indices = pa.get("stops") or []
                is_shared = len(stop_indices) > 1
                label = f"Pallet-{pallet_num}"
                # Map 1-based stop indices to dispatch_delivery_stops (sorted by sequence)
                sorted_stops = dispatch_delivery_stops.sorted("sequence")
                assigned_stops = self.env["prema.dispatch.stop"]
                for si in stop_indices:
                    if 1 <= si <= len(sorted_stops):
                        assigned_stops |= sorted_stops[si - 1]
                item = Item.create({
                    "job_id": job.id,
                    "name": label,
                    "description": self.commodity or "",
                    "pallet_count": 1,
                    "weight_lbs": self.weight_lbs / max(physical_count, 1),
                    "pickup_stop_id": created_origin.id if created_origin else False,
                    "delivery_stop_id": assigned_stops[0].id if assigned_stops else (
                        dispatch_delivery_stops[0].id if dispatch_delivery_stops else False
                    ),
                    "available_after_stop_id": created_origin.id if created_origin else False,
                    "temperature_zone": "chilled" if self.temperature_mode == "reefer" else "ambient",
                    "load_unit_type": "shared_pallet" if is_shared else "pallet",
                    "shared_skid": is_shared,
                })
                for idx, ds in enumerate(assigned_stops):
                    Alloc.create({
                        "dispatch_item_id": item.id,
                        "stop_id": ds.id,
                        "unload_sequence": (idx + 1) * 10,
                    })
        elif dispatch_delivery_stops and len(dispatch_delivery_stops) > 1:
            # Legacy: per-stop pallets (one item per pallet per stop, no sharing)
            pallet_counter = 1
            for dstop in dispatch_delivery_stops:
                stop_pallets = dstop.pallets_out or 1
                for _ in range(stop_pallets):
                    Item.create({
                        "job_id": job.id,
                        "name": f"Pallet-{pallet_counter}",
                        "description": self.commodity or "",
                        "pallet_count": 1,
                        "weight_lbs": self.weight_lbs / max(physical_count, 1),
                        "pickup_stop_id": created_origin.id if created_origin else False,
                        "delivery_stop_id": dstop.id,
                        "available_after_stop_id": created_origin.id if created_origin else False,
                        "temperature_zone": "chilled" if self.temperature_mode == "reefer" else "ambient",
                        "load_unit_type": "pallet",
                    })
                    pallet_counter += 1
        else:
            for line in self.line_ids:
                Item.create({
                    "job_id": job.id,
                    "name": line.description or "Pallet",
                    "description": line.commodity or self.commodity or "",
                    "pallet_count": line.pallets,
                    "weight_lbs": line.weight_lbs,
                    "pickup_stop_id": created_origin.id if created_origin else False,
                    "delivery_stop_id": created_destination.id if created_destination else False,
                    "available_after_stop_id": created_origin.id if created_origin else False,
                    "temperature_zone": "chilled" if self.temperature_mode == "reefer" else "ambient",
                    "load_unit_type": "pallet",
                })
        return job

    def _create_dispatch_job(self):
        """Create one Planner card per physical truck/day operation.

        A leg that picks up and delivers on different dates is intentionally
        split into two cards. Physical freight items are created once and
        referenced by all operational jobs for this booking.
        """
        self.ensure_one()
        existing = self.env["prema.dispatch.job"].sudo().search([
            ("logistics_booking_id", "=", self.id),
        ], order="operation_date, id")
        if existing:
            if self.dispatch_job_id != existing[0]:
                self.dispatch_job_id = existing[0].id
            return existing

        # Milk-run generalization: ONLY bookings explicitly created as
        # movement_v1 use the canonical movement bridge. Legacy bookings
        # keep the legacy leg/snapshot bridge even when compatibility
        # pallet rows exist (migration 18.0.11.0.0 backfills those rows
        # for historical bookings WITHOUT changing their architecture).
        # Pallet rows alone are never a bridge selector.
        if self.route_model_version == "movement_v1":
            route_job = self._create_dispatch_route_from_movements()
            if route_job:
                self.dispatch_job_id = route_job.id
                return route_job

        jobs = self.env["prema.dispatch.job"]
        sequence = 1
        first_job = None
        for leg in self.leg_ids.sorted("sequence"):
            pickup_date = leg.pickup_date or leg.departure_id.departure_date or self.pickup_date
            delivery_date = leg.delivery_date or pickup_date or self.estimated_delivery_date
            if pickup_date and delivery_date and pickup_date != delivery_date:
                job1 = self._create_dispatch_operation(leg, "pickup", pickup_date, origin_stop=leg.origin_stop_id, sequence=sequence)
                jobs |= job1
                if not first_job: first_job = job1
                sequence += 1
                jobs |= self._create_dispatch_operation(leg, "delivery", delivery_date, destination_stop=leg.destination_stop_id, sequence=sequence)
                sequence += 1
            else:
                role = leg.leg_type if leg.leg_type in ("feeder", "linehaul", "final_delivery") else "combined"
                job1 = self._create_dispatch_operation(leg, role, pickup_date or delivery_date, origin_stop=leg.origin_stop_id, destination_stop=leg.destination_stop_id, sequence=sequence)
                jobs |= job1
                if not first_job: first_job = job1
                sequence += 1

        if not jobs:
            BStop = self.env["logistics.booking.stop"].sudo()
            pickups = BStop.search([("booking_id", "=", self.id), ("stop_type", "=", "pickup")], order="sequence")
            deliveries = BStop.search([("booking_id", "=", self.id), ("stop_type", "=", "delivery")], order="sequence")
            operation_date = self.pickup_date or fields.Date.context_today(self)
            job1 = self._create_dispatch_operation(False, "custom", operation_date, origin_stop=pickups[:1], destination_stop=deliveries[-1:], sequence=sequence)
            jobs |= job1
            if not first_job: first_job = job1

        # Canonical pallets: subsequent jobs reference first job's items
        canonical_items = first_job.item_ids.filtered(lambda i: i.status != 'cancelled')
        for job in jobs:
            if job.id == first_job.id or not canonical_items:
                continue
            dupes = job.item_ids.filtered(lambda i: i.status != 'cancelled')
            # Transfer stop allocations from duplicates to canonical items
            for dup_item, canonical in zip(dupes, canonical_items):
                dup_item.stop_allocation_ids.write({'dispatch_item_id': canonical.id})
            dupes.mapped('stop_allocation_ids').write({'active': False})
            dupes.write({'status': 'cancelled'})

        self.dispatch_job_id = jobs[0].id
        return jobs

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

        # Determine country from the real pickup stop. Rate Plans are
        # historical only and must never be required to select a product.
        country = "CA"
        pickup = self.stop_ids.filtered(lambda stop: stop.stop_type == "pickup").sorted("sequence")[:1]
        if pickup and pickup.country_id:
            country = pickup.country_id.code or "CA"

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

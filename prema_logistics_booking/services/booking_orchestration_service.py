"""BookingOrchestrationService — the single canonical entry point for ALL booking channels.

Per Prema AI V4 spec (§3): Every portal, phone, internal, invoice, WhatsApp,
custom quote, recurring, and API booking MUST flow through this service.
Controllers, wizards, and WA code must NEVER directly create:
    - logistics.booking
    - logistics.booking.stop
    - logistics.booking.leg
    - account.move
    - prema.dispatch.job

Usage:
    svc = BookingOrchestrationService(env)
    norm = svc.normalize_request(raw_values, source_channel="phone")
    booking = svc.confirm_from_internal(norm, idempotency_key="...")
"""

import datetime
import json
import logging
import secrets
import uuid

from odoo import _
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

SOURCE_CHANNELS = [
    ("portal", "Customer Portal"),
    ("phone", "Phone Booking"),
    ("internal", "Internal Staff"),
    ("invoice", "Invoice Create/Open Booking"),
    ("custom_quote", "Custom Quote"),
    ("recurring", "Recurring Agreement"),
    ("whatsapp", "WhatsApp Negotiation"),
    ("api", "External API"),
]

BOOKING_STATES = [
    ("draft", "Draft"),
    ("quoted", "Quoted"),
    ("confirmed", "Confirmed"),
    ("planned", "Planned"),
    ("in_execution", "In Execution"),
    ("delivered", "Delivered"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
    ("exception", "Exception"),
]

LEG_STATES = [
    ("planned", "Planned"),
    ("reserved", "Reserved"),
    ("assigned", "Assigned"),
    ("picked_up", "Picked Up"),
    ("in_transit", "In Transit"),
    ("transferred", "Transferred"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
    ("exception", "Exception"),
]

EQUIPMENT_TYPES = [("dry", "Dry"), ("reefer", "Reefer")]

PRICING_METHODS = [
    ("rate_plan", "Rate Plan"),
    ("contract", "Contract"),
    ("negotiated", "Negotiated"),
    ("manual", "Manual"),
    ("imported_invoice", "Imported Invoice"),
]


class NormalizedBookingRequest:
    """Validated, normalized booking request — all channels produce this."""

    def __init__(self, data: dict):
        self.source_channel = data["source_channel"]
        self.source_model = data.get("source_model", "")
        self.source_res_id = data.get("source_res_id", 0)
        self.source_reference = data.get("source_reference", "")
        self.idempotency_key = data.get("idempotency_key", "")

        self.partner_id = data["partner_id"]
        self.commercial_partner_id = data.get("commercial_partner_id", 0)
        self.billing_contact_id = data.get("billing_contact_id", 0)
        self.pickup_contact_id = data.get("pickup_contact_id", 0)
        self.delivery_contact_id = data.get("delivery_contact_id", 0)

        self.equipment_type = data.get("equipment_type", "dry")
        self.required_temperature_c = data.get("required_temperature_c")
        self.required_temperature = data.get("required_temperature", "")

        self.pickup_stops = data.get("pickup_stops", [])
        self.delivery_stops = data.get("delivery_stops", [])
        self.transfer_allowed = data.get("transfer_allowed", True)
        self.requested_pickup_date = data.get("requested_pickup_date")
        self.requested_delivery_date = data.get("requested_delivery_date")

        self.pallets = data.get("pallets", 1)
        self.weight_lbs = data.get("weight_lbs", 0.0)
        self.commodity = data.get("commodity", "")
        self.stackable = data.get("stackable", True)
        self.hazmat = data.get("hazmat", False)
        self.food_grade = data.get("food_grade", False)

        self.po_number = data.get("po_number", "")
        self.customer_reference = data.get("customer_reference", "")
        self.bol_number = data.get("bol_number", "")
        self.instructions = data.get("instructions", "")

        self.pricing_method = data.get("pricing_method", "rate_plan")
        self.agreed_rate = data.get("agreed_rate", 0.0)
        self.currency_id = data.get("currency_id")

        self.existing_invoice_id = data.get("existing_invoice_id")
        self.existing_sale_order_id = data.get("existing_sale_order_id")
        self.wa_negotiation_id = data.get("wa_negotiation_id")
        self.custom_quote_id = data.get("custom_quote_id")
        self.recurring_agreement_id = data.get("recurring_agreement_id")

        self.liftgate_pickup = data.get("liftgate_pickup", False)
        self.liftgate_delivery = data.get("liftgate_delivery", False)
        self.appointment = data.get("appointment", False)
        self.residential = data.get("residential", False)

        self._validate()

    def _validate(self):
        """Raise ValidationError if required fields are missing."""
        if not self.source_channel:
            raise ValidationError(_("source_channel is required"))
        if self.source_channel not in dict(SOURCE_CHANNELS):
            raise ValidationError(_("Invalid source_channel: %s") % self.source_channel)
        if not self.partner_id:
            raise ValidationError(_("partner_id is required"))
        if self.pallets < 1:
            raise ValidationError(_("pallets must be >= 1"))
        if self.weight_lbs < 0:
            raise ValidationError(_("weight_lbs must be >= 0"))


class BookingOrchestrationService:
    """Canonical orchestration service for all booking channels.

    Every entry point (portal, phone, internal, invoice, WhatsApp, custom quote,
    recurring, API) MUST call this service. Direct model creation of
    logistics.booking, booking.stop, booking.leg, account.move, or
    prema.dispatch.job from controllers/wizards/WA code is forbidden.
    """

    def __init__(self, env):
        self.env = env

    # ═══════════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════════

    def normalize_request(self, values: dict, source_channel: str) -> NormalizedBookingRequest:
        """Convert raw input from any channel into a NormalizedBookingRequest.
        All channels MUST call this before pricing, routing, or confirmation."""
        values["source_channel"] = source_channel

        # Auto-generate idempotency key if not provided
        if not values.get("idempotency_key"):
            values["idempotency_key"] = f"{source_channel}:{uuid.uuid4().hex[:16]}"

        # Resolve partner hierarchy
        partner_id = values.get("partner_id")
        if partner_id:
            partner = self.env["res.partner"].browse(partner_id)
            if partner.exists():
                if not values.get("commercial_partner_id"):
                    values["commercial_partner_id"] = partner.commercial_partner_id.id

        return NormalizedBookingRequest(values)

    def resolve_service_options(self, normalized_request: NormalizedBookingRequest) -> list:
        """Resolve available service options (lanes, departures, pricing) for a
        normalized request. Returns list of service option dicts for customer
        or staff to choose from."""
        from ..services.routing_service import RoutingService

        router = RoutingService(self.env)
        options = []

        # Resolve each pickup→delivery pair
        for pu_stop in (normalized_request.pickup_stops or [{"postal": ""}]):
            for del_stop in (normalized_request.delivery_stops or [{"postal": ""}]):
                pu_postal = pu_stop.get("postal_code", pu_stop.get("postal", ""))
                del_postal = del_stop.get("postal_code", del_stop.get("postal", ""))

                if pu_postal and del_postal:
                    result = router.full_resolve(
                        pu_postal, del_postal,
                        pickup_date=normalized_request.requested_pickup_date,
                        pallets=normalized_request.pallets,
                        weight_lbs=normalized_request.weight_lbs,
                        equipment=normalized_request.equipment_type,
                    )
                    for opt in (result.options if hasattr(result, "options") else [result] if result else []):
                        options.append({
                            "route_type": getattr(opt, "route_type", "direct"),
                            "origin_region": getattr(opt, "origin_region", ""),
                            "destination_region": getattr(opt, "destination_region", ""),
                            "pickup_date": str(getattr(opt, "pickup_date", "")),
                            "delivery_date": str(getattr(opt, "delivery_date", "")),
                            "departure_id": getattr(opt, "departure_id", None),
                            "corridor_id": getattr(opt, "corridor_id", None),
                            "lane_id": getattr(opt, "lane_id", None),
                            "rate_plan_id": getattr(opt, "rate_plan_id", None),
                            "service_offering_id": getattr(opt, "service_offering_id", None),
                            "calculated_price": getattr(opt, "calculated_price", 0.0),
                            "transfer_count": getattr(opt, "transfer_count", 0),
                            "customer_route_text": getattr(opt, "customer_route_text", ""),
                        })

        return options

    def prepare_quote(self, normalized_request: NormalizedBookingRequest, session_ttl_minutes: int = 20) -> dict:
        """Create a pricing session / quote for the customer to review.
        Returns a dict with quote_token, price, and expiration."""
        from ..services.pricing_service import PricingService

        pricing = PricingService(self.env)
        pickup_fsa = None
        delivery_fsa = None

        # Resolve FSAs from stops or legacy postal fields
        if normalized_request.pickup_stops:
            postal = normalized_request.pickup_stops[0].get("postal_code", "")
            if postal:
                pickup_fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(postal)
        if normalized_request.delivery_stops:
            postal = normalized_request.delivery_stops[-1].get("postal_code", "")
            if postal:
                delivery_fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(postal)

        if not pickup_fsa or not delivery_fsa:
            raise UserError(_("Could not resolve origin or destination postal code."))

        result = pricing.calculate(
            pickup_fsa, delivery_fsa,
            "ltl",
            "reefer" if normalized_request.equipment_type == "reefer" else "dry",
            normalized_request.pallets,
            normalized_request.weight_lbs,
            normalized_request.liftgate_pickup,
            normalized_request.liftgate_delivery,
            normalized_request.appointment,
            normalized_request.residential,
            partner=self.env["res.partner"].browse(normalized_request.partner_id),
        )

        if not result.available:
            raise UserError(_("No service available: %s") % (result.reason or "unknown"))

        # Create pricing session
        Session = self.env["logistics.pricing.session"].sudo()
        session = Session.create({
            "partner_id": normalized_request.partner_id,
            "pickup_fsa_id": pickup_fsa.id,
            "delivery_fsa_id": delivery_fsa.id,
            "shipment_type": "ltl",
            "temperature_mode": "reefer" if normalized_request.equipment_type == "reefer" else "dry",
            "pallets": normalized_request.pallets,
            "weight_lbs": normalized_request.weight_lbs,
            "service_offering_id": result.service_offering.id,
            "rate_plan_id": result.rate_plan.id,
            "lane_id": result.lane.id,
            "pickup_date": result.pickup_date,
            "delivery_date_estimate": result.delivery_date_estimate,
            "calculated_price": result.calculated_price,
            "price_snapshot": result.price_lines,
            "expires_at": fields.Datetime.now() + datetime.timedelta(minutes=session_ttl_minutes),
        })

        return {
            "quote_token": session.token,
            "pickup_date": str(result.pickup_date),
            "delivery_date": str(result.delivery_date_estimate),
            "calculated_price": result.calculated_price,
            "price_lines": result.price_lines,
            "lane_name": result.lane.name,
            "service_offering_name": result.service_offering.name,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        }

    def confirm_from_internal(
        self,
        normalized_request: NormalizedBookingRequest,
        existing_invoice: object = None,
        skip_invoice: bool = False,
    ):
        """Create a confirmed booking from an internal (staff) source.
        This is the canonical method for phone, internal, invoice, WA,
        custom quote, and recurring channels.

        Returns the confirmed logistics.booking record.
        Raises UserError/AccessError on failure.
        """
        # ── 1. Idempotency check ──────────────────────────────────────────
        existing = self._find_existing_booking(normalized_request)
        if existing:
            _logger.info(
                "BookingOrchestrationService: idempotent return of booking %s "
                "for channel=%s key=%s",
                existing.booking_number,
                normalized_request.source_channel,
                normalized_request.idempotency_key,
            )
            return existing

        # ── 2. Validate partner ───────────────────────────────────────────
        partner = self.env["res.partner"].browse(normalized_request.partner_id)
        if not partner.exists():
            raise UserError(_("Customer not found."))

        # ── 3. Resolve FSAs ───────────────────────────────────────────────
        Fsa = self.env["logistics.fsa"].sudo()
        pickup_fsa = None
        delivery_fsa = None

        if normalized_request.pickup_stops:
            postal = normalized_request.pickup_stops[0].get("postal_code", "")
            if postal:
                pickup_fsa = Fsa.resolve_from_postal(postal)
        if normalized_request.delivery_stops:
            postal = normalized_request.delivery_stops[-1].get("postal_code", "")
            if postal:
                delivery_fsa = Fsa.resolve_from_postal(postal)

        # ── 4. Resolve pricing ────────────────────────────────────────────
        from ..services.pricing_service import PricingService

        pricing = PricingService(self.env)
        rate_plan = None
        service_offering = None
        pickup_date = normalized_request.requested_pickup_date
        delivery_date = normalized_request.requested_delivery_date
        calculated_price = normalized_request.agreed_rate

        if normalized_request.pricing_method in ("rate_plan", "contract") and pickup_fsa and delivery_fsa:
            result = pricing.calculate(
                pickup_fsa, delivery_fsa,
                "ltl",
                "reefer" if normalized_request.equipment_type == "reefer" else "dry",
                normalized_request.pallets,
                normalized_request.weight_lbs,
                normalized_request.liftgate_pickup,
                normalized_request.liftgate_delivery,
                normalized_request.appointment,
                normalized_request.residential,
                partner=partner,
            )
            if result.available:
                rate_plan = result.rate_plan
                service_offering = result.service_offering
                pickup_date = result.pickup_date
                delivery_date = result.delivery_date_estimate
                if not calculated_price:
                    calculated_price = result.calculated_price

        # Fallback: use agreed_rate if no rate plan matched
        if not calculated_price and normalized_request.agreed_rate:
            calculated_price = normalized_request.agreed_rate

        # ── 5. Build booking vals ─────────────────────────────────────────
        booking_vals = {
            "partner_id": partner.id,
            "booking_channel": normalized_request.source_channel,
            "source_channel": normalized_request.source_channel,
            "source_model": normalized_request.source_model or "",
            "source_res_id": normalized_request.source_res_id or 0,
            "source_reference": normalized_request.source_reference or "",
            "idempotency_key": normalized_request.idempotency_key,
            "shipment_type": "ltl",
            "temperature_mode": "reefer" if normalized_request.equipment_type == "reefer" else "dry",
            "required_temperature": normalized_request.required_temperature or "",
            "required_temperature_c": normalized_request.required_temperature_c or 0.0,
            "pallets": normalized_request.pallets,
            "weight_lbs": normalized_request.weight_lbs,
            "commodity": normalized_request.commodity or "",
            "calculated_price": calculated_price,
            "po_number": normalized_request.po_number or "",
            "customer_reference": normalized_request.customer_reference or "",
            "booking_number": self.env["logistics.booking"]._generate_booking_number(),
            "state": "confirmed",
            "confirmed_at": fields.Datetime.now(),
            "liftgate_pickup": normalized_request.liftgate_pickup,
            "liftgate_delivery": normalized_request.liftgate_delivery,
            "appointment": normalized_request.appointment,
            "residential": normalized_request.residential,
        }

        # Cross-reference fields
        if normalized_request.wa_negotiation_id:
            booking_vals["wa_negotiation_id"] = normalized_request.wa_negotiation_id
        if normalized_request.recurring_agreement_id:
            booking_vals["recurring_agreement_id"] = normalized_request.recurring_agreement_id

        if pickup_fsa:
            booking_vals["pickup_fsa_id"] = pickup_fsa.id
            booking_vals["pickup_address"] = pickup_fsa.display_city or pickup_fsa.fsa
        if delivery_fsa:
            booking_vals["delivery_fsa_id"] = delivery_fsa.id
            booking_vals["delivery_address"] = delivery_fsa.display_city or delivery_fsa.fsa
        if rate_plan:
            booking_vals["rate_plan_id"] = rate_plan.id
        if service_offering:
            booking_vals["service_offering_id"] = service_offering.id
        if pickup_date:
            booking_vals["pickup_date"] = pickup_date
        if delivery_date:
            booking_vals["estimated_delivery_date"] = delivery_date

        # Address from stops
        if normalized_request.pickup_stops:
            pu = normalized_request.pickup_stops[0]
            booking_vals.update({
                "pickup_company": pu.get("company_name", ""),
                "pickup_address": pu.get("formatted_address", pu.get("address", booking_vals.get("pickup_address", ""))),
                "pickup_contact_name": pu.get("contact_name", ""),
                "pickup_phone": pu.get("phone", ""),
                "pickup_instructions": pu.get("instructions", ""),
            })
        if normalized_request.delivery_stops:
            dl = normalized_request.delivery_stops[-1]
            booking_vals.update({
                "delivery_company": dl.get("company_name", ""),
                "delivery_address": dl.get("formatted_address", dl.get("address", booking_vals.get("delivery_address", ""))),
                "delivery_contact_name": dl.get("contact_name", ""),
                "delivery_phone": dl.get("phone", ""),
                "delivery_instructions": dl.get("instructions", ""),
            })

        # ── 6. Cost estimate ──────────────────────────────────────────────
        Booking = self.env["logistics.booking"]
        cost_info = Booking._estimate_cost_from_request(normalized_request, pickup_fsa, delivery_fsa)
        booking_vals["cost_snapshot"] = cost_info.get("breakdown", {})
        booking_vals["estimated_cost"] = cost_info.get("total_cost", 0.0)

        # ── 7. Atomic transaction: booking + stops + lines + legs + dispatch + tax + invoice ──
        try:
            with self.env.cr.savepoint():
                booking = Booking.sudo().create(booking_vals)

                # Create booking stops
                self._create_booking_stops(booking, normalized_request)

                # Create booking lines
                self._create_booking_lines(booking, normalized_request)

                # Create dispatch job
                booking._create_dispatch_job()

                # Apply tax decision
                booking._apply_tax_decision()

                # Create or link invoice
                if not skip_invoice:
                    if existing_invoice:
                        self._link_existing_invoice(booking, existing_invoice)
                    else:
                        booking._create_draft_invoice()

                # Create booking legs WITH transactional capacity reservation
                self._reserve_capacity_transactionally(booking, normalized_request)

        except Exception:
            _logger.exception(
                "BookingOrchestrationService: confirmation failed for channel=%s key=%s",
                normalized_request.source_channel,
                normalized_request.idempotency_key,
            )
            raise

        _logger.info(
            "BookingOrchestrationService: created booking %s (ID:%s) via channel=%s",
            booking.booking_number, booking.id, normalized_request.source_channel,
        )
        return booking

    def cancel_booking(self, booking, reason: str = ""):
        """Cancel a booking and release all associated resources."""
        booking.ensure_one()
        if booking.state == "cancelled":
            return booking

        booking.write({"state": "cancelled"})

        # Release capacity reservations on all legs
        for leg in booking.leg_ids:
            if leg.reservation_state in ("reserved", "pending"):
                leg.write({"reservation_state": "released"})

        # Cancel draft invoice if exists
        if booking.invoice_id and booking.invoice_id.state == "draft":
            booking.invoice_id.button_cancel()

        # Cancel dispatch job if exists and not completed
        if booking.dispatch_job_id:
            cancel_stage = self.env["prema.dispatch.stage"].sudo().search(
                [("stage_type", "=", "cancelled")], limit=1
            )
            if cancel_stage:
                booking.dispatch_job_id.write({"stage_id": cancel_stage.id})

        _logger.info(
            "BookingOrchestrationService: cancelled booking %s (reason: %s)",
            booking.booking_number, reason or "not specified",
        )
        return booking

    # ═══════════════════════════════════════════════════════════════════════
    # Private helpers
    # ═══════════════════════════════════════════════════════════════════════

    def _find_existing_booking(self, normalized_request: NormalizedBookingRequest):
        """Check for an existing booking with the same idempotency key."""
        Booking = self.env["logistics.booking"].sudo()
        key = normalized_request.idempotency_key
        channel = normalized_request.source_channel

        if not key:
            return None

        # Search by idempotency_key + source_channel
        existing = Booking.search([
            ("source_channel", "=", channel),
            ("idempotency_key", "=", key),
        ], limit=1)
        if existing:
            return existing

        # Fallback: check cross-reference fields
        if normalized_request.wa_negotiation_id:
            existing = Booking.search([
                ("wa_negotiation_id", "=", normalized_request.wa_negotiation_id),
            ], limit=1)
            if existing:
                return existing

        if normalized_request.existing_invoice_id:
            existing = Booking.search([
                ("invoice_id", "=", normalized_request.existing_invoice_id),
            ], limit=1)
            if existing:
                return existing

        return None

    def _create_booking_stops(self, booking, normalized_request: NormalizedBookingRequest):
        """Create booking stops from normalized request."""
        Stop = self.env["logistics.booking.stop"].sudo()
        seq = 10

        for pu in normalized_request.pickup_stops:
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "pickup",
                "company_name": pu.get("company_name", ""),
                "street": pu.get("street", ""),
                "city": pu.get("city", ""),
                "province_state": pu.get("province_state", pu.get("province", "")),
                "postal_zip": pu.get("postal_code", ""),
                "formatted_address": pu.get("formatted_address", ""),
                "latitude": pu.get("latitude", 0.0),
                "longitude": pu.get("longitude", 0.0),
                "contact_name": pu.get("contact_name", ""),
                "phone": pu.get("phone", ""),
                "pallet_count": pu.get("pallet_count", normalized_request.pallets),
                "weight_lb": pu.get("weight_lb", pu.get("weight_lbs", normalized_request.weight_lbs)),
                "liftgate_required": pu.get("liftgate_required", normalized_request.liftgate_pickup),
                "instructions": pu.get("instructions", ""),
                "reference": pu.get("reference", ""),
            })
            seq += 10

        for dl in normalized_request.delivery_stops:
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "delivery",
                "company_name": dl.get("company_name", ""),
                "street": dl.get("street", ""),
                "city": dl.get("city", ""),
                "province_state": dl.get("province_state", dl.get("province", "")),
                "postal_zip": dl.get("postal_code", ""),
                "formatted_address": dl.get("formatted_address", ""),
                "latitude": dl.get("latitude", 0.0),
                "longitude": dl.get("longitude", 0.0),
                "contact_name": dl.get("contact_name", ""),
                "phone": dl.get("phone", ""),
                "pallet_count": dl.get("pallet_count", 0),
                "weight_lb": dl.get("weight_lb", dl.get("weight_lbs", 0)),
                "liftgate_required": dl.get("liftgate_required", normalized_request.liftgate_delivery),
                "instructions": dl.get("instructions", ""),
                "reference": dl.get("reference", ""),
            })
            seq += 10

    def _create_booking_lines(self, booking, normalized_request: NormalizedBookingRequest):
        """Create booking lines from normalized request."""
        Line = self.env["logistics.booking.line"].sudo()
        Line.create({
            "booking_id": booking.id,
            "sequence": 10,
            "description": normalized_request.commodity or "LTL Shipment",
            "pallets": normalized_request.pallets,
            "weight_lbs": normalized_request.weight_lbs,
            "commodity": normalized_request.commodity or "",
        })

    def _reserve_capacity_transactionally(self, booking, normalized_request: NormalizedBookingRequest):
        """Reserve capacity on all required corridor departures atomically.

        Uses PostgreSQL row-level locking (SELECT ... FOR UPDATE) to prevent
        concurrent oversells. Validates per-segment pallet and weight limits.

        Returns list of created booking legs with reservation_state='reserved'.
        Raises UserError if any leg exceeds capacity.
        """
        Leg = self.env["logistics.booking.leg"].sudo()
        Departure = self.env["logistics.corridor.departure"]

        # ── 1. Resolve required departures ──────────────────────────────
        pickups = booking.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")
        deliveries = booking.stop_ids.filtered(lambda s: s.stop_type == "delivery").sorted("sequence")

        if not pickups or not deliveries:
            _logger.warning("Booking %s: no stops, skipping capacity reservation", booking.booking_number)
            return []

        departure_id = booking.departure_id.id if booking.departure_id else None
        if not departure_id:
            # Try to find a suitable departure
            if booking.pickup_fsa_id and booking.delivery_fsa_id:
                departures = Departure.sudo().search([
                    ("departure_date", "=", booking.pickup_date),
                    ("status", "=", "scheduled"),
                    ("active", "=", True),
                ], limit=1)
                if departures:
                    departure_id = departures[0].id
                    booking.write({"departure_id": departure_id})

        if not departure_id:
            _logger.warning(
                "Booking %s: no corridor departure found for date %s, creating legs without capacity reservation",
                booking.booking_number, booking.pickup_date,
            )
            return self._create_booking_legs_simple(booking, normalized_request)

        # Collect all departures needed (for transfers, multiple departures)
        departure_ids = [departure_id]
        # Also check for transfer departures (next-day connecting)
        if booking.stop_ids and len(pickups) >= 1 and len(deliveries) >= 1:
            # Check if pickup and delivery are on different corridors
            pu_region = booking.pickup_fsa_id.region_id
            del_region = booking.delivery_fsa_id.region_id
            first_dep = Departure.sudo().browse(departure_id)
            if first_dep.exists() and first_dep.corridor_id:
                cor_stops = first_dep.corridor_id.stop_ids
                pu_on_cor = any(s.region_id == pu_region for s in cor_stops) if pu_region else False
                del_on_cor = any(s.region_id == del_region for s in cor_stops) if del_region else False
                if pu_on_cor and not del_on_cor and normalized_request.transfer_allowed:
                    # Need a transfer — find next day's connecting departure
                    next_date = booking.pickup_date + datetime.timedelta(days=1)
                    next_deps = Departure.sudo().search([
                        ("departure_date", "=", next_date),
                        ("status", "=", "scheduled"),
                        ("active", "=", True),
                    ], limit=1)
                    if next_deps:
                        departure_ids.append(next_deps[0].id)

        # ── 2. Sort and lock departures ──────────────────────────────────
        departure_ids = sorted(set(departure_ids))
        locked_deps = []
        for did in departure_ids:
            # PostgreSQL SELECT ... FOR UPDATE row-level lock
            self.env.cr.execute(
                "SELECT id FROM logistics_corridor_departure WHERE id = %s FOR UPDATE",
                [did],
            )
            dep = Departure.sudo().browse(did)
            if not dep.exists():
                raise UserError(_("Corridor departure no longer available."))
            locked_deps.append(dep)

        # ── 3. Recompute active reserved capacity ───────────────────────
        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)

        # Collect ALL currently reserved legs on these departures
        for dep in locked_deps:
            peak = engine.compute_departure_peak(dep)
            current_peak_pallets = peak.get("peak_pallets", 0)
            current_peak_weight = peak.get("peak_weight", 0.0)

            # ── 4. Validate proposed booking against capacity ────────────
            truck_capacity = dep.max_capacity or 12
            max_payload = 11000.0  # Default; use vehicle spec if available
            if dep.vehicle_id:
                max_payload = dep.vehicle_id.x_max_payload_lbs or 11000.0

            new_pallets = booking.pallets
            new_weight = booking.weight_lbs
            estimated_peak = current_peak_pallets + new_pallets
            estimated_weight = current_peak_weight + new_weight

            # Payload check
            if estimated_weight > max_payload:
                raise UserError(_(
                    "Weight capacity exceeded on %(dep)s. "
                    "Current: %(cur).0f lb + New: %(new).0f lb > Max: %(max).0f lb",
                    dep=dep.name, cur=current_peak_weight, new=new_weight, max=max_payload,
                ))

            # Pallet check
            if estimated_peak <= truck_capacity:
                pass  # Accepted normally
            elif estimated_peak == truck_capacity + 1:
                # 13 pallets requires dispatcher override
                if not getattr(normalized_request, 'capacity_override', False) and \
                   not getattr(booking, 'capacity_override', False):
                    raise UserError(_(
                        "13 pallets on %(dep)s requires dispatcher override (pinwheel mode). "
                        "Current peak: %(cur)d + New: %(new)d = 13. "
                        "An authorized dispatcher must enable the capacity override.",
                        dep=dep.name, cur=current_peak_pallets, new=new_pallets,
                    ))
            else:
                raise UserError(_(
                    "Capacity exceeded on %(dep)s. "
                    "Current peak: %(cur)d + New: %(new)d = %(total)d > Max: %(max)d. "
                    "Please select another departure or reduce pallets.",
                    dep=dep.name, cur=current_peak_pallets, new=new_pallets,
                    total=estimated_peak, max=truck_capacity,
                ))

        # ── 5. Create legs with reserved state ───────────────────────────
        created_legs = []
        if len(departure_ids) == 1:
            # Direct: one leg
            leg = Leg.create({
                "booking_id": booking.id,
                "sequence": 10,
                "leg_type": "direct",
                "origin_stop_id": pickups[0].id,
                "destination_stop_id": deliveries[-1].id,
                "departure_id": departure_ids[0],
                "pickup_date": booking.pickup_date,
                "delivery_date": booking.estimated_delivery_date,
                "pallets": booking.pallets,
                "weight_lbs": booking.weight_lbs,
                "status": "scheduled",
                "reservation_state": "reserved",
                "customer_visible": True,
            })
            created_legs.append(leg)
        else:
            # Multi-leg: first leg feeder, last leg linehaul/final
            seq = 10
            for i, did in enumerate(departure_ids):
                dep = Departure.sudo().browse(did)
                is_first = (i == 0)
                is_last = (i == len(departure_ids) - 1)

                if is_first:
                    leg_type = "feeder"
                    origin = pickups[0]
                    dest = deliveries[-1] if is_last else pickups[0]
                elif is_last:
                    leg_type = "linehaul"
                    origin = pickups[0] if len(departure_ids) == 2 else pickups[0]
                    dest = deliveries[-1]
                else:
                    leg_type = "transfer"
                    origin = pickups[0]
                    dest = deliveries[-1]

                leg = Leg.create({
                    "booking_id": booking.id,
                    "sequence": seq,
                    "leg_type": leg_type,
                    "origin_stop_id": origin.id,
                    "destination_stop_id": dest.id,
                    "departure_id": did,
                    "pickup_date": dep.departure_date,
                    "delivery_date": dep.departure_date + datetime.timedelta(days=1) if not is_last else booking.estimated_delivery_date,
                    "pallets": booking.pallets,
                    "weight_lbs": booking.weight_lbs,
                    "status": "scheduled",
                    "reservation_state": "reserved",
                    "customer_visible": True,
                })
                created_legs.append(leg)
                seq += 10

        if len(created_legs) > 1:
            booking.write({"is_multi_leg": True})

        # ── 6. Recompute peak on each departure ──────────────────────────
        for dep in locked_deps:
            peak = engine.compute_departure_peak(dep)
            dep.write({
                "peak_pallets": peak.get("peak_pallets", 0),
                "total_handled_pallets": peak.get("total_handled", 0),
            })

        _logger.info(
            "BookingOrchestrationService: reserved capacity for booking %s on %d departure(s)",
            booking.booking_number, len(locked_deps),
        )
        return created_legs

    def _create_booking_legs_simple(self, booking, normalized_request: NormalizedBookingRequest):
        """Fallback: create legs without capacity reservation (for bookings
        without a matched corridor departure)."""
        if not booking.stop_ids:
            return []

        Leg = self.env["logistics.booking.leg"].sudo()
        pickups = booking.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")
        deliveries = booking.stop_ids.filtered(lambda s: s.stop_type == "delivery").sorted("sequence")

        if not pickups or not deliveries:
            return []

        leg = Leg.create({
            "booking_id": booking.id,
            "sequence": 10,
            "leg_type": "direct",
            "origin_stop_id": pickups[0].id,
            "destination_stop_id": deliveries[-1].id,
            "departure_id": booking.departure_id.id if booking.departure_id else False,
            "pickup_date": booking.pickup_date,
            "delivery_date": booking.estimated_delivery_date,
            "pallets": booking.pallets,
            "weight_lbs": booking.weight_lbs,
            "status": "scheduled",
            "reservation_state": "pending",
            "customer_visible": True,
        })
        return [leg]

    def _link_existing_invoice(self, booking, invoice):
        """Link an existing invoice to this booking and validate tax alignment."""
        booking.write({"invoice_id": invoice.id, "invoice_created_at": fields.Datetime.now()})
        invoice.sudo().write({"logistics_booking_id": booking.id})

        # Tax alignment check
        if booking.tax_rule_id and invoice.state == "draft":
            for line in invoice.invoice_line_ids:
                line_tax_ids = line.tax_ids.ids
                if booking.tax_rule_id.id not in line_tax_ids:
                    _logger.warning(
                        "Booking %s: invoice %s tax mismatch — booking tax %s not on invoice line",
                        booking.booking_number, invoice.name, booking.tax_rule_id.name,
                    )
                    # Don't auto-fix posted invoices; draft invoices get an activity
                    invoice.activity_schedule(
                        "mail.mail_activity_data_warning",
                        summary=_("Tax Review Required"),
                        note=_(
                            "This invoice was linked to booking %s but the tax rates "
                            "differ. Please review and update the tax lines."
                        ) % booking.booking_number,
                        user_id=invoice.create_uid.id or self.env.user.id,
                    )


# Import here to avoid circular imports
from odoo import fields

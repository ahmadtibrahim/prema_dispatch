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
import logging
import uuid

from odoo import _, fields
from odoo.exceptions import AccessError, UserError, ValidationError

from .temperature_compat import to_canonical_temperature_mode, validate_temperature_request

_logger = logging.getLogger(__name__)

VALID_LOAD_TYPES = ("ltl", "ftl")

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
    ("corridor", "Corridor $/km"),
    ("rate_plan", "Historical Rate Plan (retired alias)"),
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

        self.load_type = data.get("load_type", data.get("shipment_type", "ltl"))
        self.equipment_type = to_canonical_temperature_mode(data.get("equipment_type", "dry"))
        self.required_temperature_c = data.get("required_temperature_c")
        self.required_temperature = data.get("required_temperature", "")

        self.pickup_stops = data.get("pickup_stops", [])
        self.delivery_stops = data.get("delivery_stops", [])
        self.transfer_allowed = data.get("transfer_allowed", True)
        self.requested_pickup_date = data.get("requested_pickup_date")
        self.requested_delivery_date = data.get("requested_delivery_date")
        self.same_day_requested = data.get("same_day_requested", False)

        self.pallets = data.get("pallets", 1)
        self.physical_pallets = data.get("physical_pallets", data.get("pallets", 1))
        self.shared_pallet_mode = data.get("shared_pallet_mode", False)
        self.pallet_allocations = data.get("pallet_allocations") or []
        self.weight_lbs = data.get("weight_lbs", 0.0)
        self.commodity = data.get("commodity", "")
        self.stackable = data.get("stackable", True)
        self.hazmat = data.get("hazmat", False)
        self.food_grade = data.get("food_grade", False)

        self.po_number = data.get("po_number", "")
        self.customer_reference = data.get("customer_reference", "")
        self.bol_number = data.get("bol_number", "")
        self.instructions = data.get("instructions", "")

        self.pricing_method = data.get("pricing_method", "corridor")
        self.agreed_rate = data.get("agreed_rate", 0.0)
        self.currency_id = data.get("currency_id")

        self.departure_id = data.get("departure_id")  # manual pricing methods only

        self.existing_invoice_id = data.get("existing_invoice_id")
        self.existing_sale_order_id = data.get("existing_sale_order_id")
        self.wa_negotiation_id = data.get("wa_negotiation_id")
        self.custom_quote_id = data.get("custom_quote_id")
        self.recurring_agreement_id = data.get("recurring_agreement_id")
        self.recurring_job_id = data.get("recurring_job_id")

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
        if self.load_type not in VALID_LOAD_TYPES:
            raise ValidationError(_("Invalid load_type: %s") % self.load_type)
        validate_temperature_request(self.equipment_type, self.required_temperature_c)


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

    def prepare_quote(self, normalized_request: NormalizedBookingRequest, session_ttl_minutes: int = 20) -> dict:
        """Create a pricing session / quote for the customer to review.

        Routes through ShipmentRoutingService (canonical engine) when coordinates
        are available, falling back to legacy pricing.calculate() for FSA-only mode.
        Returns a dict with quote_token, price, and expiration."""
        from ..services.shipment_routing_service import ShipmentRoutingService

        # Extract coordinates from normalized request stops
        pickup_lat = None
        pickup_lng = None
        delivery_lat = None
        delivery_lng = None
        if normalized_request.pickup_stops:
            pickup_lat = normalized_request.pickup_stops[0].get("latitude")
            pickup_lng = normalized_request.pickup_stops[0].get("longitude")
        if normalized_request.delivery_stops:
            delivery_lat = normalized_request.delivery_stops[-1].get("latitude")
            delivery_lng = normalized_request.delivery_stops[-1].get("longitude")

        _logger.info(
            "prepare_quote ENTRY: pu_lat=%s pu_lng=%s de_lat=%s de_lng=%s "
            "pu_stops=%s de_stops=%s pallets=%s phys=%s equip=%s date=%s",
            pickup_lat, pickup_lng, delivery_lat, delivery_lng,
            len(normalized_request.pickup_stops) if normalized_request.pickup_stops else 0,
            len(normalized_request.delivery_stops) if normalized_request.delivery_stops else 0,
            normalized_request.pallets, normalized_request.physical_pallets,
            normalized_request.equipment_type, normalized_request.requested_pickup_date,
        )

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

        # ── Route A: Coordinates available → ShipmentRoutingService (hub transfers) ──
        if pickup_lat and delivery_lat and pickup_lng and delivery_lng:
            _logger.info("prepare_quote: routing via ShipmentRoutingService "
                        "(pickup=%s,%s delivery=%s,%s)",
                        pickup_lat, pickup_lng, delivery_lat, delivery_lng)

            routing_svc = ShipmentRoutingService(self.env)
            route = routing_svc.plan_route(
                pickup_lat=float(pickup_lat),
                pickup_lng=float(pickup_lng),
                delivery_lat=float(delivery_lat),
                delivery_lng=float(delivery_lng),
                pallets=normalized_request.pallets,
                weight_lbs=normalized_request.weight_lbs,
                requested_pickup_date=normalized_request.requested_pickup_date,
                equipment=normalized_request.equipment_type,
            )

            if not route.available:
                _logger.error(
                    "prepare_quote FAILED: reason_code=%s reason=%s "
                    "pickup=(%s,%s) delivery=(%s,%s) pallets=%s weight=%s "
                    "equipment=%s date=%s snapshot=%s",
                    route.reason_code, route.reason,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    normalized_request.pallets, normalized_request.weight_lbs,
                    normalized_request.equipment_type,
                    normalized_request.requested_pickup_date,
                    route.routing_snapshot,
                )
                # Map technical reason codes to customer-friendly messages
                friendly_reason = {
                    "NO_PICKUP_REGION": "We could not determine the pickup service region.",
                    "NO_DELIVERY_REGION": "We could not determine the delivery service region.",
                    "MANUAL_QUOTE_PICKUP": "Pickup location is outside our scheduled service area.",
                    "MANUAL_QUOTE_DELIVERY": "Delivery location is outside our scheduled service area.",
                    "REQUESTED_PICKUP_DATE_NOT_SERVED": route.reason,
                    "NO_LEGS": "This shipment requires manual scheduling. Please request a quote.",
                    "NETWORK_DISABLED": "Scheduled service is not available for this region.",
                    "AMBIGUOUS_PICKUP": "Pickup region could not be determined precisely.",
                    "AMBIGUOUS_DELIVERY": "Delivery region could not be determined precisely.",
                    "MANUAL_REVIEW": "This shipment requires manual scheduling. Please request a quote.",
                    "INVALID_DATE": "The requested pickup date is not valid.",
                }.get(route.reason_code, "This shipment requires manual scheduling. Please request a quote.")
                raise UserError(_(friendly_reason))

            # Extract corridor/departure info from the first leg (for session compatibility)
            first_leg = route.legs[0] if route.legs else None
            corridor = self.env["logistics.corridor"].browse(first_leg.corridor_id) if first_leg and first_leg.corridor_id else self.env["logistics.corridor"]
            last_leg = route.legs[-1] if route.legs else None
            est_delivery = last_leg.departure_date if last_leg else None

            # Build price lines from legs
            price_lines = []
            for leg in route.legs:
                price_lines.append({
                    "label": f"Leg {leg.sequence} — {leg.corridor_name} ({leg.leg_type})",
                    "distance_km": leg.estimated_distance_km,
                    "pallets": leg.pallets,
                    "rate_per_km": leg.rate_per_km,
                    "pallet_rate_per_km": leg.pallet_rate_per_km,
                    "amount": leg.leg_price,
                    "departure_date": leg.departure_date,
                })
            route_total = sum(leg.leg_price for leg in route.legs)
            booking_min = 150.0
            final_price = max(route_total, booking_min)
            if route_total < booking_min:
                price_lines.append({
                    "label": "Minimum booking adjustment",
                    "distance_km": 0, "pallets": 0,
                    "rate_per_km": 0, "pallet_rate_per_km": 0,
                    "amount": round(final_price - route_total, 2),
                    "departure_date": None,
                })

            # Create pricing session
            Session = self.env["logistics.pricing.session"].sudo()
            # Extract saved location IDs from normalized request
            pu_saved_id = None
            de_saved_id = None
            if normalized_request.pickup_stops:
                pu_saved_id = normalized_request.pickup_stops[0].get("saved_location_id")
            if normalized_request.delivery_stops:
                de_saved_id = normalized_request.delivery_stops[0].get("saved_location_id")

            session = Session.create({
                "partner_id": normalized_request.partner_id,
                "pickup_fsa_id": pickup_fsa.id if pickup_fsa else None,
                "delivery_fsa_id": delivery_fsa.id if delivery_fsa else None,
                "corridor_id": corridor.id if corridor else None,
                "shipment_type": normalized_request.load_type,
                "temperature_mode": normalized_request.equipment_type,
                "required_temperature_c": normalized_request.required_temperature_c or 0.0,
                "pallets": normalized_request.physical_pallets,  # physical pallets for pricing/capacity
                "physical_pallets": normalized_request.physical_pallets,
                "shared_pallet_mode": normalized_request.shared_pallet_mode,
                "weight_lbs": sum(ds.get("weight_lbs", 500) for ds in normalized_request.delivery_stops) if normalized_request.delivery_stops else normalized_request.weight_lbs,
                "liftgate_pickup": normalized_request.liftgate_pickup,
                "liftgate_delivery": normalized_request.liftgate_delivery,
                "appointment": normalized_request.appointment,
                "residential": normalized_request.residential,
                "same_day_requested": normalized_request.same_day_requested,
                "pickup_date": first_leg.departure_date if first_leg else None,
                "delivery_date_estimate": est_delivery,
                "calculated_price": final_price,
                "price_snapshot": price_lines + [{"_pallet_allocs": normalized_request.pallet_allocations}],
                "route_snapshot": route.routing_snapshot,
                "pickup_saved_location_id": pu_saved_id,
                "delivery_saved_location_id": de_saved_id,
                "expires_at": fields.Datetime.now() + datetime.timedelta(minutes=session_ttl_minutes),
            })

            # Create per-stop records for multi-stop bookings
            StopModel = self.env["logistics.pricing.session.stop"].sudo()
            for i, ds in enumerate(normalized_request.delivery_stops):
                sl_id = ds.get("saved_location_id")
                sl = self.env["logistics.saved.location"].browse(sl_id) if sl_id else None
                stop_idx = i + 1  # 1-based stop index matching pallet_allocations
                # Compute allocated pallets and shared flag from pallet_allocations
                allocs = normalized_request.pallet_allocations or []
                stop_pallets = [a["pallet"] for a in allocs if stop_idx in (a.get("stops") or [])]
                stop_shared = len(stop_pallets) > 0 and any(
                    len(a.get("stops") or []) > 1 for a in allocs
                    if a["pallet"] in stop_pallets
                )
                StopModel.create({
                    "session_id": session.id,
                    "sequence": stop_idx,
                    "saved_location_id": sl_id,
                    "location_name": sl.name if sl else ds.get("city", ""),
                    "street": sl.street if sl else ds.get("address", ""),
                    "city": sl.city if sl else ds.get("city", ""),
                    "state_code": sl.state_id.code if sl and sl.state_id else "",
                    "postal_code": sl.postal_code if sl else ds.get("postal_code", ""),
                    "latitude": ds.get("latitude", 0),
                    "longitude": ds.get("longitude", 0),
                    "pallets": ds.get("pallets", 1),
                    "weight_lbs": ds.get("weight_lbs", 500),
                    "shared_pallet": stop_shared or ds.get("shared_pallet", False),
                    "timing_type": ds.get("timing_type", "flexible"),
                    "window_start": ds.get("window_start") or False,
                    "window_end": ds.get("window_end") or False,
                    "appointment_time": ds.get("appointment_time") or False,
                    "liftgate_delivery": ds.get("liftgate_delivery", False),
                    "appointment": ds.get("appointment", False),
                    "instructions": ds.get("instructions", ""),
                })

            return {
                "quote_token": session.token,
                "pickup_date": first_leg.departure_date if first_leg else None,
                "delivery_date": est_delivery,
                "calculated_price": final_price,
                "price_lines": price_lines,
                "lane_name": corridor.name if corridor else "Hub Transfer",
                "service_offering_name": "Scheduled LTL" if len(route.legs) == 1 else f"Hub Transfer ({len(route.legs)} legs)",
                "expires_at": session.expires_at.isoformat() if session.expires_at else None,
                "legs": len(route.legs),
            }

        # ── Route B: FSA-only fallback (legacy) ──
        if not pickup_fsa or not delivery_fsa:
            raise UserError(_("Could not resolve origin or destination postal code."))

        from ..services.pricing_service import PricingService
        pricing = PricingService(self.env)
        result = pricing.calculate(
            pickup_fsa, delivery_fsa,
            normalized_request.load_type,
            normalized_request.equipment_type,
            normalized_request.pallets,
            normalized_request.weight_lbs,
            normalized_request.liftgate_pickup,
            normalized_request.liftgate_delivery,
            normalized_request.appointment,
            normalized_request.residential,
            normalized_request.same_day_requested,
            partner=self.env["res.partner"].browse(normalized_request.partner_id),
            required_temperature_c=normalized_request.required_temperature_c,
            resolve_departures=True,
            reference_dt=normalized_request.requested_pickup_date,
        )

        if not result.available:
            friendly = result.reason or "unknown"
            # Don't expose internal reason codes to customers
            if friendly.startswith("no_corridor"):
                friendly = "This shipment requires manual scheduling. Please request a quote."
            raise UserError(_(friendly))

        # Extract saved location IDs from normalized request
        pu_saved_id = None
        de_saved_id = None
        if normalized_request.pickup_stops:
            pu_saved_id = normalized_request.pickup_stops[0].get("saved_location_id")
        if normalized_request.delivery_stops:
            de_saved_id = normalized_request.delivery_stops[0].get("saved_location_id")

        # Create pricing session
        Session = self.env["logistics.pricing.session"].sudo()
        session = Session.create({
            "partner_id": normalized_request.partner_id,
            "pickup_fsa_id": pickup_fsa.id,
            "delivery_fsa_id": delivery_fsa.id,
            "corridor_id": result.corridor.id,
            "shipment_type": normalized_request.load_type,
            "temperature_mode": normalized_request.equipment_type,
            "required_temperature_c": normalized_request.required_temperature_c or 0.0,
            "pallets": normalized_request.physical_pallets,
            "physical_pallets": normalized_request.physical_pallets,
            "shared_pallet_mode": normalized_request.shared_pallet_mode,
            "weight_lbs": sum(ds.get("weight_lbs", 500) for ds in normalized_request.delivery_stops) if normalized_request.delivery_stops else normalized_request.weight_lbs,
            "liftgate_pickup": normalized_request.liftgate_pickup,
            "liftgate_delivery": normalized_request.liftgate_delivery,
            "appointment": normalized_request.appointment,
            "residential": normalized_request.residential,
            "same_day_requested": normalized_request.same_day_requested,
            "pickup_date": result.pickup_date,
            "delivery_date_estimate": result.delivery_date_estimate,
            "calculated_price": result.calculated_price,
            "price_snapshot": list(result.price_lines) + [{"_pallet_allocs": normalized_request.pallet_allocations}],
            "route_snapshot": result.route_snapshot,
            "pickup_saved_location_id": pu_saved_id,
            "delivery_saved_location_id": de_saved_id,
            "expires_at": fields.Datetime.now() + datetime.timedelta(minutes=session_ttl_minutes),
        })

        # Create per-stop records for multi-stop bookings
        StopModel = self.env["logistics.pricing.session.stop"].sudo()
        for i, ds in enumerate(normalized_request.delivery_stops):
            sl_id = ds.get("saved_location_id")
            sl = self.env["logistics.saved.location"].browse(sl_id) if sl_id else None
            stop_idx = i + 1
            allocs = normalized_request.pallet_allocations or []
            stop_pallets = [a["pallet"] for a in allocs if stop_idx in (a.get("stops") or [])]
            stop_shared = len(stop_pallets) > 0 and any(
                len(a.get("stops") or []) > 1 for a in allocs
                if a["pallet"] in stop_pallets
            )
            StopModel.create({
                "session_id": session.id,
                "sequence": stop_idx,
                "saved_location_id": sl_id,
                "location_name": sl.name if sl else ds.get("city", ""),
                "street": sl.street if sl else ds.get("address", ""),
                "city": sl.city if sl else ds.get("city", ""),
                "state_code": sl.state_id.code if sl and sl.state_id else "",
                "postal_code": sl.postal_code if sl else ds.get("postal_code", ""),
                "latitude": ds.get("latitude", 0),
                "longitude": ds.get("longitude", 0),
                "pallets": ds.get("pallets", 1),
                "weight_lbs": ds.get("weight_lbs", 500),
                "shared_pallet": stop_shared or ds.get("shared_pallet", False),
                "allocated_pallets": stop_pallets,
                "timing_type": ds.get("timing_type", "flexible"),
                "window_start": ds.get("window_start") or False,
                "window_end": ds.get("window_end") or False,
                "appointment_time": ds.get("appointment_time") or False,
                "liftgate_delivery": ds.get("liftgate_delivery", False),
                "appointment": ds.get("appointment", False),
                "instructions": ds.get("instructions", ""),
            })

        return {
            "quote_token": session.token,
            "pickup_date": str(result.pickup_date),
            "delivery_date": str(result.delivery_date_estimate),
            "calculated_price": result.calculated_price,
            "price_lines": result.price_lines,
            "lane_name": result.corridor.name,
            "service_offering_name": "Scheduled LTL Corridor",
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        }

    def confirm_from_internal(
        self,
        normalized_request: NormalizedBookingRequest,
        existing_invoice: object = None,
        skip_invoice: bool = False,
        pricing_session: object = None,
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

        frozen_session = False
        if pricing_session:
            frozen_session = pricing_session.sudo().exists()
            if not frozen_session:
                raise UserError(_("This price is no longer available. Please get a new price."))
            frozen_session.ensure_one()
            existing = self.env["logistics.booking"].sudo().search([
                ("pricing_session_token", "=", frozen_session.token),
            ], limit=1)
            if existing:
                return existing
            self._validate_frozen_session(frozen_session, normalized_request)

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

        pickup_date = normalized_request.requested_pickup_date
        delivery_date = normalized_request.requested_delivery_date
        calculated_price = normalized_request.agreed_rate
        route_snapshot = False
        price_snapshot = False

        if frozen_session:
            self._validate_frozen_session(
                frozen_session, normalized_request, pickup_fsa, delivery_fsa,
            )
            pickup_date = frozen_session.pickup_date
            delivery_date = frozen_session.delivery_date_estimate
            calculated_price = frozen_session.calculated_price
            route_snapshot = frozen_session.route_snapshot
            price_snapshot = frozen_session.price_snapshot
        elif normalized_request.pricing_method in ("corridor", "rate_plan", "contract") and pickup_fsa and delivery_fsa:
            pricing = PricingService(self.env)
            result = pricing.calculate(
                pickup_fsa, delivery_fsa,
                normalized_request.load_type,
                normalized_request.equipment_type,
                normalized_request.pallets,
                normalized_request.weight_lbs,
                normalized_request.liftgate_pickup,
                normalized_request.liftgate_delivery,
                normalized_request.appointment,
                normalized_request.residential,
                normalized_request.same_day_requested,
                partner=partner,
                required_temperature_c=normalized_request.required_temperature_c,
                resolve_departures=True,
                reference_dt=normalized_request.requested_pickup_date,
            )
            if not result.available and normalized_request.pricing_method in ("corridor", "rate_plan"):
                raise UserError(_("No service available: %s") % (result.reason or "unknown"))
            if result.available:
                pickup_date = result.pickup_date
                delivery_date = result.delivery_date_estimate
                route_snapshot = result.route_snapshot
                price_snapshot = result.price_lines
                if not calculated_price:
                    calculated_price = result.calculated_price

        # Fallback: use agreed_rate for contract/manual/negotiated pricing
        # only — never a silent $0 and never a Rate Plan lookup.
        if not calculated_price and normalized_request.agreed_rate:
            calculated_price = normalized_request.agreed_rate
        if not calculated_price and not route_snapshot:
            raise UserError(_(
                "Cannot confirm booking: no price could be resolved and no "
                "agreed rate was supplied."
            ))

        # ── 5. Build booking vals ─────────────────────────────────────────
        booking_vals = {
            "partner_id": partner.id,
            "booking_channel": {
                "portal": "customer_portal",
                "whatsapp": "whatsapp",
            }.get(normalized_request.source_channel, normalized_request.source_channel),
            "source_channel": normalized_request.source_channel,
            "source_model": normalized_request.source_model or "",
            "source_res_id": normalized_request.source_res_id or 0,
            "source_reference": normalized_request.source_reference or "",
            "idempotency_key": normalized_request.idempotency_key,
            "shipment_type": normalized_request.load_type,
            "temperature_mode": normalized_request.equipment_type,
            "required_temperature": normalized_request.required_temperature or "",
            "required_temperature_c": (
                normalized_request.required_temperature_c
                if normalized_request.required_temperature_c is not None
                else 0.0
            ),
            "pallets": normalized_request.physical_pallets,
            "physical_pallets": normalized_request.physical_pallets,
            "shared_pallet_mode": normalized_request.shared_pallet_mode,
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
            "same_day_requested": normalized_request.same_day_requested,
        }

        if frozen_session:
            booking_vals["pricing_session_token"] = frozen_session.token

        # Cross-reference fields
        if normalized_request.wa_negotiation_id:
            booking_vals["wa_negotiation_id"] = normalized_request.wa_negotiation_id
        if normalized_request.recurring_agreement_id:
            booking_vals["recurring_agreement_id"] = normalized_request.recurring_agreement_id
        if normalized_request.recurring_job_id:
            booking_vals["recurring_job_id"] = normalized_request.recurring_job_id

        if pickup_fsa:
            booking_vals["pickup_fsa_id"] = pickup_fsa.id
            booking_vals["pickup_address"] = pickup_fsa.display_city or pickup_fsa.fsa
        if delivery_fsa:
            booking_vals["delivery_fsa_id"] = delivery_fsa.id
            booking_vals["delivery_address"] = delivery_fsa.display_city or delivery_fsa.fsa
        if pickup_date:
            booking_vals["pickup_date"] = pickup_date
        if delivery_date:
            booking_vals["estimated_delivery_date"] = delivery_date
        if route_snapshot:
            booking_vals["route_snapshot"] = route_snapshot
        if price_snapshot:
            # Embed pallet_allocations into price_snapshot for zero-migration storage
            allocs = normalized_request.pallet_allocations
            if allocs:
                price_snapshot = list(price_snapshot) + [{"_pallet_allocs": allocs}]
            booking_vals["price_snapshot"] = price_snapshot
        if normalized_request.departure_id:
            booking_vals["departure_id"] = normalized_request.departure_id

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
                if frozen_session:
                    # Serialize consumption of one quote token. A duplicate
                    # browser submit or two staff clicks can never reserve
                    # the same frozen departures twice.
                    self.env.cr.execute(
                        "SELECT id FROM logistics_pricing_session WHERE id = %s FOR UPDATE",
                        [frozen_session.id],
                    )
                    frozen_session.invalidate_recordset(["state", "expires_at"])
                    existing = Booking.sudo().search([
                        ("pricing_session_token", "=", frozen_session.token),
                    ], limit=1)
                    if existing:
                        return existing
                    self._validate_frozen_session(
                        frozen_session, normalized_request, pickup_fsa, delivery_fsa,
                    )

                booking = Booking.sudo().create(booking_vals)

                # Create booking stops
                self._create_booking_stops(
                    booking, normalized_request.pickup_stops, normalized_request.delivery_stops,
                )
                # Flush pending creates to DB so Stop.search() finds them,
                # then invalidate One2many cache so downstream methods see them.
                self.env.flush_all()
                booking.invalidate_recordset(['stop_ids'])

                # Create booking lines
                self._create_booking_lines(booking, normalized_request)

                # Apply tax decision
                booking._apply_tax_decision()

                # Create or link invoice
                if not skip_invoice:
                    if existing_invoice:
                        self._link_existing_invoice(booking, existing_invoice)
                    else:
                        booking._create_draft_invoice()

                # Create booking legs WITH atomic capacity reservation — the
                # ONE reservation path for every channel. Only corridor-
                # priced channels (scheduled network) require an exact leg;
                # legacy ad-hoc/imported-invoice jobs rely on prema.dispatch.job.
                self.create_legs_and_reserve(
                    booking,
                    required=(
                        bool(frozen_session)
                        or normalized_request.pricing_method in ("corridor", "rate_plan")
                    ),
                )

                # Only after the exact departure(s) are reserved can the
                # Planner create the correct truck/day operation cards.
                booking._create_dispatch_job()

                if frozen_session:
                    frozen_session.write({"state": "converted"})

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
        if booking.dispatch_job_ids:
            cancel_stage = self.env["prema.dispatch.stage"].sudo().search(
                [("stage_type", "=", "cancelled")], limit=1
            )
            if cancel_stage:
                booking.dispatch_job_ids.write({"stage_id": cancel_stage.id})

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

    def _validate_frozen_session(
        self, session, normalized_request, pickup_fsa=None, delivery_fsa=None,
    ):
        """Validate a server-side quote before it is consumed.

        The displayed quote is authoritative, but only for the exact request
        that produced it. This guard is shared by phone/internal adapters and
        is re-run while the session row is locked immediately before create.
        """
        partner = self.env["res.partner"].sudo().browse(
            normalized_request.partner_id
        ).exists()
        if not partner:
            raise UserError(_("Customer not found."))
        if session.partner_id.commercial_partner_id != partner.commercial_partner_id:
            raise AccessError(_("This pricing result does not belong to this customer."))
        if session.is_expired():
            raise UserError(_("This price has expired. Please get a new price."))
        if session.state != "priced":
            raise UserError(_("This price has already been used. Open the existing booking."))

        snapshot = session.route_snapshot or {}
        if snapshot.get("pricing_authority") != "corridor_per_km":
            raise UserError(_(
                "This quote used the retired pricing setup. Please get a new corridor-based price."
            ))
        if not snapshot.get("legs"):
            raise UserError(_("This quote has no scheduled departure. Please get a new price."))

        expected_temperature = (
            normalized_request.required_temperature_c
            if normalized_request.required_temperature_c is not None
            else 0.0
        )
        scalar_mismatches = (
            session.shipment_type != normalized_request.load_type,
            session.temperature_mode != normalized_request.equipment_type,
            abs((session.required_temperature_c or 0.0) - expected_temperature) > 0.0001,
            session.pallets != normalized_request.pallets,
            abs((session.weight_lbs or 0.0) - (normalized_request.weight_lbs or 0.0)) > 0.01,
            bool(session.liftgate_pickup) != bool(normalized_request.liftgate_pickup),
            bool(session.liftgate_delivery) != bool(normalized_request.liftgate_delivery),
            bool(session.appointment) != bool(normalized_request.appointment),
            bool(session.residential) != bool(normalized_request.residential),
            bool(session.same_day_requested) != bool(normalized_request.same_day_requested),
        )
        if any(scalar_mismatches):
            raise UserError(_("Shipment details changed after pricing. Please get a new price."))
        if pickup_fsa and session.pickup_fsa_id != pickup_fsa:
            raise UserError(_("Pickup area changed after pricing. Please get a new price."))
        if delivery_fsa and session.delivery_fsa_id != delivery_fsa:
            raise UserError(_("Delivery area changed after pricing. Please get a new price."))

    def _create_booking_stops(self, booking, pickup_stops, delivery_stops):
        """Create real booking stops from raw pickup/delivery stop dicts.
        THE single stop-creation path for every channel — no channel may
        fabricate a placeholder "Pickup Location"/"Delivery Location" stop."""
        Stop = self.env["logistics.booking.stop"].sudo()
        seq = 10

        for pu in pickup_stops:
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "pickup",
                "saved_location_id": pu.get("saved_location_id") or False,
                "logistics_saved_location_id": pu.get("logistics_saved_location_id") or False,
                "company_name": pu.get("company_name", ""),
                "street": pu.get("street", ""),
                "city": pu.get("city", ""),
                "province_state": pu.get("province_state", pu.get("province", "")),
                "postal_zip": pu.get("postal_code", ""),
                "formatted_address": pu.get("formatted_address", ""),
                "google_place_id": pu.get("google_place_id", ""),
                "latitude": pu.get("latitude", 0.0),
                "longitude": pu.get("longitude", 0.0),
                "contact_name": pu.get("contact_name", ""),
                "phone": pu.get("phone", ""),
                "pallet_count": pu.get("pallet_count", 0),
                "weight_lb": pu.get("weight_lb", pu.get("weight_lbs", 0.0)),
                "liftgate_required": pu.get("liftgate_required", False),
                "instructions": pu.get("instructions", ""),
                "reference": pu.get("reference", ""),
                "timing_type": pu.get("timing_type", "flexible"),
                "window_start": pu.get("window_start") or False,
                "window_end": pu.get("window_end") or False,
                "appointment_time": pu.get("appointment_time") or False,
                "timezone": pu.get("timezone") or "",
            })
            seq += 10

        for dl in delivery_stops:
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "delivery",
                "saved_location_id": dl.get("saved_location_id") or False,
                "logistics_saved_location_id": dl.get("logistics_saved_location_id") or False,
                "company_name": dl.get("company_name", ""),
                "street": dl.get("street", ""),
                "city": dl.get("city", ""),
                "province_state": dl.get("province_state", dl.get("province", "")),
                "postal_zip": dl.get("postal_code", ""),
                "formatted_address": dl.get("formatted_address", ""),
                "google_place_id": dl.get("google_place_id", ""),
                "latitude": dl.get("latitude", 0.0),
                "longitude": dl.get("longitude", 0.0),
                "contact_name": dl.get("contact_name", ""),
                "phone": dl.get("phone", ""),
                "pallet_count": dl.get("pallet_count", 0),
                "weight_lb": dl.get("weight_lb", dl.get("weight_lbs", 0)),
                "liftgate_required": dl.get("liftgate_required", False),
                "instructions": dl.get("instructions", ""),
                "reference": dl.get("reference", ""),
                "timing_type": dl.get("timing_type", "flexible"),
                "window_start": dl.get("window_start") or False,
                "window_end": dl.get("window_end") or False,
                "appointment_time": dl.get("appointment_time") or False,
                "timezone": dl.get("timezone") or "",
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

    def create_legs_and_reserve(self, booking, required=True):
        """THE single atomic leg-creation + capacity-reservation path for
        EVERY booking channel (portal, phone, internal, invoice, WhatsApp,
        custom quote, recurring, API). Consumes booking.route_snapshot ONLY —
        never re-resolves routes, never re-picks a departure, never rereads
        live pricing. All legs are created, or none are (the caller wraps
        this in a savepoint) — there is no "pending, unreserved" fallback leg.

        Two supported inputs, in priority order:
          1. booking.route_snapshot["legs"] — each leg already carries an
             exact departure_id (frozen at quote time by PricingService /
             DepartureResolver). This is the path for every rate-plan-priced
             booking, on every channel.
          2. booking.departure_id — a single, manually-assigned departure
             (used only for contract/negotiated/manual/imported-invoice
             pricing methods, which have no RouteResolver route). Still
             fully capacity-validated — never a "pending" no-reservation leg.

        Raises UserError and creates nothing if neither is available, or if
        any leg fails revalidation.
        """
        booking.ensure_one()
        snap = booking.route_snapshot or {}
        leg_snaps = snap.get("legs") or []

        if leg_snaps:
            return self._create_legs_from_snapshot(booking, snap, leg_snaps)
        if booking.departure_id:
            return self._create_single_manual_leg(booking, booking.departure_id)
        if not required:
            # Legacy ad-hoc dispatch flow (e.g. invoice "Book Load"): no
            # scheduled-network route applies; prema.dispatch.job (already
            # created) is that flow's real capacity/assignment mechanism.
            _logger.info(
                "Booking %s: no route snapshot / departure — skipping leg "
                "creation (non rate-plan channel).", booking.booking_number,
            )
            return self.env["logistics.booking.leg"]
        raise UserError(_(
            "Cannot confirm booking %s: no frozen route snapshot and no "
            "manually-assigned departure. A booking may not be confirmed "
            "without an exact, capacity-validated departure."
        ) % (booking.booking_number or booking.id))

    def _lock_and_validate_departures(self, departure_ids, equipment, pallets, weight_lbs,
                                       allow_pinwheel_override=False):
        """Sort + SELECT...FOR UPDATE every departure, then revalidate vehicle
        assignment, temperature compatibility, and pallet/weight capacity
        INSIDE the lock. Returns {departure_id: fleet.vehicle} on success;
        raises UserError on any violation (nothing is written yet)."""
        from .capacity_engine import CapacityEngine
        from .temperature_compat import vehicle_accepts

        Departure = self.env["logistics.corridor.departure"].sudo()
        engine = CapacityEngine(self.env)
        vehicles_by_departure = {}

        for did in sorted(set(departure_ids)):
            self.env.cr.execute(
                "SELECT id FROM logistics_corridor_departure WHERE id = %s FOR UPDATE",
                [did],
            )
            dep = Departure.browse(did)
            if not dep.exists() or dep.status != "scheduled" or not dep.active:
                raise UserError(_("Departure %s is no longer available. Please get a new price.") % did)

            vehicle = dep.vehicle_id
            if not vehicle:
                raise UserError(_("Departure %s has no assigned vehicle and cannot be booked.") % dep.name)
            if not vehicle_accepts(vehicle_is_reefer=bool(vehicle.x_reefer), requested_mode=equipment):
                raise UserError(_(
                    "Departure %s's vehicle is not compatible with the requested "
                    "temperature mode. Please get a new price."
                ) % dep.name)

            straight = vehicle.straight_pallet_capacity or 0
            pinwheel = vehicle.pin_wheel_pallet_capacity or 0
            payload = vehicle.x_max_payload_lbs or 0.0
            if not straight or not pinwheel or not payload:
                raise UserError(_(
                    "Departure %s's vehicle capacity is not configured and cannot be booked."
                ) % dep.name)

            peak = engine.compute_departure_peak(dep)
            projected_pallets = peak["peak_pallets"] + pallets
            projected_weight = peak["peak_weight"] + weight_lbs

            if projected_weight > payload:
                raise UserError(_(
                    "Weight capacity exceeded on %(dep)s: %(cur).0f lb + %(new).0f lb > %(max).0f lb.",
                    dep=dep.name, cur=peak["peak_weight"], new=weight_lbs, max=payload,
                ))
            if projected_pallets > pinwheel:
                raise UserError(_(
                    "Pallet capacity exceeded on %(dep)s: %(cur)d + %(new)d = %(total)d > %(max)d.",
                    dep=dep.name, cur=peak["peak_pallets"], new=pallets,
                    total=projected_pallets, max=pinwheel,
                ))
            if projected_pallets > straight and not allow_pinwheel_override:
                raise UserError(_(
                    "%(total)d pallets on %(dep)s requires dispatcher override (pinwheel mode).",
                    total=projected_pallets, dep=dep.name,
                ))

            vehicles_by_departure[did] = vehicle

        return vehicles_by_departure

    def _create_legs_from_snapshot(self, booking, snap, leg_snaps):
        Leg = self.env["logistics.booking.leg"].sudo()
        Stop = self.env["logistics.booking.stop"].sudo()

        departure_ids = [ls.get("departure_id") for ls in leg_snaps]
        if not all(departure_ids):
            raise UserError(_(
                "Cannot confirm booking %s: the frozen route snapshot is "
                "missing an exact departure for one or more legs."
            ) % (booking.booking_number or booking.id))

        equipment = snap.get("temperature_mode", "dry")
        pallets = snap.get("pallets") or booking.pallets
        weight_lbs = snap.get("weight_lbs") or booking.weight_lbs
        vehicles_by_departure = self._lock_and_validate_departures(
            departure_ids, equipment, pallets, weight_lbs,
            allow_pinwheel_override=bool(booking.capacity_override),
        )

        # Frozen vehicle must still match what was quoted — a vehicle swap
        # on the departure since quote time invalidates the frozen price.
        for ls in leg_snaps:
            frozen_vehicle_id = ls.get("vehicle_id")
            actual_vehicle = vehicles_by_departure[ls["departure_id"]]
            if frozen_vehicle_id and actual_vehicle.id != frozen_vehicle_id:
                raise UserError(_(
                    "The vehicle assigned to a departure in this quote has "
                    "changed. Please get a new price."
                ))

        # Use search() instead of booking.stop_ids One2many to avoid ORM
        # cache staleness: _create_booking_stops ran in the same transaction
        # but the One2many field may not reflect newly-created records yet.
        pickup_stop = Stop.search([
            ("booking_id", "=", booking.id), ("stop_type", "=", "pickup"),
        ], order="sequence", limit=1)
        delivery_stop = Stop.search([
            ("booking_id", "=", booking.id), ("stop_type", "=", "delivery"),
        ], order="sequence", limit=1)
        if not pickup_stop or not delivery_stop:
            raise UserError(_(
                "Cannot confirm booking %s: real pickup/delivery stops are missing."
            ) % (booking.booking_number or booking.id))

        currency = self.env["res.currency"].sudo().browse(
            leg_snaps[0].get("currency_id") or False
        ).exists()
        currency = currency or booking.currency_id or self.env.company.currency_id

        Region = self.env["logistics.region"].sudo()
        created_legs = self.env["logistics.booking.leg"]
        hub_stop_by_hub_id = {}
        for i, ls in enumerate(leg_snaps):
            is_first, is_last = (i == 0), (i == len(leg_snaps) - 1)
            origin = pickup_stop if is_first else None
            dest = delivery_stop if is_last else None

            hub_id = ls.get("hub_id") or ls.get("transfer_hub_id")
            if hub_id:
                hub_stop = hub_stop_by_hub_id.get(hub_id)
                if not hub_stop:
                    Hub = self.env["logistics.hub"].sudo().browse(hub_id)
                    hub_stop = Stop.create({
                        "booking_id": booking.id,
                        "sequence": 50,
                        "stop_type": "delivery" if is_first else "pickup",
                        "company_name": Hub.public_name,
                        "street": Hub.saved_location_id.street or Hub.saved_location_id.address or "",
                        "city": Hub.saved_location_id.city or "",
                        "saved_location_id": ls.get("hub_location_id") or False,
                    })
                    hub_stop_by_hub_id[hub_id] = hub_stop
                if is_first:
                    dest = hub_stop
                if is_last:
                    origin = hub_stop

            frozen_price = ls.get("price", 0.0)
            if currency:
                frozen_price = currency.round(frozen_price)

            origin_region = Region.browse(ls.get("origin_region_id") or False).exists()
            if not origin_region:
                origin_region = Region.search(
                    [("code", "=", ls.get("origin_region", ""))], limit=1,
                )
            dest_region = Region.browse(ls.get("dest_region_id") or False).exists()
            if not dest_region:
                dest_region = Region.search(
                    [("code", "=", ls.get("dest_region", ""))], limit=1,
                )

            leg = Leg.create({
                "booking_id": booking.id,
                "sequence": (i + 1) * 10,
                "leg_type": "direct" if len(leg_snaps) == 1 else ("feeder" if is_first else "linehaul"),
                "origin_stop_id": origin.id,
                "destination_stop_id": dest.id,
                "departure_id": ls["departure_id"],
                "origin_region_id": origin_region.id if origin_region else False,
                "destination_region_id": dest_region.id if dest_region else False,
                "lane_id": ls.get("lane_id", False),
                "offering_id": ls.get("offering_id", False),
                "rate_plan_id": ls.get("rate_plan_id", False),
                "rate_plan_name": ls.get("rate_plan_name", ""),
                "rate_plan_version": ls.get("rate_plan_version", 0),
                "currency_id": currency.id if currency else False,
                "frozen_leg_price": frozen_price,
                "frozen_price_breakdown": ls.get("price_lines", []),
                "transfer_hub_id": hub_id or False,
                "pickup_date": ls.get("pickup_date") or ls.get("departure_date") or booking.pickup_date,
                "delivery_date": ls.get("delivery_date") or ls.get("departure_date") or booking.estimated_delivery_date,
                "pallets": pallets,
                "weight_lbs": weight_lbs,
                "status": "scheduled",
                "reservation_state": "reserved",
                "customer_visible": True,
            })
            created_legs += leg

        if len(created_legs) > 1:
            booking.write({"is_multi_leg": True})
        if created_legs:
            booking.write({"departure_id": created_legs[0].departure_id.id})
        self._refresh_departure_peaks(vehicles_by_departure.keys())
        _logger.info(
            "BookingOrchestrationService: reserved capacity for booking %s on departures %s",
            booking.booking_number, list(vehicles_by_departure.keys()),
        )
        return created_legs

    def _create_single_manual_leg(self, booking, departure):
        """Manual/contract/negotiated pricing path: staff picked one exact
        departure directly (no RouteResolver route exists for it). Still
        fully capacity-validated — never a 'pending' unreserved leg."""
        pickups = booking.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")
        deliveries = booking.stop_ids.filtered(lambda s: s.stop_type == "delivery").sorted("sequence")
        if not pickups or not deliveries:
            raise UserError(_(
                "Cannot confirm booking %s: real pickup/delivery stops are missing."
            ) % (booking.booking_number or booking.id))

        equipment = booking.temperature_mode or "dry"
        vehicles_by_departure = self._lock_and_validate_departures(
            [departure.id], equipment, booking.pallets, booking.weight_lbs,
            allow_pinwheel_override=bool(booking.capacity_override),
        )

        leg = self.env["logistics.booking.leg"].sudo().create({
            "booking_id": booking.id,
            "sequence": 10,
            "leg_type": "direct",
            "origin_stop_id": pickups[0].id,
            "destination_stop_id": deliveries[-1].id,
            "departure_id": departure.id,
            "pickup_date": departure.departure_date,
            "delivery_date": booking.estimated_delivery_date or departure.departure_date,
            "pallets": booking.pallets,
            "weight_lbs": booking.weight_lbs,
            "status": "scheduled",
            "reservation_state": "reserved",
            "customer_visible": True,
        })
        self._refresh_departure_peaks(vehicles_by_departure.keys())
        return leg

    def _refresh_departure_peaks(self, departure_ids):
        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        Departure = self.env["logistics.corridor.departure"].sudo()
        for did in departure_ids:
            dep = Departure.browse(did)
            peak = engine.compute_departure_peak(dep)
            dep.write({
                "peak_pallets": peak.get("peak_pallets", 0),
                "total_handled_pallets": peak.get("total_handled", 0),
            })

    def _link_existing_invoice(self, booking, invoice):
        """Link an existing invoice to this booking and validate tax alignment."""
        booking.write({"invoice_id": invoice.id, "invoice_created_at": fields.Datetime.now()})
        invoice.sudo().write({"logistics_booking_id": booking.id})

        # Scheduled LTL uses the frozen corridor quote, never the invoice's
        # pre-existing total as a pricing bypass. Keep other invoice lines
        # intact and synchronize the configured freight line only.
        if (booking.route_snapshot or {}).get("pricing_authority") == "corridor_per_km":
            if invoice.state != "draft":
                raise UserError(_("A scheduled LTL booking can only link to a draft invoice."))
            product, _country = booking._select_freight_product()
            if not product:
                raise UserError(_(
                    "The configured freight product is missing. Configure it before booking Scheduled LTL from an invoice."
                ))
            freight_lines = invoice.invoice_line_ids.filtered(lambda line: line.product_id == product)
            values = {
                "name": booking._generate_invoice_description(),
                "quantity": 1.0,
                "price_unit": booking.calculated_price,
            }
            if freight_lines:
                freight_lines[:1].write(values)
            else:
                income_account = (
                    product.property_account_income_id
                    or product.categ_id.property_account_income_categ_id
                    or invoice.journal_id.default_account_id
                )
                if not income_account:
                    raise UserError(_(
                        "No income account is configured for the freight product or invoice journal. "
                        "Configure one before booking Scheduled LTL from an invoice."
                    ))
                self.env["account.move.line"].sudo().create({
                    **values,
                    "move_id": invoice.id,
                    "product_id": product.id,
                    "account_id": income_account.id,
                    "tax_ids": [(6, 0, booking.tax_rule_id.ids)],
                })

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

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
        # Generalized route builder: the full ordered stop list with stable
        # stop_keys (pickups + deliveries interleaved as the customer
        # ordered them). Only movement_v1 requests carry it.
        self.route_stops = data.get("route_stops") or []
        self.transfer_allowed = data.get("transfer_allowed", True)
        self.requested_pickup_date = data.get("requested_pickup_date")
        self.requested_delivery_date = data.get("requested_delivery_date")
        self.same_day_requested = data.get("same_day_requested", False)

        self.pallets = data.get("pallets", 1)
        self.physical_pallets = data.get("physical_pallets", data.get("pallets", 1))
        self.shared_pallet_mode = data.get("shared_pallet_mode", False)
        self.pallet_allocations = data.get("pallet_allocations") or []
        # Architecture discriminator — explicit at creation. Default legacy
        # so no existing channel silently flips to the movement bridge.
        self.route_model_version = data.get("route_model_version", "legacy")
        self.pallet_movements = data.get("pallet_movements") or []
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
        if self.route_model_version not in ("legacy", "movement_v1"):
            raise ValidationError(_("Invalid route_model_version: %s") % self.route_model_version)
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

    def _ltl_additional_stop_charge(self, normalized_request, corridor):
        """LTL additional-stop charge, corridor-configured.

        Delivery stops are grouped by their saved-location city. Each city
        with N stops adds (N - 1) charges; the totals are summed across
        cities (e.g. 3 Ottawa stops → 2 charges; 4 stops in one city →
        3 charges; a lone Belleville stop beside two Ottawa stops → only
        the extra Ottawa stop is charged).

        Additional PICKUP stops (milk-run, movement_v1) are charged one
        per stop beyond the first via ltl_additional_pickup_charge —
        same-city grouping does not apply to origins.

        Returns (additional_stop_count, additional_stop_rate,
                 additional_stop_total). Always (0, 0.0, 0.0) for FTL or
        when the corridor has no configured charge.
        """
        if normalized_request.load_type != "ltl" or not corridor:
            return 0, 0.0, 0.0
        rate = corridor.ltl_additional_stop_charge or 0.0
        city_counts = {}
        for stop in normalized_request.delivery_stops or []:
            city = (stop.get("city") or "").strip().lower()
            if city:
                city_counts[city] = city_counts.get(city, 0) + 1
        count = sum(max(n - 1, 0) for n in city_counts.values())
        if not count:
            return 0, rate, 0.0
        return count, rate, round(count * rate, 2) if rate else 0.0

    def _ltl_additional_pickup_charge(self, normalized_request, corridor):
        """Additional-pickup charge for generalized milk-run bookings:
        one corridor-configured charge per pickup stop beyond the first.
        Returns (count, rate, total); (0, 0.0, 0.0) when N/A."""
        if normalized_request.load_type != "ltl" or not corridor:
            return 0, 0.0, 0.0
        rate = corridor.ltl_additional_pickup_charge or 0.0
        route_pickups = [
            s for s in (normalized_request.route_stops or [])
            if s.get("stop_type") == "pickup"
        ]
        if len(route_pickups) <= 1:
            return 0, rate, 0.0
        count = len(route_pickups) - 1
        return count, rate, round(count * rate, 2) if rate else 0.0

    def _milk_run_stop_cost_allocations(self, route_stops, milk_run, subtotal):
        """Explanatory per-stop cost allocations for a route-level milk run.

        Each served delivery is allocated a share of the authoritative
        route subtotal (discounted, minimum-floored) proportional to its
        billable distance × pallets. Cents are rounded and the residual is
        applied to the largest share, so the allocations sum EXACTLY to the
        subtotal — they are explanatory, never a second pricing path.
        """
        pallets_by_key = {
            rs.get("stop_key", ""): int(rs.get("pallets") or 0)
            for rs in (route_stops or [])
            if rs.get("stop_type") == "delivery"
        }
        weighted = []
        for entry in milk_run.get("per_stop") or []:
            if entry.get("outcome") != "available" or not entry.get("billable_km"):
                continue
            pallets = pallets_by_key.get(entry.get("stop_key", "")) or 1
            weighted.append((entry, pallets, entry["billable_km"] * pallets))
        total_weight = sum(w for _, _, w in weighted)
        if not total_weight:
            return []
        allocations = []
        for entry, pallets, weight in weighted:
            allocations.append({
                "stop_key": entry["stop_key"],
                "city": entry["city"],
                "pallets": pallets,
                "billable_km": entry["billable_km"],
                "amount": round(subtotal * weight / total_weight, 2),
            })
        # Rounding drift (floating-point) → the largest share, keeping the
        # allocations an exact decomposition of the subtotal.
        residual = round(subtotal - sum(a["amount"] for a in allocations), 2)
        if residual:
            largest = max(allocations, key=lambda a: (a["amount"], a["billable_km"]))
            largest["amount"] = round(largest["amount"] + residual, 2)
        return allocations

    def prepare_quote(self, normalized_request: NormalizedBookingRequest, session_ttl_minutes: int = 20,
                      requested_departure_id=None) -> dict:
        """Create a pricing session / quote for the customer to review.

        Routes through ShipmentRoutingService (canonical engine) when coordinates
        are available, falling back to legacy pricing.calculate() for FSA-only mode.
        requested_departure_id (from the calendar-selected date) is server-
        re-validated inside the routing service so the quote binds to the
        EXACT departure the customer selected — never a silently different one.
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
            # Generalized movement_v1 requests carry the FULL ordered route
            # stop list. They price at ROUTE LEVEL through the FURTHEST
            # served point (Brampton → Belleville → Ottawa prices through
            # Ottawa) — never first pickup → last-entered delivery.
            # Legacy delivery-only requests keep the direct plan_route path.
            route_stops_for_route = normalized_request.route_stops or []
            if route_stops_for_route:
                route = routing_svc.plan_milk_run_route(
                    stops=route_stops_for_route,
                    pallets=normalized_request.pallets,
                    weight_lbs=normalized_request.weight_lbs,
                    requested_pickup_date=normalized_request.requested_pickup_date,
                    equipment=normalized_request.equipment_type,
                    shipment_type=normalized_request.load_type,
                    requested_departure_id=requested_departure_id,
                )
            else:
                route = routing_svc.plan_route(
                    pickup_lat=float(pickup_lat),
                    pickup_lng=float(pickup_lng),
                    delivery_lat=float(delivery_lat),
                    delivery_lng=float(delivery_lng),
                    pallets=normalized_request.pallets,
                    weight_lbs=normalized_request.weight_lbs,
                    requested_pickup_date=normalized_request.requested_pickup_date,
                    equipment=normalized_request.equipment_type,
                    shipment_type=normalized_request.load_type,
                    requested_departure_id=requested_departure_id,
                    # Customer facilities: the leg ETAs become
                    # facility-hours-aware (never before facility opens) —
                    # the same authority the calendar probes use.
                    pickup_stop=(
                        normalized_request.pickup_stops[0]
                        if normalized_request.pickup_stops else None),
                    delivery_stop=(
                        normalized_request.delivery_stops[-1]
                        if normalized_request.delivery_stops else None),
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
                    "FTL_DISPATCHER_APPROVAL": "This load qualifies for Full Truckload and needs dispatcher approval. Please contact us for pricing.",
                    "FTL_REQUIRES_DIRECT": "Full Truckload service requires a direct corridor between pickup and delivery. Please contact us for pricing.",
                    "FTL_RATE_NOT_CONFIGURED": "Full Truckload pricing is not configured for this lane. Please contact us for pricing.",
                }.get(route.reason_code, "This shipment requires manual scheduling. Please request a quote.")
                raise UserError(_(friendly_reason))

            # Extract corridor/departure info from the first leg (for session compatibility)
            first_leg = route.legs[0] if route.legs else None
            corridor = self.env["logistics.corridor"].browse(first_leg.corridor_id) if first_leg and first_leg.corridor_id else self.env["logistics.corridor"]
            last_leg = route.legs[-1] if route.legs else None
            # The REAL delivery date from the routing engine's timing chain
            # (corridor stop config, travel-calc fallback) — never the
            # departure date (a 550 km run is not same-day delivery).
            est_delivery = route.estimated_delivery or (
                last_leg.departure_date if last_leg else None)

            # Build price lines from legs. Each leg carries the canonical
            # weight-aware pricing breakdown (frozen at quote time) so the
            # Step-3 customer breakdown renders EXACTLY what was priced —
            # no independent recalculation in the template.
            price_lines = []
            for leg in route.legs:
                formula = dict(leg.pricing_formula or {})
                price_lines.append({
                    "label": f"Leg {leg.sequence} — {leg.corridor_name} ({leg.leg_type})",
                    "distance_km": leg.estimated_distance_km,
                    "pallets": leg.pallets,
                    "rate_per_km": leg.rate_per_km,
                    "pallet_rate_per_km": leg.pallet_rate_per_km,
                    "amount": leg.leg_price,
                    "departure_date": leg.departure_date,
                    # Weight-pricing components from the canonical
                    # calculator (corridor config authority).
                    "base_leg_charge": formula.get("base_leg_charge"),
                    "included_weight_lbs": formula.get("shipment_included_weight"),
                    "excess_weight_lbs": formula.get("extra_weight_lbs"),
                    "excess_weight_rate_per_lb": formula.get("excess_weight_rate_per_lb"),
                    "excess_weight_charge": formula.get("extra_weight_charge"),
                    "included_weight_per_pallet": formula.get("included_weight_per_pallet"),
                    "actual_weight_lbs": formula.get("actual_weight_lbs"),
                    "weight_pricing_method": formula.get("weight_pricing_method"),
                })
            route_total = sum(leg.leg_price for leg in route.legs)
            is_ftl = route.routing_snapshot.get("pricing_mode") == "ftl"
            # Minimum Booking Charge comes from the selected corridor's own
            # configuration — never a hardcoded value.
            booking_min = corridor.minimum_booking_charge if corridor else 150.0
            # The routing snapshot is the authoritative LTL total: it already
            # contains the booking-level pallet-volume discount and the
            # minimum-charge floor. Never recompute from raw leg prices —
            # that would silently drop the discount.
            pricing = route.routing_snapshot.get("pricing") or {}
            final_price = pricing.get("final_transportation")
            if final_price is None:
                final_price = route_total if is_ftl else max(route_total, booking_min)
            # FTL multi-stop: replace the internal per-leg lines with the
            # frozen customer breakdown — "Dedicated FTL Transportation"
            # (base, furthest-served-destination rule) + one line per
            # surcharged stop. Built ONLY from the server-side
            # ftl_multistop snapshot (never recalculated here); the lines
            # sum to final_price by construction.
            ftl_multistop = route.routing_snapshot.get("ftl_multistop") or {}
            if is_ftl and ftl_multistop.get("final_transportation") is not None:
                price_lines = [{
                    "label": "Dedicated FTL Transportation",
                    "distance_km": ftl_multistop.get("base_distance_km") or 0.0,
                    "pallets": normalized_request.physical_pallets or normalized_request.pallets,
                    "rate_per_km": ftl_multistop.get("base_rate_per_km") or 0.0,
                    "pallet_rate_per_km": 0.0,
                    "amount": round(ftl_multistop["base_price"], 2),
                    "departure_date": first_leg.departure_date if first_leg else None,
                    "base_leg_charge": round(ftl_multistop["base_price"], 2),
                }]
                for fee in ftl_multistop.get("per_stop") or []:
                    fee_type = fee.get("fee_type")
                    if fee_type not in ("regional", "same_region"):
                        continue
                    city = fee.get("city") or fee.get("stop_key") or "Delivery"
                    prefix = (
                        "Additional Regional Delivery — "
                        if fee_type == "regional"
                        else "Additional Same-Region Delivery — ")
                    price_lines.append({
                        "label": prefix + city,
                        "distance_km": 0, "pallets": 0,
                        "rate_per_km": 0.0, "pallet_rate_per_km": 0.0,
                        "amount": round(fee.get("amount") or 0.0, 2),
                        "departure_date": None,
                    })
                route_total = round(ftl_multistop["final_transportation"], 2)
            elif is_ftl:
                # Single-stop FTL (or FTL with no multi-stop breakdown):
                # freeze ONE customer-facing line at the authoritative
                # server-computed FTL price — the same value that becomes
                # calculated_price — so Step 3 renders a Dedicated FTL row
                # that reconciles EXACTLY (650.00 = 650.00). The FTL flat
                # rate is never recalculated here; the internal LTL-flavored
                # leg formula must not leak into a customer FTL quote.
                price_lines = [{
                    "label": "Dedicated FTL Transportation",
                    "distance_km": first_leg.estimated_distance_km if first_leg else 0.0,
                    "pallets": normalized_request.physical_pallets or normalized_request.pallets,
                    "rate_per_km": 0.0,
                    "pallet_rate_per_km": 0.0,
                    "amount": round(final_price, 2),
                    "departure_date": first_leg.departure_date if first_leg else None,
                    "base_leg_charge": round(final_price, 2),
                }]
                route_total = round(final_price, 2)
            # Authoritative ROUTE subtotal BEFORE the corridor-configured
            # additional stop/pickup charges below — the number the
            # milk-run per-stop cost allocations must sum to EXACTLY.
            route_subtotal = final_price
            if not is_ftl and pricing.get("volume_discount_pct"):
                price_lines.append({
                    "label": "Volume discount (%g%%)" % pricing["volume_discount_pct"],
                    "distance_km": 0, "pallets": 0,
                    "rate_per_km": 0, "pallet_rate_per_km": 0,
                    "amount": round(pricing.get("volume_discount_amount", 0.0) or 0.0, 2),
                    "departure_date": None,
                })
            elif not is_ftl and route_total < booking_min:
                price_lines.append({
                    "label": "Minimum booking adjustment",
                    "distance_km": 0, "pallets": 0,
                    "rate_per_km": 0, "pallet_rate_per_km": 0,
                    "amount": round(final_price - route_total, 2),
                    "departure_date": None,
                })

            # LTL additional-stop charge (same-city extra stops, corridor-
            # configured). Applied after the volume discount and before the
            # booking-minimum floor; never for FTL.
            additional_stop_count, additional_stop_rate, additional_stop_total = (
                0, 0.0, 0.0,
            )
            if not is_ftl:
                additional_stop_count, additional_stop_rate, additional_stop_total = (
                    self._ltl_additional_stop_charge(normalized_request, corridor)
                )
                if additional_stop_total:
                    final_price = final_price + additional_stop_total
                    final_price = max(final_price, booking_min)
                    price_lines.append({
                        "label": "Additional Stop (%d × $%.2f)" % (
                            additional_stop_count, additional_stop_rate,
                        ),
                        "distance_km": 0, "pallets": 0,
                        "rate_per_km": 0, "pallet_rate_per_km": 0,
                        "amount": additional_stop_total,
                        "departure_date": None,
                    })

            # Generalized milk-run: additional PICKUP stops are their own
            # corridor-configured charge (one per stop beyond the first).
            additional_pickup_count, additional_pickup_rate, additional_pickup_total = (
                0, 0.0, 0.0,
            )
            if not is_ftl and normalized_request.route_stops:
                additional_pickup_count, additional_pickup_rate, additional_pickup_total = (
                    self._ltl_additional_pickup_charge(normalized_request, corridor)
                )
                if additional_pickup_total:
                    final_price = final_price + additional_pickup_total
                    final_price = max(final_price, booking_min)
                    price_lines.append({
                        "label": "Additional Pickup (%d × $%.2f)" % (
                            additional_pickup_count, additional_pickup_rate,
                        ),
                        "distance_km": 0, "pallets": 0,
                        "rate_per_km": 0, "pallet_rate_per_km": 0,
                        "amount": additional_pickup_total,
                        "departure_date": None,
                    })

            # Explanatory per-stop cost allocations for movement_v1 milk
            # runs: each served delivery gets a share of the authoritative
            # route subtotal weighted by billable distance × pallets,
            # rounded to cents so the allocations sum EXACTLY to the
            # subtotal (never the raw, pre-discount leg total).
            stop_cost_allocations = []
            milk_run = route.routing_snapshot.get("milk_run") or {}
            if route_stops_for_route and milk_run and not is_ftl:
                stop_cost_allocations = self._milk_run_stop_cost_allocations(
                    route_stops_for_route, milk_run, route_subtotal)
                if milk_run.get("manual_review_required"):
                    _logger.warning(
                        "prepare_quote MILK-RUN MANUAL REVIEW: source=%s "
                        "reasons=%s",
                        normalized_request.source_reference,
                        milk_run.get("manual_review_reasons"))

            # Snapshot for the session carries the additional-stop fields
            # and the final transportation including the charge.
            route_snapshot_for_session = dict(route.routing_snapshot)
            if stop_cost_allocations:
                route_snapshot_for_session.setdefault("milk_run", {})[
                    "stop_allocations"] = stop_cost_allocations
            session_pricing = dict(route_snapshot_for_session.get("pricing") or {})
            session_pricing.update({
                "additional_stop_count": additional_stop_count,
                "additional_stop_rate": additional_stop_rate,
                "additional_stop_total": additional_stop_total,
                "additional_pickup_count": additional_pickup_count,
                "additional_pickup_rate": additional_pickup_rate,
                "additional_pickup_total": additional_pickup_total,
                "final_transportation": round(final_price, 2),
            })
            route_snapshot_for_session["pricing"] = session_pricing

            # Frozen departure capacity for the customer quote page: the
            # canonical VehicleCapacityService answer for the exact truck
            # and departure this quote is reserved on.
            capacity_info = {}
            try:
                frozen_departure = (
                    self.env["logistics.corridor.departure"].sudo().browse(
                        first_leg.departure_id)
                    if first_leg and first_leg.departure_id else False
                )
            except Exception:
                frozen_departure = False
            if frozen_departure and frozen_departure.vehicle_id:
                from .vehicle_capacity_service import VehicleCapacityService
                capacity_result = VehicleCapacityService(self.env).evaluate(
                    frozen_departure.vehicle_id, frozen_departure, 0,
                )
                capacity_info = {
                    "vehicle_name": frozen_departure.vehicle_id.name or "",
                    "layout_code": (capacity_result["selected_layout"] or {}).get("code", ""),
                    "layout_name": (capacity_result["selected_layout"] or {}).get("name", ""),
                    "max_pallets": capacity_result["maximum_capacity"],
                    "reserved_pallets": capacity_result["reserved_pallets"],
                    "remaining_pallets": capacity_result["remaining_pallets"],
                }
            route_snapshot_for_session["capacity"] = capacity_info

            # Create pricing session
            Session = self.env["logistics.pricing.session"].sudo()
            # Extract saved location IDs from normalized request
            pu_saved_id = None
            de_saved_id = None
            if normalized_request.pickup_stops:
                pu_saved_id = normalized_request.pickup_stops[0].get("saved_location_id")
            if normalized_request.delivery_stops:
                if normalized_request.route_stops:
                    # Generalized milk-run: the session's canonical
                    # delivery anchor is the LAST delivery in route order —
                    # the same stop whose FSA defines delivery_fsa_id.
                    # (The first delivery would break the confirm-time
                    # postal re-check against the last stop's FSA.)
                    de_saved_id = normalized_request.delivery_stops[-1].get("saved_location_id")
                else:
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
                "weight_lbs": normalized_request.weight_lbs,  # physical weight, NOT sum of per-stop
                "liftgate_pickup": normalized_request.liftgate_pickup,
                "liftgate_delivery": normalized_request.liftgate_delivery,
                "appointment": normalized_request.appointment,
                "residential": normalized_request.residential,
                "same_day_requested": normalized_request.same_day_requested,
                "pickup_date": first_leg.departure_date if first_leg else None,
                # The calendar-selected departure — the quote binds to this
                # exact scheduled departure; confirmation re-validates it.
                "departure_id": first_leg.departure_id if first_leg else None,
                "delivery_date_estimate": est_delivery,
                "calculated_price": final_price,
                "price_snapshot": price_lines + [
                    {"_pallet_allocs": normalized_request.pallet_allocations},
                ] + ([
                    {"_pallet_movements": normalized_request.pallet_movements},
                ] if normalized_request.route_model_version == "movement_v1" else []) + ([
                    {"_stop_cost_allocations": stop_cost_allocations},
                ] if stop_cost_allocations else []),
                "route_snapshot": route_snapshot_for_session,
                "pickup_saved_location_id": pu_saved_id,
                "delivery_saved_location_id": de_saved_id,
                "expires_at": fields.Datetime.now() + datetime.timedelta(minutes=session_ttl_minutes),
            })

            # Create per-stop records for multi-stop bookings. Generalized
            # movement_v1 requests carry the FULL ordered route stop list
            # (pickups + deliveries) with stable stop keys and per-stop
            # requirements; legacy requests keep the delivery-only loop.
            StopModel = self.env["logistics.pricing.session.stop"].sudo()
            if normalized_request.route_stops:
                from ..services.itinerary_planner import snapshot_saved_location_hours
                for position, rs in enumerate(normalized_request.route_stops):
                    sl_id = rs.get("saved_location_id")
                    sl = self.env["logistics.saved.location"].browse(sl_id) if sl_id else None
                    stop_type = rs.get("stop_type") or "delivery"
                    StopModel.create({
                        "session_id": session.id,
                        "sequence": (position + 1) * 10,
                        "stop_key": rs.get("stop_key") or "",
                        "stop_type": stop_type,
                        "saved_location_id": sl_id or False,
                        "location_name": rs.get("location_name")
                            or (sl.name if sl else rs.get("city", "")),
                        "street": rs.get("street") or (sl.street if sl else ""),
                        "city": rs.get("city") or (sl.city if sl else ""),
                        "state_code": rs.get("state_code") or (sl.state_id.code if sl and sl.state_id else ""),
                        "postal_code": rs.get("postal_code") or (sl.postal_code if sl else ""),
                        "latitude": rs.get("latitude", sl.latitude if sl else 0.0),
                        "longitude": rs.get("longitude", sl.longitude if sl else 0.0),
                        "pallets": rs.get("pallets", 1),
                        "weight_lbs": rs.get("weight_lbs", 500),
                        "shared_pallet": rs.get("shared_pallet", False),
                        "liftgate_required": rs.get("liftgate_required", False),
                        "dock_available": rs.get("dock_available", False),
                        "appointment_required": rs.get("appointment_required", False),
                        "timing_type": rs.get("timing_type", "flexible"),
                        "window_start": rs.get("window_start") or False,
                        "window_end": rs.get("window_end") or False,
                        "appointment_time": rs.get("appointment_time") or False,
                        "service_time_minutes": rs.get("service_time_minutes") or 15,
                        "operating_hours_snapshot": snapshot_saved_location_hours(
                            self.env, sl, stop_type,
                        ),
                        "timezone": rs.get("timezone") or (sl.timezone if sl else "") or "America/Toronto",
                        "instructions": rs.get("instructions", ""),
                        # Legacy compat fields
                        "liftgate_delivery": rs.get("liftgate_required", False),
                        "appointment": rs.get("appointment_required", False),
                    })
            else:
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

            # Real service timing for the quote page — same chain as the
            # calendar (corridor stop config + travel-calc fallback).
            def _iso_part(value, mode):
                if not value:
                    return ""
                if mode == "date":
                    return str(value)[:10]
                try:
                    return datetime.datetime.fromisoformat(str(value)).strftime("%-I:%M %p")
                except ValueError:
                    return ""

            return {
                "quote_token": session.token,
                "pickup_date": first_leg.departure_date if first_leg else None,
                "pickup_time": _iso_part(first_leg.pickup_datetime, "time") if first_leg else "",
                "delivery_date": est_delivery,
                "delivery_time": _iso_part(last_leg.delivery_datetime, "time") if last_leg else "",
                "corridor_id": first_leg.corridor_id if first_leg else None,
                "corridor_name": first_leg.corridor_name if first_leg else "",
                "transfer_hub_name": (
                    self.env["logistics.hub"].sudo().browse(
                        first_leg.transfer_hub_id).public_name
                    if first_leg and first_leg.transfer_hub_id else ""
                ),
                "departure_id": first_leg.departure_id if first_leg else None,
                "corridor_departure_date": first_leg.departure_date if first_leg else None,
                "corridor_departure_time": _iso_part(
                    first_leg.corridor_departure_datetime, "time") if first_leg else "",
                "calculated_price": final_price,
                "price_lines": price_lines,
                "lane_name": corridor.name if corridor else "Hub Transfer",
                "service_offering_name": "Scheduled LTL" if len(route.legs) == 1 else f"Hub Transfer ({len(route.legs)} legs)",
                "expires_at": session.expires_at.isoformat() if session.expires_at else None,
                "legs": len(route.legs),
                "capacity": capacity_info,
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

        # LTL additional-stop charge (same-city extra stops, corridor-
        # configured). Applied after the engine total; never for FTL.
        additional_stop_count, additional_stop_rate, additional_stop_total = (
            self._ltl_additional_stop_charge(normalized_request, result.corridor)
        )
        calculated_price = result.calculated_price
        price_lines = list(result.price_lines)
        route_snapshot_for_session = dict(result.route_snapshot or {})
        session_pricing = dict(route_snapshot_for_session.get("pricing") or {})
        if additional_stop_total:
            calculated_price += additional_stop_total
            price_lines.append({
                "label": "Additional Stop (%d × $%.2f)" % (
                    additional_stop_count, additional_stop_rate,
                ),
                "amount": additional_stop_total,
            })
        session_pricing.update({
            "additional_stop_count": additional_stop_count,
            "additional_stop_rate": additional_stop_rate,
            "additional_stop_total": additional_stop_total,
            "final_transportation": round(calculated_price, 2),
        })
        route_snapshot_for_session["pricing"] = session_pricing

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
            "weight_lbs": normalized_request.weight_lbs,
            "liftgate_pickup": normalized_request.liftgate_pickup,
            "liftgate_delivery": normalized_request.liftgate_delivery,
            "appointment": normalized_request.appointment,
            "residential": normalized_request.residential,
            "same_day_requested": normalized_request.same_day_requested,
            "pickup_date": result.pickup_date,
            "delivery_date_estimate": result.delivery_date_estimate,
            "calculated_price": calculated_price,
            "price_snapshot": price_lines + [{"_pallet_allocs": normalized_request.pallet_allocations}],
            "route_snapshot": route_snapshot_for_session,
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
            "calculated_price": calculated_price,
            "price_lines": price_lines,
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
            # Explicit architecture discriminator, frozen at creation.
            # Default is legacy; only requests built by the generalized
            # movement-aware flow carry movement_v1 (never inferred from
            # pallet rows or allocations).
            "route_model_version": normalized_request.route_model_version or "legacy",
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

                # movement_v1: canonical physical pallet rows, created ONLY
                # for explicitly movement-architected bookings. Legacy
                # bookings never get operational movement rows from this
                # path (migration backfill rows are compatibility-only).
                if booking.route_model_version == "movement_v1":
                    self._create_booking_pallets(
                        booking, normalized_request.pallet_movements,
                    )

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

    @staticmethod
    def _stop_saved_ids(env, stop_dict):
        """Resolve the canonical saved-location FK pair for one stop dict.

        Two conventions exist in the wild and BOTH must work here:
          - the portal / quote flow carries logistics.saved.location ids in
            saved_location_id (the session's FK is that table);
          - internal channels (recurring agreements, import wizards) carry
            prema.dispatch.location master-facility ids.
        Booking stops store BOTH ids — dispatch facility (operational
        anchor, saved_location_id FK) and customer logistics location —
        so the missing side is resolved instead of trusting either
        convention blindly. Mirrors the translation confirm_from_session
        (logistics_booking.py) performs for the portal path.

        Returns (dispatch_location_id, logistics_saved_location_id).
        """
        LogisticsLoc = env["logistics.saved.location"]
        dispatch_id = stop_dict.get("saved_location_id") or False
        logistics_id = stop_dict.get("logistics_saved_location_id") or False

        if logistics_id:
            sl = LogisticsLoc.browse(logistics_id)
            if sl.exists():
                if sl.dispatch_location_id:
                    dispatch_id = sl.dispatch_location_id.id
                elif not dispatch_id:
                    dispatch_id = False
                logistics_id = sl.id
            else:
                logistics_id = False
        elif dispatch_id:
            sl = LogisticsLoc.browse(dispatch_id)
            if sl.exists():
                # Portal convention: saved_location_id IS the logistics id.
                logistics_id = sl.id
                dispatch_id = sl.dispatch_location_id.id if sl.dispatch_location_id else False
            else:
                # Internal convention: saved_location_id is a dispatch id.
                sl = LogisticsLoc.search(
                    [("dispatch_location_id", "=", dispatch_id)], limit=1,
                )
                logistics_id = sl.id if sl else False
        return dispatch_id, logistics_id

    def _create_booking_stops(self, booking, pickup_stops, delivery_stops):
        """Create real booking stops from raw pickup/delivery stop dicts.
        THE single stop-creation path for every channel — no channel may
        fabricate a placeholder "Pickup Location"/"Delivery Location" stop."""
        Stop = self.env["logistics.booking.stop"].sudo()
        seq = 10

        for pu in pickup_stops:
            dispatch_id, logistics_id = self._stop_saved_ids(self.env, pu)
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "pickup",
                "stop_key": pu.get("stop_key") or "",
                "saved_location_id": dispatch_id,
                "logistics_saved_location_id": logistics_id,
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
                "dock_available": pu.get("dock_available", False),
                "appointment_required": pu.get("appointment_required", False),
                "instructions": pu.get("instructions", ""),
                "reference": pu.get("reference", ""),
                "timing_type": pu.get("timing_type", "flexible"),
                "window_start": pu.get("window_start") or False,
                "window_end": pu.get("window_end") or False,
                "appointment_time": pu.get("appointment_time") or False,
                "hard_deadline": pu.get("hard_deadline") or False,
                "service_time_minutes": pu.get("service_time_minutes") or 15,
                "operating_hours_snapshot": pu.get("operating_hours_snapshot") or False,
                "timezone": pu.get("timezone") or "",
            })
            seq += 10

        for dl in delivery_stops:
            dispatch_id, logistics_id = self._stop_saved_ids(self.env, dl)
            Stop.create({
                "booking_id": booking.id,
                "sequence": seq,
                "stop_type": "delivery",
                "stop_key": dl.get("stop_key") or "",
                "saved_location_id": dispatch_id,
                "logistics_saved_location_id": logistics_id,
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
                "dock_available": dl.get("dock_available", False),
                "appointment_required": dl.get("appointment_required", False),
                "instructions": dl.get("instructions", ""),
                "reference": dl.get("reference", ""),
                "timing_type": dl.get("timing_type", "flexible"),
                "window_start": dl.get("window_start") or False,
                "window_end": dl.get("window_end") or False,
                "appointment_time": dl.get("appointment_time") or False,
                "hard_deadline": dl.get("hard_deadline") or False,
                "service_time_minutes": dl.get("service_time_minutes") or 15,
                "operating_hours_snapshot": dl.get("operating_hours_snapshot") or False,
                "timezone": dl.get("timezone") or "",
            })
            seq += 10

    def _create_booking_pallets(self, booking, pallet_movements):
        """Create canonical physical pallet rows for movement_v1 bookings.

        pallet_movements: list of dicts with stable stop keys
        {key, label, weight_lbs, shared, pickup_stop_key,
        delivery_stop_keys, delivery_weights, delivery_pieces,
        commodity, temperature_notes, reference}.
        Stop keys resolve against the booking stops created in the same
        transaction — never positional indices.

        delivery_weights/delivery_pieces: per-delivery PORTION lists
        aligned with delivery_stop_keys (a shared pallet's weight is split
        across its stops). When absent, _auto_split_portions() fills them:
        single-delivery pallets take the whole weight, shared pallets
        split evenly.
        """
        Pallet = self.env["logistics.booking.pallet"].sudo()
        # Build the allocations under portion_batch: the sum-to-pallet
        # constraint cannot judge a pallet while its allocations are still
        # being created one at a time — the completed pallet is validated
        # explicitly at the end of the batch.
        Allocation = self.env[
            "logistics.booking.pallet.stop.allocation"
        ].sudo().with_context(portion_batch=True)
        stops_by_key = {
            stop.stop_key: stop
            for stop in booking.stop_ids
            if stop.stop_key
        }
        sequence = 10
        for movement in pallet_movements:
            pickup_key = movement.get("pickup_stop_key")
            pickup_stop = stops_by_key.get(pickup_key) if pickup_key else booking.stop_ids.filtered(
                lambda s: s.stop_type == "pickup")[:1]
            if not pickup_stop:
                raise UserError(_(
                    "Pallet movement %s references unknown pickup stop %s."
                ) % (movement.get("key", "?"), pickup_key or "(none)"))
            pallet = Pallet.create({
                "booking_id": booking.id,
                "sequence": sequence,
                "label": movement.get("label") or "",
                "weight_lbs": movement.get("weight_lbs") or 0.0,
                "commodity": movement.get("commodity") or "",
                "temperature_notes": movement.get("temperature_notes") or "",
                "reference": movement.get("reference") or "",
                "shared": bool(movement.get("shared")),
                "pickup_stop_id": pickup_stop.id,
                "state": "pending_pickup",
            })
            sequence += 10
            weights = movement.get("delivery_weights") or []
            pieces = movement.get("delivery_pieces") or []
            for unload_index, delivery_key in enumerate(movement.get("delivery_stop_keys") or []):
                delivery_stop = stops_by_key.get(delivery_key)
                if not delivery_stop:
                    raise UserError(_(
                        "Pallet movement %s references unknown delivery stop %s."
                    ) % (movement.get("key", "?"), delivery_key))
                Allocation.create({
                    "pallet_id": pallet.id,
                    "delivery_stop_id": delivery_stop.id,
                    "unload_sequence": (unload_index + 1) * 10,
                    "weight_lbs": float(weights[unload_index] or 0.0)
                    if unload_index < len(weights) else 0.0,
                    "piece_count": int(pieces[unload_index] or 0)
                    if unload_index < len(pieces) else 0,
                })
        # Pallets without entered portions get the default split (whole
        # weight for single delivery, even split for shared) — then the
        # completed pallets are validated so the sum-to-pallet invariant
        # always holds for new bookings.
        pallets = self.env["logistics.booking.pallet"].sudo().search(
            [("booking_id", "=", booking.id)])
        pallets._auto_split_portions()
        pallets._validate_portion_sum()

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
                                       allow_pinwheel_override=False, service_type="ltl"):
        """Sort + SELECT...FOR UPDATE every departure, then revalidate vehicle
        assignment, temperature compatibility, and pallet/weight capacity
        INSIDE the lock. Returns {departure_id: fleet.vehicle} on success;
        raises UserError on any violation (nothing is written yet).

        service_type: 'ftl' (Full Truckload / Dedicated / Exclusive) needs
        the ENTIRE vehicle — the departure must be completely free. 'ltl'
        (the default, and the service type of every threshold-priced load)
        reserves physical positions only and can never join an exclusively
        held truck. A corridor's FTL PRICING threshold (enable_ftl +
        ftl_threshold_pallets + auto_price) never changes service_type —
        pricing mode is not service type, and pricing never auto-reserves
        the truck."""
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

            # Canonical dynamic capacity: the assigned vehicle's active
            # pallet layouts decide fit — pinwheel (or any configured
            # layout) activates automatically, nothing hardcoded.
            from .vehicle_capacity_service import VehicleCapacityService
            capacity = VehicleCapacityService(self.env)
            payload = vehicle.x_max_payload_lbs or 0.0
            if capacity.maximum_capacity(vehicle) <= 0 or not payload:
                raise UserError(_(
                    "Departure %s's vehicle capacity is not configured and cannot be booked."
                ) % dep.name)

            peak = engine.compute_departure_peak(dep)
            # Exclusivity gate — FTL/Dedicated/Exclusive owns the whole
            # vehicle; LTL can never join a held truck.
            if service_type == "ftl":
                if peak["peak_pallets"] or peak["exclusive_vehicle_reserved"]:
                    raise UserError(_(
                        "Full Truckload / dedicated moves require the ENTIRE "
                        "vehicle — %(dep)s already has bookings on it. Choose "
                        "a free departure.",
                        dep=dep.name,
                    ))
            elif peak["exclusive_vehicle_reserved"]:
                raise UserError(_(
                    "%(dep)s's truck is exclusively reserved for a Full "
                    "Truckload / dedicated shipment.",
                    dep=dep.name,
                ))

            projected_pallets = peak["peak_pallets"] + pallets
            projected_weight = peak["peak_weight"] + weight_lbs
            remaining = capacity.maximum_capacity(vehicle) - peak["peak_pallets"]

            if projected_weight > payload:
                raise UserError(_(
                    "Weight capacity exceeded on %(dep)s: %(cur).0f lb + %(new).0f lb > %(max).0f lb.",
                    dep=dep.name, cur=peak["peak_weight"], new=weight_lbs, max=payload,
                ))
            valid, _layout = capacity.select_layout(vehicle, projected_pallets)
            if not valid:
                raise UserError(_(
                    "Only %(remaining)s pallet position(s) remain on the "
                    "selected departure. Please reduce the pallet quantity "
                    "or choose another departure.",
                    remaining=max(remaining, 0),
                ))

            vehicles_by_departure[did] = vehicle

        return vehicles_by_departure

    def _create_legs_from_snapshot(self, booking, snap, leg_snaps):
        from ..services.region_resolver import RegionResolver

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
        # Service TYPE decides exclusivity, never pricing mode: an LTL
        # booking that tripped the corridor's FTL pricing threshold stays
        # 'ltl' here and reserves its positions only.
        service_type = (
            "ftl" if (booking.load_type == "ftl" or booking.shipment_type == "ftl")
            else "ltl"
        )
        vehicles_by_departure = self._lock_and_validate_departures(
            departure_ids, equipment, pallets, weight_lbs,
            allow_pinwheel_override=bool(booking.capacity_override),
            service_type=service_type,
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

        region_bridge = RegionResolver(self.env)
        created_legs = self.env["logistics.booking.leg"]
        hub_stop_by_hub_id = {}
        for i, ls in enumerate(leg_snaps):
            is_first, is_last = (i == 0), (i == len(leg_snaps) - 1)
            origin = pickup_stop if is_first else None
            dest = delivery_stop if is_last else None

            hub_id = ls.get("hub_id") or ls.get("transfer_hub_id")
            # Hub stops only exist for REAL multi-leg transfer topology.
            # A single-leg (direct/final_mile) snapshot carries the hub id
            # as corridor-internal metadata — treating it as a transfer
            # pointed BOTH leg endpoints at the hub placeholder, which then
            # replaced the customer's confirmed pickup facility in dispatch
            # (Booking 185: United Dairy became "994 Westport Crescent,
            # Mississauga"). Never let that happen again.
            if hub_id and len(leg_snaps) > 1:
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
                        # Explicit marker: placeholder, never an
                        # operational stop, never customer-facing.
                        "hub_transfer_stop": True,
                    })
                    hub_stop_by_hub_id[hub_id] = hub_stop
                if is_first:
                    dest = hub_stop
                if is_last:
                    origin = hub_stop

            if not origin or not dest:
                raise UserError(_(
                    "Cannot create leg %(leg)s for booking %(booking)s: "
                    "missing %(which)s stop. The routing snapshot may be incomplete "
                    "or missing a hub/transfer stop."
                ) % {
                    "leg": i + 1,
                    "booking": booking.booking_number or booking.id,
                    "which": "origin" if not origin else "destination",
                })

            frozen_price = ls.get("price", 0.0)
            if currency:
                frozen_price = currency.round(frozen_price)

            # Normalize both endpoints through the region bridge: the
            # snapshot may carry OLD lane regions (FSA-priced channels,
            # codes R1..R15) or NEW official-LTL regions (corridor-routed
            # channels). Legs are stored with canonical (142-159) IDs so
            # corridor stops, direct rules, and legs share one system.
            origin_region = region_bridge.canonical_region(
                ls.get("origin_region_id") or ls.get("origin_region")
            )
            dest_region = region_bridge.canonical_region(
                ls.get("dest_region_id") or ls.get("dest_region")
            )

            # Bridge into lane-based pricing data: attach the old-region
            # lane served by this corridor leg when the snapshot did not
            # freeze one (the corridor↔lane link table was removed, so this
            # is the only path from corridor routing to lanes).
            lane_id = ls.get("lane_id", False)
            if not lane_id and origin_region and dest_region:
                lanes = region_bridge.matching_lanes(origin_region, dest_region)
                if lanes:
                    lane_id = lanes[0].id

            leg = Leg.create({
                "booking_id": booking.id,
                "sequence": (i + 1) * 10,
                "leg_type": "direct" if len(leg_snaps) == 1 else ("feeder" if is_first else "linehaul"),
                "origin_stop_id": origin.id,
                "destination_stop_id": dest.id,
                "departure_id": ls["departure_id"],
                "origin_region_id": origin_region.id if origin_region else False,
                "destination_region_id": dest_region.id if dest_region else False,
                "lane_id": lane_id or False,
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
            service_type=(
                "ftl" if (booking.load_type == "ftl" or booking.shipment_type == "ftl")
                else "ltl"
            ),
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

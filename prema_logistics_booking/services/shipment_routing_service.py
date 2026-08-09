"""Shipment Routing Service — canonical shipment orchestration engine.

Connects all Phase 3-6 components into deterministic routing:
  Address → Region → Direct/Hub → Legs → Corridor → Departure → Distance → Price

Single entry point for all booking channels.
"""

import logging
from collections import namedtuple
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

# ── Result types ────────────────────────────────────────────────────────
ShipmentRoute = namedtuple("ShipmentRoute", [
    "available",            # bool
    "reason",               # str — human-readable
    "reason_code",          # str — machine-readable
    "legs",                 # list of ProposedLeg
    "total_pallets",        # int
    "total_weight_lbs",     # float
    "estimated_delivery",   # str — ISO date or None
    "routing_snapshot",     # dict — full audit trail
])

ProposedLeg = namedtuple("ProposedLeg", [
    "sequence",             # int
    "leg_type",             # str — 'direct','feeder_to_hub','linehaul','final_mile'
    "origin_region_code",   # str
    "dest_region_code",     # str
    "corridor_id",          # int or None
    "corridor_name",        # str
    "departure_id",         # int or None
    "departure_date",       # str
    "estimated_distance_km",# float
    "estimated_drive_hrs",  # float
    "rate_per_km",          # float
    "pallet_rate_per_km",   # float
    "pallets",              # int
    "leg_price",            # float
    "transfer_hub_id",      # int or None
    "hub_ready_at",         # str — ISO datetime or None
])


class ShipmentRoutingService:
    """Orchestrate the full shipment routing pipeline."""

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Public API ───────────────────────────────────────────────────

    def plan_route(self, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                   pallets=1, weight_lbs=0, requested_pickup_date=None,
                   equipment="dry", pickup_country=None, pickup_state=None,
                   delivery_country=None, delivery_state=None):
        """Plan a complete shipment route from pickup to delivery coordinates.

        Returns ShipmentRoute with proposed legs, pricing, and audit trail.
        Does NOT reserve capacity or create database records.
        """
        from ..services.region_resolver import RegionResolver
        from ..services.direct_delivery_service import DirectDeliveryService

        region_resolver = RegionResolver(self.env)
        direct_svc = DirectDeliveryService(self.env)

        timestamp = datetime.utcnow().isoformat() + "Z"
        snapshot = {"timestamp": timestamp, "steps": []}

        # ── Step 1: Resolve pickup region ──────────────────────────
        pickup_result = region_resolver.resolve(
            pickup_lat, pickup_lng,
            country=pickup_country, state=pickup_state,
        )
        snapshot["steps"].append({
            "step": "resolve_pickup",
            "outcome": pickup_result.outcome,
            "region": pickup_result.matched_region_code,
        })

        if pickup_result.outcome == "NETWORK_DISABLED":
            return ShipmentRoute(False, "Pickup region: network disabled.",
                                 "NETWORK_DISABLED", [], pallets, weight_lbs,
                                 None, snapshot)
        if pickup_result.outcome == "MANUAL_QUOTE":
            return ShipmentRoute(False, "Pickup location is outside scheduled corridors.",
                                 "MANUAL_QUOTE_PICKUP", [], pallets, weight_lbs,
                                 None, snapshot)
        if pickup_result.outcome == "AMBIGUOUS":
            return ShipmentRoute(False, "Pickup region is ambiguous — manual review required.",
                                 "AMBIGUOUS_PICKUP", [], pallets, weight_lbs,
                                 None, snapshot)
        if not pickup_result.matched_region:
            return ShipmentRoute(False, "Could not determine pickup region.",
                                 "NO_PICKUP_REGION", [], pallets, weight_lbs,
                                 None, snapshot)

        # ── Step 2: Resolve delivery region ────────────────────────
        delivery_result = region_resolver.resolve(
            delivery_lat, delivery_lng,
            country=delivery_country, state=delivery_state,
        )
        snapshot["steps"].append({
            "step": "resolve_delivery",
            "outcome": delivery_result.outcome,
            "region": delivery_result.matched_region_code,
        })

        if delivery_result.outcome == "NETWORK_DISABLED":
            return ShipmentRoute(False, "Delivery region: network disabled.",
                                 "NETWORK_DISABLED", [], pallets, weight_lbs,
                                 None, snapshot)
        if delivery_result.outcome == "MANUAL_QUOTE":
            return ShipmentRoute(False, "Delivery location is outside scheduled corridors.",
                                 "MANUAL_QUOTE_DELIVERY", [], pallets, weight_lbs,
                                 None, snapshot)
        if delivery_result.outcome == "AMBIGUOUS":
            return ShipmentRoute(False, "Delivery region is ambiguous — manual review required.",
                                 "AMBIGUOUS_DELIVERY", [], pallets, weight_lbs,
                                 None, snapshot)
        if not delivery_result.matched_region:
            return ShipmentRoute(False, "Could not determine delivery region.",
                                 "NO_DELIVERY_REGION", [], pallets, weight_lbs,
                                 None, snapshot)

        origin_region = pickup_result.matched_region
        dest_region = delivery_result.matched_region

        # ── Step 3: Determine pickup day ───────────────────────────
        if requested_pickup_date:
            try:
                pickup_date = datetime.strptime(str(requested_pickup_date)[:10], "%Y-%m-%d")
            except ValueError:
                return ShipmentRoute(False, f"Invalid pickup date: {requested_pickup_date}",
                                     "INVALID_DATE", [], pallets, weight_lbs, None, snapshot)
        else:
            pickup_date = datetime.utcnow() + timedelta(days=1)

        pickup_day = pickup_date.strftime("%A").lower()

        # ── Step 4: Validate pickup day against corridor schedule ──
        valid_day = self._is_valid_pickup_day(origin_region, pickup_day)
        if not valid_day:
            next_day = self._next_valid_pickup_day(origin_region, pickup_date)
            return ShipmentRoute(
                False,
                f"Pickup day '{pickup_day}' is not served for {origin_region.code}. "
                f"Next eligible pickup: {next_day.strftime('%A')} {next_day.strftime('%Y-%m-%d')}.",
                "REQUESTED_PICKUP_DATE_NOT_SERVED", [], pallets, weight_lbs, None,
                {**snapshot, "next_eligible_pickup": next_day.strftime("%Y-%m-%d")},
            )

        # ── Step 5: Direct vs Hub decision ─────────────────────────
        routing = direct_svc.decide(
            origin_region.id, dest_region.id,
            pickup_day=pickup_day,
        )
        snapshot["steps"].append({
            "step": "routing_decision",
            "decision": routing.decision,
            "reason_code": routing.reason_code,
            "matched_rule": routing.matched_rule.id if routing.matched_rule else None,
        })

        if routing.decision == "MANUAL_REVIEW":
            return ShipmentRoute(False, routing.reason_text, routing.reason_code,
                                 [], pallets, weight_lbs, None, snapshot)

        # ── Step 6: Build proposed legs ────────────────────────────
        hub = self._get_default_hub()
        legs = []

        if routing.decision == "DIRECT_ALLOWED":
            # One direct leg
            leg = self._build_leg(
                sequence=1, leg_type="direct",
                origin_region=origin_region, dest_region=dest_region,
                pallets=pallets, weight_lbs=weight_lbs,
                pickup_date=pickup_date, pickup_day=pickup_day,
                equipment=equipment, transfer_hub=None,
                pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                delivery_lat=delivery_lat, delivery_lng=delivery_lng,
            )
            if leg:
                legs.append(leg)

        elif routing.decision == "HUB_TRANSFER_REQUIRED":
            # Check if delivery IS the Hub
            if dest_region.id == (hub.canonical_region_id.id if hub.canonical_region_id else None):
                # One leg: pickup → Hub
                leg = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin_region, dest_region=dest_region,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=None,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                )
                if leg:
                    legs.append(leg)
            else:
                # Leg 1: pickup → Hub
                hub_ready = pickup_date  # simplified — same day arrival
                leg1 = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin_region, dest_region=hub.canonical_region_id,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=hub,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=hub.latitude or 43.589,
                    delivery_lng=hub.longitude or -79.644,
                )
                if leg1:
                    legs.append(leg1)

                # Leg 2: Hub → delivery (next eligible departure)
                leg2_pickup_date = self._next_departure_after(hub_ready, dest_region)
                if leg2_pickup_date:
                    leg2 = self._build_leg(
                        sequence=2, leg_type="final_mile",
                        origin_region=hub.canonical_region_id, dest_region=dest_region,
                        pallets=pallets, weight_lbs=weight_lbs,
                        pickup_date=leg2_pickup_date,
                        pickup_day=leg2_pickup_date.strftime("%A").lower(),
                        equipment=equipment, transfer_hub=hub,
                        pickup_lat=hub.latitude or 43.589,
                        pickup_lng=hub.longitude or -79.644,
                        delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                    )
                    if leg2:
                        legs.append(leg2)

        if not legs:
            return ShipmentRoute(False, "Could not build shipment legs.",
                                 "NO_LEGS", [], pallets, weight_lbs, None, snapshot)

        # ── Step 7: Price legs ─────────────────────────────────────
        total_price = sum(leg.leg_price for leg in legs)
        booking_min = 150.0
        final_price = max(total_price, booking_min)

        # ── Step 8: Estimate delivery ──────────────────────────────
        if legs:
            last_leg = legs[-1]
            est_delivery = last_leg.departure_date
        else:
            est_delivery = None

        snapshot["legs"] = [dict(l._asdict()) for l in legs]
        snapshot["pricing_authority"] = "corridor_per_km"
        snapshot["pricing_version"] = "current"
        snapshot["pricing"] = {
            "leg_total": round(total_price, 2),
            "booking_minimum": booking_min,
            "final_transportation": round(final_price, 2),
        }

        return ShipmentRoute(
            available=True,
            reason=f"Route planned: {len(legs)} leg(s), {pickup_day} pickup.",
            reason_code="ROUTE_PLANNED",
            legs=legs,
            total_pallets=pallets,
            total_weight_lbs=weight_lbs,
            estimated_delivery=est_delivery,
            routing_snapshot=snapshot,
        )

    def get_eligible_pickup_dates(self, pickup_lat, pickup_lng,
                                   delivery_lat, delivery_lng,
                                   pallets=1, weight_lbs=500,
                                   equipment="dry",
                                   horizon_weeks=8):
        """Return eligible pickup dates for a movement within the booking horizon.

        Each date includes routing metadata: feeder corridor, onward corridor,
        earliest departure, and expected delivery date.

        Returns:
            list of dicts with keys: date (YYYY-MM-DD), day_name, feeder_corridor,
            onward_corridor, departure_date, estimated_delivery, leg_count
        """
        from datetime import datetime as dt_module, timedelta
        from ..services.region_resolver import RegionResolver
        from ..services.direct_delivery_service import DirectDeliveryService

        region_resolver = RegionResolver(self.env)
        direct_svc = DirectDeliveryService(self.env)

        # Resolve regions (same as plan_route)
        pickup_result = region_resolver.resolve(pickup_lat, pickup_lng)
        if not pickup_result.matched_region:
            return []
        delivery_result = region_resolver.resolve(delivery_lat, delivery_lng)
        if not delivery_result.matched_region:
            return []

        origin = pickup_result.matched_region
        dest = delivery_result.matched_region
        hub = self._get_default_hub()

        # Check routing decision
        routing = direct_svc.decide(origin.id, dest.id)
        if routing.decision == "MANUAL_REVIEW":
            return []

        eligible = []
        today = dt_module.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        horizon_end = today + timedelta(weeks=horizon_weeks)

        current = today + timedelta(days=1)  # start from tomorrow (same-day needs cutoff check)
        while current <= horizon_end:
            day_name = current.strftime("%A").lower()
            date_str = current.strftime("%Y-%m-%d")

            # Check pickup day is valid for origin region
            if not self._is_valid_pickup_day(origin, day_name):
                current += timedelta(days=1)
                continue

            # Build legs to verify full route feasibility
            legs_info = self._probe_legs(
                origin, dest, hub, routing, current, day_name,
                pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                pallets, weight_lbs, equipment,
            )

            if legs_info and legs_info.get("feasible"):
                eligible.append({
                    "date": date_str,
                    "day_name": day_name.capitalize(),
                    "feeder_corridor": legs_info.get("feeder_corridor", ""),
                    "onward_corridor": legs_info.get("onward_corridor", ""),
                    "departure_date": legs_info.get("departure_date", date_str),
                    "estimated_delivery": legs_info.get("estimated_delivery", ""),
                    "leg_count": legs_info.get("leg_count", 1),
                })

            current += timedelta(days=1)

        return eligible

    def _probe_legs(self, origin, dest, hub, routing, pickup_date, pickup_day,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    pallets, weight_lbs, equipment):
        """Quick-probe leg feasibility for a candidate pickup date. Returns
        dict with corridor/departure info if feasible, or empty dict."""
        result = {}

        if routing.decision == "DIRECT_ALLOWED":
            leg = self._build_leg(
                sequence=1, leg_type="direct",
                origin_region=origin, dest_region=dest,
                pallets=pallets, weight_lbs=weight_lbs,
                pickup_date=pickup_date, pickup_day=pickup_day,
                equipment=equipment, transfer_hub=None,
                pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                delivery_lat=delivery_lat, delivery_lng=delivery_lng,
            )
            if leg and leg.corridor_id:
                result["feasible"] = True
                result["feeder_corridor"] = leg.corridor_name
                result["onward_corridor"] = ""
                result["departure_date"] = leg.departure_date
                result["estimated_delivery"] = leg.departure_date
                result["leg_count"] = 1

        elif routing.decision == "HUB_TRANSFER_REQUIRED":
            if dest.id == (hub.canonical_region_id.id if hub.canonical_region_id else None):
                leg1 = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin, dest_region=dest,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=None,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                )
                if leg1 and leg1.corridor_id:
                    result["feasible"] = True
                    result["feeder_corridor"] = leg1.corridor_name
                    result["departure_date"] = leg1.departure_date
                    result["estimated_delivery"] = leg1.departure_date
                    result["leg_count"] = 1
            else:
                leg1 = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin, dest_region=hub.canonical_region_id,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=hub,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=hub.latitude or 43.589,
                    delivery_lng=hub.longitude or -79.644,
                )
                if not leg1 or not leg1.corridor_id:
                    return {}

                hub_ready = pickup_date
                leg2_pickup_date = self._next_departure_after(hub_ready, dest)
                if not leg2_pickup_date:
                    return {}

                leg2 = self._build_leg(
                    sequence=2, leg_type="final_mile",
                    origin_region=hub.canonical_region_id, dest_region=dest,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=leg2_pickup_date,
                    pickup_day=leg2_pickup_date.strftime("%A").lower(),
                    equipment=equipment, transfer_hub=hub,
                    pickup_lat=hub.latitude or 43.589,
                    pickup_lng=hub.longitude or -79.644,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                )
                if leg2 and leg2.corridor_id:
                    result["feasible"] = True
                    result["feeder_corridor"] = leg1.corridor_name
                    result["onward_corridor"] = leg2.corridor_name
                    result["departure_date"] = leg1.departure_date
                    result["estimated_delivery"] = leg2.departure_date
                    result["leg_count"] = 2

        return result

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_default_hub(self):
        return self.env["logistics.hub"].search([("is_default", "=", True)], limit=1)

    def _is_valid_pickup_day(self, region, day_name):
        """Check if any corridor serving this region operates on the given day."""
        Corridor = self.env["logistics.corridor"]
        Stop = self.env["logistics.corridor.stop"]
        day_field = f"operate_{day_name.lower()}"

        stops = Stop.search([
            ("region_id", "=", region.id),
            ("active", "=", True),
            ("pickup_allowed", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        if not corridor_ids:
            return False

        corridors = Corridor.search([
            ("id", "in", corridor_ids),
            ("active", "=", True),
            (day_field, "=", True),
        ])
        return bool(corridors)

    def _next_valid_pickup_day(self, region, from_date):
        """Find next date when this region is served for pickup."""
        dt = from_date + timedelta(days=1)
        for _ in range(14):  # search 2 weeks
            day = dt.strftime("%A").lower()
            if self._is_valid_pickup_day(region, day):
                return dt
            dt += timedelta(days=1)
        return from_date + timedelta(days=7)  # fallback

    def _next_departure_after(self, after_date, dest_region):
        """Find the next departure serving the destination region."""
        Departure = self.env["logistics.corridor.departure"]
        Stop = self.env["logistics.corridor.stop"]
        Corridor = self.env["logistics.corridor"]

        # Find corridors serving this region for delivery
        stops = Stop.search([
            ("region_id", "=", dest_region.id),
            ("active", "=", True),
            ("delivery_allowed", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        if not corridor_ids:
            return None

        start = after_date.strftime("%Y-%m-%d") if hasattr(after_date, 'strftime') else str(after_date)[:10]
        departures = Departure.search([
            ("corridor_id", "in", corridor_ids),
            ("departure_date", ">=", start),
        ], order="departure_date", limit=1)

        if departures:
            d = departures[0]
            return datetime.strptime(str(d.departure_date)[:10], "%Y-%m-%d")
        return None

    def _build_leg(self, sequence, leg_type, origin_region, dest_region,
                   pallets, weight_lbs, pickup_date, pickup_day, equipment,
                   transfer_hub, pickup_lat, pickup_lng, delivery_lat, delivery_lng):
        """Build a ProposedLeg with corridor, departure, distance, and price."""
        Corridor = self.env["logistics.corridor"]
        Stop = self.env["logistics.corridor.stop"]
        Departure = self.env["logistics.corridor.departure"]

        # Find corridor serving this origin→dest
        day_field = f"operate_{pickup_day.lower()}"
        stops = Stop.search([
            ("region_id", "in", [origin_region.id, dest_region.id]),
            ("active", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        corridor = Corridor.search([
            ("id", "in", corridor_ids),
            ("active", "=", True),
            (day_field, "=", True),
        ], limit=1)

        # Find departure
        date_str = pickup_date.strftime("%Y-%m-%d") if hasattr(pickup_date, 'strftime') else str(pickup_date)[:10]
        departure = Departure.search([
            ("corridor_id", "=", corridor.id if corridor else 0),
            ("departure_date", "=", date_str),
        ], limit=1)

        # Estimate distance (straight-line with road factor)
        import math
        dx = (delivery_lng - pickup_lng) * 111.32 * math.cos(math.radians((pickup_lat + delivery_lat) / 2))
        dy = (delivery_lat - pickup_lat) * 111.32
        est_km = math.sqrt(dx**2 + dy**2) * 1.4  # road factor

        # Pricing
        rate_per_km = corridor.rate_per_km if corridor else 4.0
        planned_pallets = corridor.planned_pallets if corridor else 8
        pallet_rate = rate_per_km / max(planned_pallets, 1)
        leg_price = est_km * pallet_rate * pallets

        return ProposedLeg(
            sequence=sequence,
            leg_type=leg_type,
            origin_region_code=origin_region.code,
            dest_region_code=dest_region.code,
            corridor_id=corridor.id if corridor else None,
            corridor_name=corridor.name if corridor else "Unknown",
            departure_id=departure.id if departure else None,
            departure_date=date_str,
            estimated_distance_km=round(est_km, 1),
            estimated_drive_hrs=round(est_km / 80, 1),
            rate_per_km=rate_per_km,
            pallet_rate_per_km=round(pallet_rate, 4),
            pallets=pallets,
            leg_price=round(leg_price, 2),
            transfer_hub_id=transfer_hub.id if transfer_hub else None,
            hub_ready_at=None,
        )

    def confirm_route(self, booking):
        """Confirm the routing plan into booking.leg records.

        Idempotent: if booking already has confirmed legs, return existing.
        Uses existing BookingOrchestrationService for atomic confirmation.
        """
        from .booking_orchestration_service import BookingOrchestrationService

        existing_legs = self.env["logistics.booking.leg"].search([
            ("booking_id", "=", booking.id),
            ("status", "=", "confirmed"),
        ])
        if existing_legs:
            _logger.info("Booking %s already has %d confirmed legs — idempotent return",
                         booking.id, len(existing_legs))
            return existing_legs

        # Delegate to existing orchestration for atomic booking creation
        orch = BookingOrchestrationService(self.env)
        return orch.confirm_from_internal(booking)

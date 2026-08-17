"""Shipment Routing Service — canonical shipment orchestration engine.

Connects all Phase 3-6 components into deterministic routing:
  Address → Region → Direct/Hub → Legs → Corridor → Departure → Distance → Price

Single entry point for all booking channels.
"""

import logging
from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

_logger = logging.getLogger(__name__)


def _round_half_up(value):
    """Currency-style rounding (Odoo's ROUND_HALF_UP) for booking-level
    adjustments, so $1,332.45 × 0.90 = $1,199.205 → $1,199.21."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

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

    # ── Region normalization ──────────────────────────────────────────

    def _canonical_region(self, region):
        """Normalize any region reference (old lane region 1-20 or new
        official LTL region 142-159) to the canonical official-LTL record
        via RegionResolver. Returns an empty recordset when unresolvable."""
        from ..services.region_resolver import RegionResolver
        return RegionResolver(self.env).canonical_region(region)

    # ── Public API ───────────────────────────────────────────────────

    def plan_route(self, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                   pallets=1, weight_lbs=0, requested_pickup_date=None,
                   equipment="dry", pickup_country=None, pickup_state=None,
                   delivery_country=None, delivery_state=None, shipment_type="ltl"):
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

        # Canonicalize through the region bridge: the polygon resolver
        # returns official-LTL regions (142-159); any old lane region
        # (1-20) handed in by a caller is normalized here too.
        origin_region = region_resolver.canonical_region(pickup_result.matched_region)
        dest_region = region_resolver.canonical_region(delivery_result.matched_region)
        if not origin_region or not dest_region:
            return ShipmentRoute(False, "Could not canonicalize resolved regions.",
                                 "NO_CANONICAL_REGION", [], pallets, weight_lbs,
                                 None, snapshot)

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
            elif origin_region == hub.canonical_region_id:
                # Pickup is already inside the Hub's own region — no phantom
                # feeder leg. One corridor segment: pickup region → delivery.
                leg = self._build_leg(
                    sequence=1, leg_type="final_mile",
                    origin_region=origin_region, dest_region=dest_region,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=hub,
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
        # FTL classification mirrors the pricing engine: the corridor's
        # Enable Full Truckload / FTL Threshold / "When threshold reached"
        # configuration is the sole authority. FTL pricing always calls the
        # corridor's compute_ftl_price() — one source of truth.
        corridor = (
            self.env["logistics.corridor"].browse(legs[0].corridor_id)
            if legs and legs[0].corridor_id else self.env["logistics.corridor"]
        )
        requested_ftl = shipment_type == "ftl"
        threshold_hit = bool(
            corridor and corridor.enable_ftl and corridor.ftl_threshold_pallets
            and pallets >= corridor.ftl_threshold_pallets
        )
        use_ftl = bool(corridor) and corridor.enable_ftl and (
            requested_ftl or (threshold_hit and corridor.ftl_behavior == "auto_price")
        )
        if corridor and corridor.enable_ftl and threshold_hit \
                and corridor.ftl_behavior == "dispatcher_approval" and not requested_ftl:
            return ShipmentRoute(
                False, "FTL threshold reached — dispatcher approval is required.",
                "FTL_DISPATCHER_APPROVAL", [], pallets, weight_lbs, None, snapshot,
            )
        if use_ftl and len(legs) != 1:
            return ShipmentRoute(
                False, "FTL requires a dedicated direct corridor movement.",
                "FTL_REQUIRES_DIRECT", [], pallets, weight_lbs, None, snapshot,
            )
        # Minimum Booking Charge comes from the selected corridor's own
        # configuration — never a hardcoded value.
        booking_min = corridor.minimum_booking_charge if corridor else 150.0
        leg_total_raw = sum(leg.leg_price for leg in legs)
        if use_ftl:
            # Billable distance = the corridor's own segment distance — the
            # same source the pricing engine freezes into booking snapshots —
            # never the full corridor length.
            segment = corridor.resolve_region_segment(origin_region, dest_region)
            distance = segment["distance_km"] if segment else (legs[0].estimated_distance_km or 0.0)
            ftl = corridor.compute_ftl_price(origin_region, dest_region, distance)
            if ftl["pricing_type"] == "flat_rate":
                if not ftl["regional_rule"] or ftl["regional_rule"].flat_rate <= 0:
                    return ShipmentRoute(
                        False, "FTL rate is not configured on this corridor.",
                        "FTL_RATE_NOT_CONFIGURED", [], pallets, weight_lbs, None, snapshot,
                    )
            elif ftl["rate_per_km"] <= 0:
                return ShipmentRoute(
                    False, "FTL rate is not configured on this corridor.",
                    "FTL_RATE_NOT_CONFIGURED", [], pallets, weight_lbs, None, snapshot,
                )
            legs[0] = legs[0]._replace(
                estimated_distance_km=round(distance, 1),
                rate_per_km=round(ftl["rate_per_km"], 4),
                pallet_rate_per_km=0.0,
                leg_price=round(ftl["price"], 2),
            )
            leg_total_raw = legs[0].leg_price
            total_price = leg_total_raw
            final_price = total_price
            snapshot["pricing_mode"] = "ftl"
            snapshot["ftl_pricing"] = {
                "distance_km": round(distance, 1),
                "rate_per_km": ftl["rate_per_km"],
                "distance_price": ftl["distance_price"],
                "pricing_type": ftl["pricing_type"],
                "regional_rule_id": ftl["regional_rule"].id if ftl["regional_rule"] else False,
            }
        else:
            total_price = leg_total_raw
            # Pallet-volume discount: applied ONCE on the booking's LTL
            # freight total, never per leg and never to FTL. The anchor
            # corridor (first leg) owns the tier configuration.
            volume_discount_pct = 0.0
            if corridor and corridor.enable_volume_discounts:
                volume_discount_pct = self.env["logistics.pallet.volume.tier"].get_discount_for_pallets(
                    corridor.id, pallets,
                )
            if volume_discount_pct:
                total_price = _round_half_up(total_price * (100.0 - volume_discount_pct) / 100.0)
                snapshot["volume_discount_pct"] = volume_discount_pct
                snapshot["volume_discount_amount"] = round(total_price - leg_total_raw, 2)
            final_price = max(total_price, booking_min)
            snapshot["pricing_mode"] = "corridor_per_km"

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
            "leg_total": round(leg_total_raw, 2),
            "volume_discount_pct": snapshot.get("volume_discount_pct", 0.0),
            "volume_discount_amount": snapshot.get("volume_discount_amount", 0.0),
            "booking_minimum": 0.0 if use_ftl else booking_min,
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

    # ── Milk-run route pricing (route-level / furthest served point) ──

    _PRICING_BASIS_PARAM = "prema_logistics_booking.pricing_basis"
    _BACKTRACK_TOLERANCE_KM = 25.0

    def _pricing_basis(self):
        """Pricing basis for multi-stop (movement_v1) bookings.

        'route_level_furthest_point' (default, current corridor policy):
        the itinerary prices from the first pickup to the FURTHEST served
        corridor destination — a Brampton → Belleville → Ottawa milk-run
        prices through Ottawa, because the same scheduled truck covers the
        whole corridor at the corridor's per-km rate.

        'segment_pallet_occupancy' is the FUTURE basis (per-segment
        pallet-occupancy pricing); it is NOT implemented yet. Callers must
        surface a manual-review flag instead of silently switching.
        """
        param = self.env["ir.config_parameter"].sudo().get_param(
            self._PRICING_BASIS_PARAM, "route_level_furthest_point")
        return param if param in ("route_level_furthest_point", "segment_pallet_occupancy") \
            else "route_level_furthest_point"

    def plan_milk_run_route(self, stops, pallets=1, weight_lbs=0,
                            requested_pickup_date=None, equipment="dry",
                            shipment_type="ltl"):
        """Price the canonical milk-run itinerary at ROUTE LEVEL.

        Policy (current corridor configuration): the route is priced from
        the FIRST pickup to the FURTHEST downstream corridor destination it
        serves — NOT first pickup → last-entered delivery. Each delivery is
        planned independently against the live corridor config (segment
        distance, rate per km, planned pallets, volume tiers, booking
        minimum); the delivery whose billable distance is greatest becomes
        the canonical route the whole itinerary prices through.

        Live config only — corridor distance, $/km, planned pallets and
        discount are read from the corridor records, never hardcoded.

        stops: ordered route stops as dicts with stop_type, latitude,
        longitude, stop_key, city. The first pickup with coordinates is the
        origin; every delivery with coordinates is evaluated.

        Returns a ShipmentRoute whose routing_snapshot carries a 'milk_run'
        section: basis, furthest stop, per-stop billable distances,
        backtracking / unreachable deliveries and any manual-review flags
        (out-of-corridor delivery, backward order, unimplemented pricing
        basis). Does NOT reserve capacity or create records.
        """
        basis = self._pricing_basis()
        manual_review_reasons = []
        if basis != "route_level_furthest_point":
            manual_review_reasons.append(
                "pricing basis '%s' is configured but not implemented — "
                "route-level (furthest served point) pricing applied; "
                "manual review required." % basis)

        # Coordinates may live on the stop dict itself (client-validated
        # Google pin) OR on the stop's saved location — the portal passes
        # route_stops through with saved_location_id only. Client coords
        # win; the saved location supplements when they are missing.
        SavedLocation = self.env["logistics.saved.location"]

        def _enrich(stop):
            stop = dict(stop)
            if not (stop.get("latitude") and stop.get("longitude")):
                loc_id = stop.get("saved_location_id")
                if loc_id:
                    loc = SavedLocation.browse(int(loc_id))
                    if loc.exists() and loc.latitude and loc.longitude:
                        stop["latitude"] = loc.latitude
                        stop["longitude"] = loc.longitude
            return stop

        stops = [_enrich(s) for s in (stops or [])]

        # Origin: the first pickup with coordinates (movement_v1 guarantees
        # them, but never trust a silent degradation).
        origin = None
        for stop in stops:
            if stop.get("stop_type") == "pickup" and stop.get("latitude") and stop.get("longitude"):
                origin = stop
                break
        if not origin:
            snapshot = {"timestamp": datetime.utcnow().isoformat() + "Z",
                        "steps": [], "milk_run": {"basis": basis}}
            return ShipmentRoute(
                False, "No pickup stop with coordinates on the route.",
                "NO_PICKUP_REGION", [], pallets, weight_lbs, None, snapshot)

        deliveries = [
            s for s in stops
            if s.get("stop_type") == "delivery" and s.get("latitude") and s.get("longitude")
        ]
        if not deliveries:
            snapshot = {"timestamp": datetime.utcnow().isoformat() + "Z",
                        "steps": [], "milk_run": {"basis": basis}}
            return ShipmentRoute(
                False, "No delivery stop with coordinates on the route.",
                "NO_LEGS", [], pallets, weight_lbs, None, snapshot)

        # ── Plan every delivery against the live corridor network ─────
        per_stop = []
        available_routes = []
        failures = []
        for stop in deliveries:
            route = self.plan_route(
                pickup_lat=float(origin["latitude"]),
                pickup_lng=float(origin["longitude"]),
                delivery_lat=float(stop["latitude"]),
                delivery_lng=float(stop["longitude"]),
                pallets=pallets, weight_lbs=weight_lbs,
                requested_pickup_date=requested_pickup_date,
                equipment=equipment, shipment_type=shipment_type,
            )
            billable_km = 0.0
            if route.available:
                billable_km = round(sum(
                    leg.estimated_distance_km or 0.0 for leg in route.legs), 1)
            entry = {
                "stop_key": stop.get("stop_key", ""),
                "city": stop.get("city", ""),
                "outcome": "available" if route.available else route.reason_code,
                "billable_km": billable_km,
                "legs": len(route.legs) if route.available else 0,
            }
            per_stop.append(entry)
            if route.available:
                available_routes.append((route, entry))
            else:
                failures.append(entry)

        # No reachable delivery at all → surface the FIRST failure exactly
        # as plan_route would (friendly reason mapping in prepare_quote).
        if not available_routes:
            if failures:
                stop = deliveries[0]
                first_failure = self.plan_route(
                    pickup_lat=float(origin["latitude"]),
                    pickup_lng=float(origin["longitude"]),
                    delivery_lat=float(stop["latitude"]),
                    delivery_lng=float(stop["longitude"]),
                    pallets=pallets, weight_lbs=weight_lbs,
                    requested_pickup_date=requested_pickup_date,
                    equipment=equipment, shipment_type=shipment_type,
                )
                first_failure.routing_snapshot["milk_run"] = {
                    "basis": basis, "per_stop": per_stop,
                    "manual_review_required": True,
                    "manual_review_reasons": [
                        "no delivery is reachable on scheduled corridors"] +
                        manual_review_reasons,
                }
                return first_failure
            snapshot = {"timestamp": datetime.utcnow().isoformat() + "Z",
                        "steps": [], "milk_run": {"basis": basis}}
            return ShipmentRoute(
                False, "Could not build shipment legs.", "NO_LEGS",
                [], pallets, weight_lbs, None, snapshot)

        # ── Canonical route = the FURTHEST served point ────────────────
        # Ties resolve to the deeper stop in route order (same corridor,
        # later in the itinerary).
        furthest_route, furthest_entry = max(
            available_routes, key=lambda pair: (pair[1]["billable_km"],
                                                pair[0].legs and pair[0].legs[-1].sequence or 0))

        # ── Backtracking / detour detection ────────────────────────────
        # A delivery materially closer to the origin than the farthest
        # billable distance already seen (in route order) is a detour back
        # toward home — priced at route level but flagged for dispatch
        # review (never silently priced as a second route). Same-city
        # repeat stops (a cluster, e.g. two Belleville stops with Ottawa
        # between them in entry order) are NOT detours: the truck already
        # visits that city, and the corridor-configured additional-stop
        # charge covers the extra stop.
        backtracking = []
        seen_cities = set()
        running_max = 0.0
        for entry in per_stop:
            if entry["outcome"] != "available":
                continue
            city = (entry.get("city") or entry.get("stop_key") or "").strip().lower()
            if entry["billable_km"] < running_max - self._BACKTRACK_TOLERANCE_KM \
                    and city not in seen_cities:
                backtracking.append({
                    "stop_key": entry["stop_key"], "city": entry["city"],
                    "billable_km": entry["billable_km"],
                    "vs_furthest_km": furthest_entry["billable_km"],
                })
                manual_review_reasons.append(
                    "delivery '%s' (%s) is served %.0f km short of the furthest "
                    "point after it in route order — backtracking/detour, "
                    "manual review required."
                    % (entry["stop_key"], entry["city"],
                       furthest_entry["billable_km"] - entry["billable_km"]))
            seen_cities.add(city)
            running_max = max(running_max, entry["billable_km"])

        # Deliveries that did not resolve onto a scheduled corridor are a
        # hard flag: the quote prices the reachable network only.
        unreachable = [e for e in per_stop if e["outcome"] != "available"]
        for entry in unreachable:
            manual_review_reasons.append(
                "delivery '%s' (%s) is outside scheduled corridors (%s) — "
                "manual review required." % (
                    entry["stop_key"], entry["city"], entry["outcome"]))

        # ── Merge the canonical route with milk-run metadata ───────────
        snapshot = dict(furthest_route.routing_snapshot)
        snapshot["milk_run"] = {
            "basis": basis,
            "origin": {
                "stop_key": origin.get("stop_key", ""),
                "city": origin.get("city", ""),
            },
            "furthest_stop_key": furthest_entry["stop_key"],
            "furthest_city": furthest_entry["city"],
            "furthest_billable_km": furthest_entry["billable_km"],
            "per_stop": per_stop,
            "backtracking_deliveries": backtracking,
            "unreachable_deliveries": unreachable,
            "manual_review_required": bool(manual_review_reasons),
            "manual_review_reasons": manual_review_reasons,
        }
        pricing = dict(snapshot.get("pricing") or {})
        pricing["basis"] = basis
        snapshot["pricing"] = pricing

        return ShipmentRoute(
            available=True,
            reason=("Milk-run planned: %d delivery(s), priced through %s "
                    "(%s, %.1f km)." % (
                        len(deliveries), furthest_entry["city"],
                        furthest_entry["stop_key"], furthest_entry["billable_km"])),
            reason_code="ROUTE_PLANNED",
            legs=furthest_route.legs,
            total_pallets=pallets,
            total_weight_lbs=weight_lbs,
            estimated_delivery=furthest_route.estimated_delivery,
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

        origin = region_resolver.canonical_region(pickup_result.matched_region)
        dest = region_resolver.canonical_region(delivery_result.matched_region)
        if not origin or not dest:
            return []
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
                # Compute remaining capacity for the departure
                remaining = 0
                max_cap = 13
                dep_date = legs_info.get("departure_date", date_str)
                departure = self.env["logistics.corridor.departure"].sudo().search([
                    ("departure_date", "=", dep_date),
                    ("active", "=", True),
                    ("status", "not in", ("cancelled", "completed")),
                ], limit=1)
                if departure and departure.vehicle_id:
                    cap = departure.vehicle_id.pin_wheel_pallet_capacity or 13
                    max_cap = cap
                    # Sum physical pallets from non-cancelled bookings on this departure
                    active_bookings = self.env["logistics.booking"].sudo().search([
                        ("departure_id", "=", departure.id),
                        ("state", "not in", ("cancelled", "draft")),
                    ])
                    peak = sum(b.physical_pallets or b.pallets for b in active_bookings)
                    remaining = max(0, cap - peak)
                eligible.append({
                    "date": date_str,
                    "day_name": day_name.capitalize(),
                    "feeder_corridor": legs_info.get("feeder_corridor", ""),
                    "onward_corridor": legs_info.get("onward_corridor", ""),
                    "departure_date": legs_info.get("departure_date", date_str),
                    "estimated_delivery": legs_info.get("estimated_delivery", ""),
                    "leg_count": legs_info.get("leg_count", 1),
                    "remaining_capacity": remaining,
                    "max_capacity": max_cap,
                })

            current += timedelta(days=1)

        return eligible

    def _probe_legs(self, origin, dest, hub, routing, pickup_date, pickup_day,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    pallets, weight_lbs, equipment):
        """Quick-probe leg feasibility for a candidate pickup date. Returns
        dict with corridor/departure info if feasible, or empty dict."""
        origin = self._canonical_region(origin)
        dest = self._canonical_region(dest)
        if not origin or not dest:
            return {}
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
            elif origin == hub.canonical_region_id:
                # Pickup is already inside the Hub's own region — no phantom
                # feeder leg; one corridor segment serves the movement.
                leg1 = self._build_leg(
                    sequence=1, leg_type="final_mile",
                    origin_region=origin, dest_region=dest,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=hub,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                )
                if leg1 and leg1.corridor_id:
                    result["feasible"] = True
                    result["feeder_corridor"] = ""
                    result["onward_corridor"] = leg1.corridor_name
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

                # Max custody hold: compute actual hours between pickup and onward departure.
                # Use departure_time (hour float, e.g. 1.0 = 1:00 AM) from the onward departure.
                onward_dep = self.env["logistics.corridor.departure"].sudo().search([
                    ("departure_date", "=", leg2_pickup_date),
                    ("corridor_id.stop_ids.region_id", "=", dest.id),
                    ("active", "=", True),
                    ("status", "not in", ("cancelled", "completed")),
                ], limit=1)
                onward_departure_hour = onward_dep.departure_time if onward_dep else 1.0
                pickup_datetime = datetime.combine(pickup_date, datetime.min.time()) + timedelta(hours=8)  # assume 8AM pickup
                onward_datetime = datetime.combine(leg2_pickup_date, datetime.min.time()) + timedelta(hours=onward_departure_hour)
                hold_hours = (onward_datetime - pickup_datetime).total_seconds() / 3600.0
                if hold_hours > 24:
                    return {}  # reject — max 24h custody hold

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

    def _directionally_compatible(self, corridor, origin_region, dest_region):
        """A direction-ordered corridor may only serve a shipment whose
        origin appears BEFORE its destination in the corridor's ordered
        stop sequence. Same-region movements and corridors that serve both
        directions (bidirectional / round-trip / local) keep the existing
        permissive behavior."""
        if origin_region == dest_region:
            return True
        if corridor.direction in ("bidirectional", "round_trip", "local", "local_loop"):
            return True
        ordered = corridor.stop_ids.filtered(
            lambda stop: stop.active and stop.region_id
        ).sorted("sequence")
        origin_index = next(
            (index for index, stop in enumerate(ordered) if stop.region_id == origin_region),
            None,
        )
        dest_index = next(
            (index for index, stop in enumerate(ordered) if stop.region_id == dest_region),
            None,
        )
        return origin_index is not None and dest_index is not None and origin_index < dest_index

    def _is_valid_pickup_day(self, region, day_name):
        """Check if any corridor serving this region operates on the given day."""
        # Corridor stops are keyed by official-LTL region (142-159);
        # normalize old lane regions (1-20) before searching.
        region = self._canonical_region(region)
        if not region:
            return False
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
        # Corridor stops are keyed by official-LTL region (142-159);
        # normalize old lane regions (1-20) before searching.
        dest_region = self._canonical_region(dest_region)
        if not dest_region:
            return None
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
        from ..services.region_resolver import RegionResolver

        # Normalize both endpoints through the region bridge: corridor
        # stops are keyed by official-LTL regions (142-159) while lanes
        # are keyed by old regions (1-20).
        origin_region = self._canonical_region(origin_region)
        dest_region = self._canonical_region(dest_region)
        if not origin_region or not dest_region:
            return None
        region_bridge = RegionResolver(self.env)
        lanes = region_bridge.matching_lanes(origin_region, dest_region)

        Corridor = self.env["logistics.corridor"]
        Stop = self.env["logistics.corridor.stop"]
        Departure = self.env["logistics.corridor.departure"]

        # Find corridor serving this origin→dest. Direction compatibility is
        # checked BEFORE day availability: a reverse-direction corridor must
        # never be substituted merely because it operates on the requested
        # day.
        day_field = f"operate_{pickup_day.lower()}"
        stops = Stop.search([
            ("region_id", "in", [origin_region.id, dest_region.id]),
            ("active", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        candidates = Corridor.search([
            ("id", "in", corridor_ids),
            ("active", "=", True),
            (day_field, "=", True),
        ], order="id")
        corridor = False
        for candidate in candidates:
            if self._directionally_compatible(candidate, origin_region, dest_region):
                corridor = candidate
                break
        if not corridor:
            # No directionally-compatible corridor operates on this day.
            # Never fall back to a reverse-direction corridor or a
            # corridor-less synthetic leg.
            return None

        # Find departure
        date_str = pickup_date.strftime("%Y-%m-%d") if hasattr(pickup_date, 'strftime') else str(pickup_date)[:10]
        departure = Departure.search([
            ("corridor_id", "=", corridor.id if corridor else 0),
            ("departure_date", "=", date_str),
        ], limit=1)

        # Distance priority: the corridor's canonical ordered segment
        # distance, then the legacy straight-line × 1.4 estimate when the
        # endpoint coordinates are available, then the old-region lane's
        # road distance. Never the full corridor length.
        import math
        segment = corridor.resolve_region_segment(origin_region, dest_region) if corridor else False
        if segment:
            est_km = segment["distance_km"]
        elif pickup_lat and pickup_lng and delivery_lat and delivery_lng:
            dx = (delivery_lng - pickup_lng) * 111.32 * math.cos(math.radians((pickup_lat + delivery_lat) / 2))
            dy = (delivery_lat - pickup_lat) * 111.32
            est_km = math.sqrt(dx**2 + dy**2) * 1.4  # road factor
        elif lanes and lanes[0].road_km:
            est_km = lanes[0].road_km
        else:
            est_km = 0.0

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

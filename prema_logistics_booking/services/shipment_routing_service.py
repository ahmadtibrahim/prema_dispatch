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
    # Real service timing (corridor.stop config with travel-calc fallback):
    "pickup_datetime",          # str — ISO datetime or None
    "delivery_datetime",        # str — ISO datetime or None
    "corridor_departure_datetime",  # str — ISO datetime or None
    "timing_source",            # str — 'configured' | 'corridor_departure_time' | 'travel_calc_fallback'
], defaults=[None, None, None, ""])


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

    # ── ONE eligibility authority (calendar == quote) ─────────────────

    def resolve_route_stops(self, stops):
        """Strict stop resolution shared by the calendar and quote paths.

        A pickup or delivery only counts as serviceable when the
        RegionResolver returns a SCHEDULED_MATCH region — the exact rule
        plan_route (Get Price) enforces. There is deliberately NO postal /
        FSA fallback here: a coordinate outside every service polygon is a
        MANUAL_QUOTE movement, and inventing a region for it is how the
        calendar and the quote drifted apart.

        Returns a dict: serviceable, manual_quote (show the Manual Quote
        Required banner), reason (customer-facing), origin_region,
        delivery_plans, hub.
        """
        from ..services.region_resolver import RegionResolver
        from ..services.direct_delivery_service import DirectDeliveryService

        region_resolver = RegionResolver(self.env)
        direct_svc = DirectDeliveryService(self.env)

        # Enrich coordinates + postal from saved locations (same rule as
        # plan_milk_run_route: client coords win, saved location supplements).
        SavedLocation = self.env["logistics.saved.location"]
        enriched = []
        for stop in stops or []:
            stop = dict(stop)
            loc_id = stop.get("saved_location_id")
            loc = None
            if loc_id:
                loc = SavedLocation.browse(int(loc_id))
                if not loc.exists():
                    loc = None
            if loc:
                if not (stop.get("latitude") and stop.get("longitude")):
                    if loc.latitude and loc.longitude:
                        stop["latitude"] = loc.latitude
                        stop["longitude"] = loc.longitude
                if not stop.get("postal_code"):
                    stop["postal_code"] = loc.postal_code or ""
            enriched.append(stop)
        stops = enriched

        # Origin: the first pickup with coordinates (never a silent
        # degradation to a delivery).
        origin = None
        for stop in stops:
            if stop.get("stop_type") == "pickup" and stop.get("latitude") and stop.get("longitude"):
                origin = stop
                break

        def verdict(serviceable, manual_quote, reason, **extra):
            result = {
                "serviceable": serviceable,
                "manual_quote": manual_quote,
                "reason": reason,
                "origin_region": None,
                "delivery_plans": [],
                "hub": self._get_default_hub(),
                "origin": origin,
            }
            result.update(extra)
            return result

        if not origin:
            return verdict(False, False, "No pickup stop with coordinates on the route.")

        deliveries = [
            s for s in stops
            if s.get("stop_type") == "delivery" and s.get("latitude") and s.get("longitude")
        ]
        if not deliveries:
            return verdict(False, False, "No delivery stop with coordinates on the route.")

        # Pickup: strict SCHEDULED_MATCH only.
        pickup_result = region_resolver.resolve(
            float(origin["latitude"]), float(origin["longitude"]),
        )
        if pickup_result.outcome == "NETWORK_DISABLED":
            return verdict(
                False, True,
                "Scheduled service is not available for this pickup region.",
            )
        if pickup_result.outcome in ("MANUAL_QUOTE", "AMBIGUOUS") or not pickup_result.matched_region:
            return verdict(
                False, True,
                "Manual Quote Required — this pickup is outside the scheduled "
                "booking network.",
            )
        origin_region = region_resolver.canonical_region(pickup_result.matched_region)
        if not origin_region:
            return verdict(
                False, True,
                "Manual Quote Required — this pickup is outside the scheduled "
                "booking network.",
            )

        # Every delivery: strict SCHEDULED_MATCH too. A delivery that
        # cannot be resolved makes the whole route non-serviceable — it
        # must never be silently dropped.
        delivery_plans = []
        for stop in deliveries:
            result = region_resolver.resolve(float(stop["latitude"]), float(stop["longitude"]))
            if result.outcome == "NETWORK_DISABLED":
                return verdict(
                    False, True,
                    "Scheduled service is not available for this delivery region.",
                )
            if result.outcome in ("MANUAL_QUOTE", "AMBIGUOUS") or not result.matched_region:
                return verdict(
                    False, True,
                    "Manual Quote Required — this delivery is outside the "
                    "scheduled booking network.",
                )
            dest = region_resolver.canonical_region(result.matched_region)
            if not dest:
                return verdict(
                    False, True,
                    "Manual Quote Required — this delivery is outside the "
                    "scheduled booking network.",
                )
            routing = direct_svc.decide(origin_region.id, dest.id)
            delivery_plans.append({
                "stop": stop,
                "dest": dest,
                "routing": routing,
            })

        return verdict(
            True, False, "",
            origin_region=origin_region,
            delivery_plans=delivery_plans,
        )

    def calendar_availability(self, stops, physical_pallets=1, weight_lbs=500,
                              equipment="dry", horizon_weeks=8):
        """Canonical calendar verdict for the portal.

        ONE authority: the same strict stop resolution the Get Price path
        runs. When the pickup (or any delivery) is a MANUAL_QUOTE movement
        the response carries manual_quote=True and NO dates — the portal
        renders the Manual Quote Required banner instead of fake
        selectable dates.

        Returns {"manual_quote": bool, "reason": str, "dates": [...]}.
        """
        resolved = self.resolve_route_stops(stops)
        if not resolved["serviceable"]:
            return {"manual_quote": True, "reason": resolved["reason"], "dates": []}
        dates = self.get_eligible_pickup_dates_for_route(
            stops, physical_pallets=physical_pallets, weight_lbs=weight_lbs,
            equipment=equipment, horizon_weeks=horizon_weeks,
        )
        return {"manual_quote": False, "reason": "", "dates": dates}

    # ── Real service timing (corridor stop config + travel-calc fallback) ──

    # Canonical planning speed for the travel-time fallback — the same
    # figure _build_leg uses for estimated_drive_hrs.
    _AVG_TRUCK_SPEED_KPH = 80.0

    def _leg_timings(self, corridor, departure, origin_region, dest_region,
                     distance_km, pickup_date):
        """Real pickup / delivery datetimes for one scheduled corridor leg.

        Authority order:
          1. corridor.stop planned times via resolve_region_segment
             (origin_departure_time / destination_arrival_time, plus the
             configured day offsets)
          2. the departure's own departure_time (= corridor.start_time)
             for the pickup when the origin stop has no planned departure
             time — never a hardcoded clock time
          3. the canonical travel-time calculation (distance / 80 km/h)
             for the delivery when the destination stop has no planned
             arrival time — a clearly identified fallback.

        Returns dict with datetime objects + per-side timing_source.
        """
        base = pickup_date
        if isinstance(base, str):
            base = datetime.strptime(base[:10], "%Y-%m-%d")

        dep_hour = departure.departure_time if departure else 0.0
        if not dep_hour and corridor:
            dep_hour = corridor.start_time or 0.0
        corridor_departure_dt = base + timedelta(hours=dep_hour or 0.0)

        segment = corridor.resolve_region_segment(origin_region, dest_region) if corridor else False
        origin_day = (segment.get("pickup_day_offset") or 0) if segment else 0
        dest_day = (segment.get("delivery_day_offset") or 0) if segment else 0
        origin_hour = segment.get("origin_departure_time") if segment else False
        dest_hour = segment.get("destination_arrival_time") if segment else False

        if origin_hour:
            pickup_dt = base + timedelta(days=origin_day, hours=origin_hour)
            pickup_source = "configured"
        else:
            pickup_dt = corridor_departure_dt + timedelta(days=origin_day)
            pickup_source = "corridor_departure_time"

        if dest_hour:
            delivery_dt = base + timedelta(days=dest_day, hours=dest_hour)
            delivery_source = "configured"
        else:
            km = distance_km or ((segment.get("distance_km") or 0.0) if segment else 0.0) or 0.0
            delivery_dt = pickup_dt + timedelta(hours=km / self._AVG_TRUCK_SPEED_KPH)
            delivery_source = "travel_calc_fallback"

        if delivery_source == "travel_calc_fallback":
            timing_source = "travel_calc_fallback"
        elif pickup_source == "corridor_departure_time":
            timing_source = "corridor_departure_time"
        else:
            timing_source = "configured"

        return {
            "pickup_datetime": pickup_dt,
            "delivery_datetime": delivery_dt,
            "corridor_departure_datetime": corridor_departure_dt,
            "pickup_source": pickup_source,
            "delivery_source": delivery_source,
            "timing_source": timing_source,
        }

    def _iso_dt(self, dt):
        return dt.isoformat() if dt else None

    def _parse_iso_dt(self, iso):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(str(iso))
        except ValueError:
            return None

    def _date_part(self, iso):
        """'2026-08-25T05:00:00' → '2026-08-25' ('' when absent)."""
        dt = self._parse_iso_dt(iso)
        return dt.strftime("%Y-%m-%d") if dt else ""

    def _time_part(self, iso):
        """'2026-08-25T05:00:00' → '5:00 AM' ('' when absent)."""
        dt = self._parse_iso_dt(iso)
        return dt.strftime("%-I:%M %p") if dt else ""

    def _leg_info(self, leg):
        """Compact dict for a built leg — what the calendar needs to bind
        capacity and the quote to the EXACT departure."""
        return {
            "leg_type": leg.leg_type,
            "corridor_id": leg.corridor_id,
            "corridor_name": leg.corridor_name,
            "departure_id": leg.departure_id,
            "departure_date": leg.departure_date,
            "pickup_datetime": leg.pickup_datetime,
            "delivery_datetime": leg.delivery_datetime,
            "corridor_departure_datetime": leg.corridor_departure_datetime,
            "timing_source": leg.timing_source,
        }

    # ── Public API ───────────────────────────────────────────────────

    def plan_route(self, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                   pallets=1, weight_lbs=0, requested_pickup_date=None,
                   equipment="dry", pickup_country=None, pickup_state=None,
                   delivery_country=None, delivery_state=None, shipment_type="ltl",
                   requested_departure_id=None):
        """Plan a complete shipment route from pickup to delivery coordinates.

        requested_departure_id: the calendar-selected pickup departure.
        Server-re-validated inside _build_leg — it must belong to the
        corridor serving this route on the requested date, be active and
        still scheduled; otherwise the route is refused (never silently
        substituted with a different departure).

        Returns ShipmentRoute with proposed legs, pricing, and audit trail.
        Does NOT reserve capacity or create database records.
        """
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
            pickup_date = self._op_today() + timedelta(days=1)

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
                requested_departure_id=requested_departure_id,
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
                    requested_departure_id=requested_departure_id,
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
                    requested_departure_id=requested_departure_id,
                )
                if leg:
                    legs.append(leg)
            else:
                # Leg 1: pickup → Hub
                leg1 = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin_region, dest_region=hub.canonical_region_id,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=hub,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=hub.latitude or 43.589,
                    delivery_lng=hub.longitude or -79.644,
                    requested_departure_id=requested_departure_id,
                )
                if leg1:
                    legs.append(leg1)

                    # Leg 2: Hub → delivery. The onward leg departs on the
                    # next ACTUAL scheduled departure after the feeder's
                    # real arrival at the hub (never a same-day assumption).
                    hub_ready = self._parse_iso_dt(leg1.delivery_datetime) or pickup_date
                    leg2_pickup_date = self._next_departure_after(
                        hub_ready, dest_region, origin_region=hub.canonical_region_id)
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
                            # Max custody hold from REAL configured times —
                            # the actual gap between this leg's pickup
                            # datetime and the onward corridor departure.
                            leg1_pickup_dt = self._parse_iso_dt(leg1.pickup_datetime)
                            leg2_dep_dt = self._parse_iso_dt(leg2.corridor_departure_datetime)
                            if leg1_pickup_dt and leg2_dep_dt:
                                hold_hours = (leg2_dep_dt - leg1_pickup_dt).total_seconds() / 3600.0
                                if hold_hours <= 24:
                                    legs.append(leg2)
                            else:
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
        # The REAL delivery date from the corridor stop configuration
        # (travel-calc fallback when stop times are unset) — never the
        # departure date (a 550 km run is not same-day delivery).
        if legs:
            last_leg = legs[-1]
            est_delivery = self._date_part(last_leg.delivery_datetime) or last_leg.departure_date
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
                            shipment_type="ltl", requested_departure_id=None):
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
                requested_departure_id=requested_departure_id,
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
                    requested_departure_id=requested_departure_id,
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

    # ── Operational timezone (section 3: never UTC calendar dates) ─────

    _OP_TZ_PARAM = "prema_logistics_booking.operational_tz"

    def _op_tz(self):
        """The company's operational timezone. Default America/Toronto;
        overridable via an ir.config_parameter."""
        import pytz
        name = self.env["ir.config_parameter"].sudo().get_param(
            self._OP_TZ_PARAM, "America/Toronto")
        try:
            return pytz.timezone(name)
        except Exception:
            return pytz.timezone("America/Toronto")

    def _op_today(self):
        """Today's calendar date in the OPERATIONAL timezone — the customer's
        and driver's operational date. Never datetime.utcnow() (which flips
        the operational date to tomorrow before midnight locally)."""
        import pytz
        return pytz.utc.localize(datetime.utcnow()).astimezone(self._op_tz()).date()

    def get_eligible_pickup_dates(self, pickup_lat, pickup_lng,
                                   delivery_lat, delivery_lng,
                                   pallets=1, weight_lbs=500,
                                   equipment="dry",
                                   horizon_weeks=8):
        """Eligible pickup dates for a single-pickup / single-delivery
        movement (legacy signature). Delegates to the full-route engine —
        one code path for every booking shape."""
        stops = [
            {"stop_type": "pickup", "latitude": pickup_lat, "longitude": pickup_lng},
            {"stop_type": "delivery", "latitude": delivery_lat, "longitude": delivery_lng},
        ]
        return self.get_eligible_pickup_dates_for_route(
            stops, physical_pallets=pallets, weight_lbs=weight_lbs,
            equipment=equipment, horizon_weeks=horizon_weeks)

    def get_eligible_pickup_dates_for_route(self, stops, physical_pallets=1,
                                            weight_lbs=500, equipment="dry",
                                            horizon_weeks=8):
        """Return eligible pickup dates for a COMPLETE multi-stop route.

        The date is eligible ONLY when the entire shipment can move:
        every delivery stop must resolve onto a directionally-compatible
        corridor that departs on that pickup date with an ACTUAL
        scheduled departure (active, not cancelled/completed, truck
        assigned), every one of those exact departures must pass the
        same vehicle/equipment/payload/capacity rules the quote and
        confirmation paths enforce, and the peak physical pallet count
        (shared pallets count ONCE) must fit the truck together with
        existing reservations.

        Stop resolution is STRICT (resolve_route_stops): a pickup or
        delivery outside every service polygon is MANUAL_QUOTE — the
        calendar must not invent dates for it (the quote already refuses).
        Callers that need the manual-quote marker use calendar_availability.

        Never evaluates just the first delivery. Never hardcodes capacity.

        stops: ordered route stops as dicts with stop_type, latitude,
               longitude, stop_key, city. The first pickup with coordinates
               is the origin; every delivery with coordinates is evaluated.

        Returns:
            list of dicts: date, day_name, feeder_corridor, onward_corridor,
            corridor_id, departure_id, departure_date, estimated_delivery,
            pickup_date/time/datetime, delivery_date/time/datetime,
            corridor_departure_date/time/datetime, timing_source, transfer,
            transfer_hub_name, leg_count, per_stop, remaining_capacity,
            max_capacity, layout_code, layout_name
        """
        from datetime import timedelta
        from ..services.vehicle_capacity_service import VehicleCapacityService
        from ..services.departure_resolver import DepartureResolver
        from ..services.direct_delivery_service import DirectDeliveryService

        # Strict shared stop resolution — the SAME authority the quote
        # path uses. No postal/FSA fallback can invent serviceability.
        resolved = self.resolve_route_stops(stops)
        if not resolved["serviceable"]:
            return []
        origin_region = resolved["origin_region"]
        delivery_plans = resolved["delivery_plans"]
        hub = resolved["hub"]
        origin = resolved["origin"] or {}

        departure_svc = DepartureResolver(self.env)
        Departure = self.env["logistics.corridor.departure"]

        eligible = []
        # "Today" is the OPERATIONAL calendar date (Toronto by default) —
        # never UTC, so the calendar does not flip to tomorrow before
        # midnight locally.
        today = self._op_today()
        horizon_end = today + timedelta(weeks=horizon_weeks)

        current = today + timedelta(days=1)  # start from tomorrow (same-day needs cutoff check)
        while current <= horizon_end:
            day_name = current.strftime("%A").lower()
            date_str = current.strftime("%Y-%m-%d")

            # Pickup day must be served for the origin region.
            if not self._is_valid_pickup_day(origin_region, day_name):
                current += timedelta(days=1)
                continue

            # Evaluate EVERY delivery stop on this pickup date. The date is
            # eligible only when the whole route can move.
            per_stop = []
            route_feasible = True
            feeder_names = []
            onward_names = []
            leg_count = 0
            estimated_delivery = ""
            latest_delivery_iso = ""
            transfer_hub_name = ""
            all_legs = []  # every leg dict across every delivery stop
            direct_svc = DirectDeliveryService(self.env)
            for plan in delivery_plans:
                # Re-decide per pickup DAY — the direct/hub decision can
                # differ by day (rule allowed_service_days), and the quote
                # path decides with the same pickup_day.
                routing = direct_svc.decide(
                    origin_region.id, plan["dest"].id, pickup_day=day_name)
                legs_info = self._probe_legs(
                    origin_region, plan["dest"], hub, routing,
                    current, day_name,
                    float(origin["latitude"]), float(origin["longitude"]),
                    float(plan["stop"]["latitude"]), float(plan["stop"]["longitude"]),
                    physical_pallets, weight_lbs, equipment,
                )
                if not legs_info or not legs_info.get("feasible"):
                    route_feasible = False
                    per_stop.append({
                        "stop_key": plan["stop"].get("stop_key", ""),
                        "city": plan["stop"].get("city", ""),
                        "feasible": False,
                    })
                    continue
                per_stop.append({
                    "stop_key": plan["stop"].get("stop_key", ""),
                    "city": plan["stop"].get("city", ""),
                    "feasible": True,
                    "corridor": legs_info.get("feeder_corridor") or legs_info.get("onward_corridor", ""),
                    "departure_date": legs_info.get("departure_date", date_str),
                    "departure_id": legs_info.get("departure_id"),
                    "corridor_id": legs_info.get("corridor_id"),
                    "delivery_datetime": legs_info.get("delivery_datetime"),
                })
                if legs_info.get("feeder_corridor"):
                    feeder_names.append(legs_info["feeder_corridor"])
                if legs_info.get("onward_corridor"):
                    onward_names.append(legs_info["onward_corridor"])
                leg_count = max(leg_count, legs_info.get("leg_count", 1))
                # The latest expected delivery across the route.
                if legs_info.get("estimated_delivery", "") > estimated_delivery:
                    estimated_delivery = legs_info["estimated_delivery"]
                if legs_info.get("delivery_datetime") and (
                        not latest_delivery_iso
                        or legs_info["delivery_datetime"] > latest_delivery_iso):
                    latest_delivery_iso = legs_info["delivery_datetime"]
                if legs_info.get("transfer_hub_name"):
                    transfer_hub_name = legs_info["transfer_hub_name"]
                all_legs += legs_info.get("legs") or []

            if not route_feasible:
                current += timedelta(days=1)
                continue

            # ── Exact-departure eligibility + capacity ─────────────────
            # Capacity is evaluated against the EXACT departures the route
            # legs selected — never an independently-searched departure for
            # the origin region. Every leg's departure must be eligible
            # (vehicle assigned + operational, equipment-compatible,
            # payload OK, pallets fit) via the same DepartureResolver
            # rules the confirmation path enforces.
            unique_dep_ids = sorted({
                leg["departure_id"] for leg in all_legs if leg.get("departure_id")
            })
            if not unique_dep_ids:
                # _probe_legs already required a real departure per leg;
                # this is a safety net, not a substitute.
                current += timedelta(days=1)
                continue
            all_departures_eligible = True
            for dep_id in unique_dep_ids:
                dep = Departure.sudo().browse(dep_id)
                ok, _reason, _vehicle = departure_svc.evaluate_departure(
                    dep, equipment, physical_pallets, weight_lbs)
                if not ok:
                    all_departures_eligible = False
                    break
            if not all_departures_eligible:
                current += timedelta(days=1)
                continue

            # Capacity display from the PICKUP (first) leg's exact
            # departure — the same numbers the quote page will freeze.
            first_leg = all_legs[0]
            pickup_dep = Departure.sudo().browse(first_leg.get("departure_id"))
            cap = {}
            if pickup_dep and pickup_dep.vehicle_id:
                cap_result = VehicleCapacityService(self.env).evaluate(
                    pickup_dep.vehicle_id, pickup_dep, 0)
                layout = cap_result["selected_layout"] or {}
                cap = {
                    "available": True,
                    "departure_id": pickup_dep.id,
                    "departure_date": pickup_dep.departure_date.isoformat()
                    if pickup_dep.departure_date else date_str,
                    "max_pallets": cap_result["maximum_capacity"],
                    "reserved_pallets": cap_result["reserved_pallets"],
                    "remaining_pallets": cap_result["remaining_pallets"],
                    "remaining_sellable_capacity": cap_result["remaining_sellable_capacity"],
                    "layout_code": layout.get("code", ""),
                    "layout_name": layout.get("name", ""),
                }

            eligible.append({
                "date": date_str,
                "day_name": day_name.capitalize(),
                "feeder_corridor": ", ".join(sorted(set(feeder_names))),
                "onward_corridor": ", ".join(sorted(set(onward_names))),
                # The exact service option this date sells:
                "corridor_id": first_leg.get("corridor_id"),
                "corridor_name": first_leg.get("corridor_name"),
                "departure_id": first_leg.get("departure_id"),
                "departure_date": first_leg.get("departure_date") or date_str,
                "departure_time": self._time_part(first_leg.get("corridor_departure_datetime")),
                # Real service timing (corridor stop config + travel-calc
                # fallback) — estimated_delivery is NEVER the departure date.
                "pickup_date": self._date_part(first_leg.get("pickup_datetime")) or date_str,
                "pickup_time": self._time_part(first_leg.get("pickup_datetime")),
                "pickup_datetime": first_leg.get("pickup_datetime"),
                "delivery_date": self._date_part(latest_delivery_iso) or estimated_delivery,
                "delivery_time": self._time_part(latest_delivery_iso),
                "delivery_datetime": latest_delivery_iso,
                "estimated_delivery": estimated_delivery,
                "corridor_departure_date": first_leg.get("departure_date") or date_str,
                "corridor_departure_time": self._time_part(first_leg.get("corridor_departure_datetime")),
                "corridor_departure_datetime": first_leg.get("corridor_departure_datetime"),
                "timing_source": first_leg.get("timing_source", ""),
                "transfer": bool(leg_count == 2),
                "transfer_hub_name": transfer_hub_name,
                "leg_count": leg_count,
                "per_stop": per_stop,
                "remaining_capacity": cap.get("remaining_pallets", 0),
                "remaining_sellable_capacity": cap.get("remaining_sellable_capacity",
                                                        cap.get("remaining_pallets", 0)),
                "max_capacity": cap.get("max_pallets", 0),
                "layout_code": cap.get("layout_code", ""),
                "layout_name": cap.get("layout_name", ""),
            })

            current += timedelta(days=1)

        return eligible

    def _probe_legs(self, origin, dest, hub, routing, pickup_date, pickup_day,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    pallets, weight_lbs, equipment):
        """Quick-probe leg feasibility for a candidate pickup date. Returns
        dict with corridor/departure info if feasible, or empty dict.

        A leg is feasible ONLY with an actual scheduled departure — the
        corridor's operating-day checkbox alone is never sufficient.
        Every returned leg carries its exact departure_id; the caller
        evaluates capacity against those exact departures."""
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
            if leg and leg.departure_id:
                result.update({
                    "feasible": True,
                    "feeder_corridor": leg.corridor_name,
                    "onward_corridor": "",
                    "departure_date": leg.departure_date,
                    "estimated_delivery": self._date_part(leg.delivery_datetime),
                    "pickup_datetime": leg.pickup_datetime,
                    "delivery_datetime": leg.delivery_datetime,
                    "corridor_departure_datetime": leg.corridor_departure_datetime,
                    "corridor_id": leg.corridor_id,
                    "corridor_name": leg.corridor_name,
                    "departure_id": leg.departure_id,
                    "timing_source": leg.timing_source,
                    "transfer": False,
                    "transfer_hub_name": "",
                    "leg_count": 1,
                    "legs": [self._leg_info(leg)],
                })

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
                if leg1 and leg1.departure_id:
                    result.update({
                        "feasible": True,
                        "feeder_corridor": leg1.corridor_name,
                        "onward_corridor": "",
                        "departure_date": leg1.departure_date,
                        "estimated_delivery": self._date_part(leg1.delivery_datetime),
                        "pickup_datetime": leg1.pickup_datetime,
                        "delivery_datetime": leg1.delivery_datetime,
                        "corridor_departure_datetime": leg1.corridor_departure_datetime,
                        "corridor_id": leg1.corridor_id,
                        "corridor_name": leg1.corridor_name,
                        "departure_id": leg1.departure_id,
                        "timing_source": leg1.timing_source,
                        "transfer": False,
                        "transfer_hub_name": "",
                        "leg_count": 1,
                        "legs": [self._leg_info(leg1)],
                    })
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
                if leg1 and leg1.departure_id:
                    result.update({
                        "feasible": True,
                        "feeder_corridor": "",
                        "onward_corridor": leg1.corridor_name,
                        "departure_date": leg1.departure_date,
                        "estimated_delivery": self._date_part(leg1.delivery_datetime),
                        "pickup_datetime": leg1.pickup_datetime,
                        "delivery_datetime": leg1.delivery_datetime,
                        "corridor_departure_datetime": leg1.corridor_departure_datetime,
                        "corridor_id": leg1.corridor_id,
                        "corridor_name": leg1.corridor_name,
                        "departure_id": leg1.departure_id,
                        "timing_source": leg1.timing_source,
                        "transfer": False,
                        "transfer_hub_name": "",
                        "leg_count": 1,
                        "legs": [self._leg_info(leg1)],
                    })
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
                if not leg1 or not leg1.departure_id:
                    return {}

                # Onward leg departs on the next ACTUAL scheduled departure
                # after the feeder's real arrival at the hub.
                hub_ready = self._parse_iso_dt(leg1.delivery_datetime) or pickup_date
                leg2_pickup_date = self._next_departure_after(
                    hub_ready, dest, origin_region=hub.canonical_region_id)
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
                if not leg2 or not leg2.departure_id:
                    return {}

                # Max custody hold from REAL configured times — never a
                # hardcoded 8 AM pickup assumption. The hold is the actual
                # gap between this leg's pickup datetime and the onward
                # leg's corridor departure datetime.
                leg1_pickup_dt = self._parse_iso_dt(leg1.pickup_datetime)
                leg2_dep_dt = self._parse_iso_dt(leg2.corridor_departure_datetime)
                if leg1_pickup_dt and leg2_dep_dt:
                    hold_hours = (leg2_dep_dt - leg1_pickup_dt).total_seconds() / 3600.0
                    if hold_hours > 24:
                        return {}  # reject — max 24h custody hold

                result.update({
                    "feasible": True,
                    "feeder_corridor": leg1.corridor_name,
                    "onward_corridor": leg2.corridor_name,
                    "departure_date": leg1.departure_date,
                    "estimated_delivery": self._date_part(leg2.delivery_datetime),
                    "pickup_datetime": leg1.pickup_datetime,
                    "delivery_datetime": leg2.delivery_datetime,
                    "corridor_departure_datetime": leg1.corridor_departure_datetime,
                    "corridor_id": leg1.corridor_id,
                    "corridor_name": leg1.corridor_name,
                    "departure_id": leg1.departure_id,
                    "timing_source": leg2.timing_source,
                    "transfer": True,
                    "transfer_hub_name": hub.public_name if hub else "",
                    "leg_count": 2,
                    "legs": [self._leg_info(leg1), self._leg_info(leg2)],
                })

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

    def _next_departure_after(self, after_date, dest_region, origin_region=None):
        """Find the next departure carrying origin_region → dest_region.

        Only directionally-compatible corridors count: a transfer leg
        leaving the Hub rides the corridor whose direction runs Hub →
        dest — never the corridor that merely serves dest as a delivery
        stop on the reverse run. origin_region defaults to the Hub's
        canonical region (transfer legs always start there)."""
        # Corridor stops are keyed by official-LTL region (142-159);
        # normalize old lane regions (1-20) before searching.
        dest_region = self._canonical_region(dest_region)
        if not dest_region:
            return None
        if origin_region is None:
            hub = self._get_default_hub()
            origin_region = hub.canonical_region_id if hub else False
        else:
            origin_region = self._canonical_region(origin_region)
        if not origin_region:
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
        # Direction filter: the onward corridor must run origin → dest.
        directionally_ok = []
        for corridor in Corridor.search([("id", "in", corridor_ids), ("active", "=", True)]):
            if self._directionally_compatible(corridor, origin_region, dest_region):
                directionally_ok.append(corridor.id)
        if not directionally_ok:
            return None

        start = after_date.strftime("%Y-%m-%d") if hasattr(after_date, 'strftime') else str(after_date)[:10]
        departures = Departure.search([
            ("corridor_id", "in", directionally_ok),
            ("departure_date", ">=", start),
            ("active", "=", True),
            ("status", "not in", ("cancelled", "completed")),
            ("vehicle_id", "!=", False),
        ], order="departure_date", limit=1)

        if departures:
            d = departures[0]
            return datetime.strptime(str(d.departure_date)[:10], "%Y-%m-%d")
        return None

    def _build_leg(self, sequence, leg_type, origin_region, dest_region,
                   pallets, weight_lbs, pickup_date, pickup_day, equipment,
                   transfer_hub, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                   requested_departure_id=None):
        """Build a ProposedLeg with corridor, departure, distance, and price.

        A leg is feasible ONLY with an actual scheduled departure row
        (active, not cancelled/completed, with a vehicle assigned). The
        corridor's operating-day checkbox alone is never a departure.

        When the calendar sent a requested_departure_id (so the quote binds
        to the exact departure the customer selected), it is server-
        re-validated here: it must exist, belong to this corridor, be on the
        requested pickup date, and be active/viable. Any mismatch → None —
        an arbitrary portal-supplied departure id is never trusted."""
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

        # Find departure — exact match on corridor + date, and ONLY a real
        # scheduled departure (active, not cancelled/completed, vehicle
        # assigned) makes this leg feasible.
        date_str = pickup_date.strftime("%Y-%m-%d") if hasattr(pickup_date, 'strftime') else str(pickup_date)[:10]
        departure = False
        if requested_departure_id:
            # Server-side re-validation of the departure the customer
            # selected on the calendar: must EXIST, belong to THIS corridor,
            # be on THIS pickup date, and be active/viable. An arbitrary or
            # stale portal-supplied id falls through to the corridor's own
            # exact-date departure — never a different date or corridor.
            requested = Departure.browse(int(requested_departure_id)).exists()
            if (requested
                    and requested.corridor_id.id == corridor.id
                    and str(requested.departure_date)[:10] == date_str
                    and requested.active
                    and requested.status not in ("cancelled", "completed")
                    and requested.vehicle_id):
                departure = requested
        if not departure:
            departure = Departure.search([
                ("corridor_id", "=", corridor.id),
                ("departure_date", "=", date_str),
                ("active", "=", True),
                ("status", "not in", ("cancelled", "completed")),
                ("vehicle_id", "!=", False),
            ], limit=1)
        if not departure:
            # No actual scheduled departure on this date — the leg is not
            # feasible even if the corridor operates today. The calendar
            # must never show a date the truck is not scheduled.
            return None

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

        # Real pickup / delivery times from the configured corridor stop
        # times (never a hardcoded 8 AM), with the corridor's departure time
        # and the canonical travel calculation as identified fallbacks.
        timings = self._leg_timings(
            corridor, departure, origin_region, dest_region, est_km, date_str)

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
            pickup_datetime=self._iso_dt(timings["pickup_datetime"]),
            delivery_datetime=self._iso_dt(timings["delivery_datetime"]),
            corridor_departure_datetime=self._iso_dt(timings["corridor_departure_datetime"]),
            timing_source=timings["timing_source"],
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

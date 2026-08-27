"""Shipment Routing Service — canonical shipment orchestration engine.

Connects all Phase 3-6 components into deterministic routing:
  Address → Region → Direct/Hub → Legs → Corridor → Departure → Distance → Price

Single entry point for all booking channels.
"""

import logging
from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from pytz import timezone as tz

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
    # Weight-aware pricing breakdown (canonical calculator output):
    "pricing_formula",          # dict — calculate_leg_per_km breakdown
    # Prior-day pickup (Phase 2): pickup_date is the day freight is
    # physically collected; departure_date stays the LINEHAUL day
    # (Sunday pickup → Monday departure). prior_day_pickup marks it.
    "pickup_date",              # str — physical pickup day (ISO date)
    "prior_day_pickup",         # bool — collected before the linehaul date
], defaults=[None, None, None, "", None, "", False])


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

        # Enrich coordinates + postal from the canonical facility (same
        # rule as plan_milk_run_route: client coords win, the facility
        # pin supplements). Access row preferred; the facility is the
        # physical fallback (SAVED LOCATION CONSOLIDATION 18.0.13.25.0).
        Access = self.env["logistics.location.customer.access"]
        Facility = self.env["prema.dispatch.location"]
        enriched = []
        for stop in stops or []:
            stop = dict(stop)
            acc = None
            acc_id = stop.get("customer_access_id")
            if acc_id:
                acc = Access.browse(int(acc_id))
                if not acc.exists():
                    acc = None
            fac = None
            fac_id = stop.get("facility_id") or (acc.facility_id.id if acc and acc.facility_id else None)
            if fac_id:
                fac = Facility.browse(int(fac_id))
                if not fac.exists():
                    fac = None
            if acc or fac:
                latitude = (acc.latitude if acc and acc.latitude
                            else (fac.pin_lat if fac and fac.pin_lat else 0.0))
                longitude = (acc.longitude if acc and acc.longitude
                             else (fac.pin_lng if fac and fac.pin_lng else 0.0))
                if not (stop.get("latitude") and stop.get("longitude")):
                    if latitude and longitude:
                        stop["latitude"] = latitude
                        stop["longitude"] = longitude
                if not stop.get("postal_code"):
                    stop["postal_code"] = (acc.postal_code if acc else "") or (fac.postal_code if fac else "") or ""
                if not stop.get("saved_location_id"):
                    # Normalize: downstream consumers read saved_location_id
                    # as the facility id.
                    stop["saved_location_id"] = fac.id if fac else None
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
                              equipment="dry", horizon_weeks=8,
                              shipment_type="ltl"):
        """Canonical calendar verdict for the portal.

        ONE authority: the same strict stop resolution the Get Price path
        runs. When the pickup (or any delivery) is a MANUAL_QUOTE movement
        the response carries manual_quote=True and NO dates — the portal
        renders the Manual Quote Required banner instead of fake
        selectable dates.

        FTL is a dedicated direct movement: with shipment_type="ftl" the
        calendar only returns ONE-LEG direct dates — the same
        direct-vs-transfer authority Get Price enforces via
        FTL_REQUIRES_DIRECT (never feeder_to_hub / hub transfer / linehaul
        / final-mile). When no direct FTL service exists the response is
        manual_quote=True with a clear lane-level reason — never a
        feeder/transit date Get Price would reject.

        Returns {"manual_quote": bool, "reason": str, "dates": [...]}.
        """
        resolved = self.resolve_route_stops(stops)
        if not resolved["serviceable"]:
            return {"manual_quote": True, "reason": resolved["reason"], "dates": []}
        dates = self.get_eligible_pickup_dates_for_route(
            stops, physical_pallets=physical_pallets, weight_lbs=weight_lbs,
            equipment=equipment, horizon_weeks=horizon_weeks,
            shipment_type=shipment_type,
        )
        if not dates:
            # The route resolves onto the network but NO scheduled pickup
            # date exists in the horizon (no corridor serves the lane, or
            # the only connection exceeds the custody hold). The portal
            # must render the Manual Quote Required banner — never a blank
            # calendar the Get Price path refuses. Same verdict for LTL
            # and FTL.
            if shipment_type == "ftl":
                reason = ("Dedicated Full Truckload service is not available "
                          "as a direct scheduled route for this lane.")
            else:
                reason = ("No scheduled service is available for this route "
                          "in the coming weeks.")
            return {"manual_quote": True, "reason": reason, "dates": []}
        return {"manual_quote": False, "reason": "", "dates": dates}

    # ── Real service timing (corridor stop config + travel-calc fallback) ──

    # Canonical planning speed for the travel-time fallback — the same
    # figure _build_leg uses for estimated_drive_hrs.
    _AVG_TRUCK_SPEED_KPH = 80.0

    def _facility_eta(self, stop, arrival_dt, stop_type):
        """Customer-facing ESTIMATED service time at a facility.

        Authority: ItineraryPlanner (facility operating hours snapshot +
        timing_type windows/appointments + service time), evaluated in the
        facility's own timezone. The estimate is NEVER before the facility
        opens: an early arrival waits at the door; a closed day (or an
        arrival after close) rolls to the next day the facility is open.

        Returns (eta_dt, source, rolled) — source is "facility_hours" when
        facility data was applied, None when the stop has no facility
        (e.g. the network hub) and the caller's fallback stands.
        """
        if not stop or not stop.get("saved_location_id"):
            return arrival_dt, None, False
        try:
            from ..services.itinerary_planner import (
                ItineraryPlanner,
                snapshot_facility_hours,
            )
            fac = self.env["prema.dispatch.location"].sudo().browse(
                int(stop["saved_location_id"]))
            if not fac.exists():
                return arrival_dt, None, False
            hours = snapshot_facility_hours(self.env, fac, stop_type)
            planner = ItineraryPlanner(self.env)
            # Recommended service time: an explicit booking request wins,
            # then the facility's planning authority (manual override →
            # historical median dwell → operational-class type default),
            # then the 15-minute baseline. ONE hierarchy — never a
            # hardcoded service time.
            service_min = 15
            if hasattr(fac, "planning_service_time_minutes"):
                service_min = fac.planning_service_time_minutes() or 15
            plan_stop = {
                "latitude": stop.get("latitude") or fac.pin_lat or 0.0,
                "longitude": stop.get("longitude") or fac.pin_lng or 0.0,
                "timezone": stop.get("timezone") or "America/Toronto",
                "operating_hours_snapshot": hours,
                "timing_type": stop.get("timing_type") or "flexible",
                "window_start": stop.get("window_start"),
                "window_end": stop.get("window_end"),
                "appointment_time": stop.get("appointment_time"),
                "service_time_minutes": stop.get("service_time_minutes") or service_min,
            }
            feasible, _waiting, service_start, _departure = planner.arrival_plan(
                plan_stop, arrival_dt)
            if feasible:
                return service_start, "facility_hours", False
            # Closed day or arrival after close: roll to the next open
            # slot (bounded — never an endless scan). The estimate still
            # respects facility hours: never before an opening time.
            probe = arrival_dt
            for _ in range(7):
                probe = probe + timedelta(days=1)
                window = planner.effective_window(plan_stop, probe)
                if window is None:
                    continue
                tz_obj = tz(plan_stop["timezone"])
                open_dt = probe.astimezone(tz_obj).replace(
                    hour=int(window[0]), minute=int((window[0] % 1) * 60), second=0)
                open_dt = open_dt.astimezone(arrival_dt.tzinfo) if arrival_dt.tzinfo else open_dt
                # Only accept if the truck can actually be served that day
                # (arrival-by-open is guaranteed by construction: we start
                # the day AT opening).
                return open_dt, "facility_hours", True
            return arrival_dt, None, False
        except Exception:
            # Facility data must never break a quote/calendar — fall back
            # to the caller's raw timing on any lookup failure.
            return arrival_dt, None, False

    def _leg_timings(self, corridor, departure, origin_region, dest_region,
                     distance_km, pickup_date, pickup_stop=None,
                     delivery_stop=None, linehaul_date=None):
        """Real pickup / delivery datetimes for one scheduled corridor leg.

        Authority order:
          1. corridor.stop planned times via resolve_region_segment
             (origin_departure_time / destination_arrival_time, plus the
             configured day offsets)
          2. CUSTOMER FACILITY HOURS — when the corridor stop has NO
             planned time, the ESTIMATED PICKUP / DELIVERY TIME comes from
             ItineraryPlanner (facility operating hours, timing-type
             windows, appointments, service time), evaluated in the
             facility's timezone. It is NEVER before the facility opens,
             and a closed day rolls to the next open day. This is the SAME
             authority for the calendar (_probe_legs) and Get Price
             (_build_leg) — one timing chain, two consumers.
          3. the departure's own departure_time (= corridor.start_time)
             for the pickup when no facility is known — never a hardcoded
             clock time
          4. the canonical travel-time calculation (distance / 80 km/h)
             for the delivery when neither a planned arrival time nor a
             facility is known — a clearly identified fallback.

        pickup_stop / delivery_stop: optional dicts with saved_location_id,
        latitude, longitude, timezone, timing_type / window_start /
        window_end / appointment_time / service_time_minutes (the portal
        stop shape).

        linehaul_date: when supplied (prior-day pickup), `pickup_date` is
        the day freight is physically collected while the corridor
        departure and the delivery estimate anchor on the LINEHAUL day
        (Sunday pickup → Monday departure). Same-day service passes
        linehaul_date=None and both anchors collapse to one — timing
        behavior is identical to before.

        Returns dict with datetime objects + per-side timing_source.
        """
        base = pickup_date
        if isinstance(base, str):
            base = datetime.strptime(base[:10], "%Y-%m-%d")

        dep_base = linehaul_date
        if dep_base:
            if isinstance(dep_base, str):
                dep_base = datetime.strptime(dep_base[:10], "%Y-%m-%d")
        else:
            dep_base = base

        dep_hour = departure.departure_time if departure else 0.0
        if not dep_hour and corridor:
            dep_hour = corridor.start_time or 0.0
        corridor_departure_dt = dep_base + timedelta(hours=dep_hour or 0.0)

        segment = corridor.resolve_region_segment(origin_region, dest_region) if corridor else False
        origin_day = (segment.get("pickup_day_offset") or 0) if segment else 0
        dest_day = (segment.get("delivery_day_offset") or 0) if segment else 0
        origin_hour = segment.get("origin_departure_time") if segment else False
        dest_hour = segment.get("destination_arrival_time") if segment else False

        if origin_hour:
            pickup_dt = base + timedelta(days=origin_day, hours=origin_hour)
            pickup_source = "configured"
        elif linehaul_date:
            # Prior-day pickup: the local pickup run starts at the
            # corridor's scheduled start time ON the pickup day — the
            # corridor stop times describe the linehaul run, not the
            # earlier local collection.
            pickup_dt = base + timedelta(hours=dep_hour or 0.0)
            pickup_source = "corridor_departure_time"
        else:
            pickup_dt = corridor_departure_dt + timedelta(days=origin_day)
            pickup_source = "corridor_departure_time"
            # Facility-aware ETA: the customer pickup time is NEVER the
            # corridor's 5:00 AM start — it is the facility's own earliest
            # serviceable time (opening hours + windows + appointments).
            if pickup_stop:
                arrival = pickup_dt
                hub_km = (segment.get("distance_from_origin_km") or 0.0) if segment else 0.0
                if hub_km > 0:
                    arrival = arrival + timedelta(hours=hub_km / self._AVG_TRUCK_SPEED_KPH)
                eta, source, _rolled = self._facility_eta(
                    pickup_stop, arrival, "pickup")
                if source:
                    pickup_dt = eta
                    pickup_source = source

        if dest_hour:
            delivery_dt = dep_base + timedelta(days=dest_day, hours=dest_hour)
            delivery_source = "configured"
        else:
            km = distance_km or ((segment.get("distance_km") or 0.0) if segment else 0.0) or 0.0
            # Prior-day pickup: delivery happens AFTER the linehaul
            # departure — anchor the travel estimate on the departure,
            # never on the earlier physical pickup day.
            delivery_anchor = corridor_departure_dt if linehaul_date else pickup_dt
            delivery_dt = delivery_anchor + timedelta(hours=km / self._AVG_TRUCK_SPEED_KPH)
            delivery_source = "travel_calc_fallback"
            # Facility-aware ETA on the delivery side too — the delivery
            # window/appointment and receiving hours govern the estimate.
            if delivery_stop:
                eta, source, _rolled = self._facility_eta(
                    delivery_stop, delivery_dt, "delivery")
                if source:
                    delivery_dt = eta
                    delivery_source = source

        if delivery_source == "facility_hours" or pickup_source == "facility_hours":
            timing_source = "facility_hours"
        elif delivery_source == "travel_calc_fallback":
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
        if not dt or (dt.hour == 0 and dt.minute == 0 and dt.second == 0):
            return ""
        return dt.strftime("%-I:%M %p")

    def _leg_info(self, leg):
        """Compact dict for a built leg — what the calendar needs to bind
        capacity and the quote to the EXACT departure."""
        return {
            "leg_type": leg.leg_type,
            "corridor_id": leg.corridor_id,
            "corridor_name": leg.corridor_name,
            "departure_id": leg.departure_id,
            "departure_date": leg.departure_date,
            "pickup_date": leg.pickup_date,
            "prior_day_pickup": leg.prior_day_pickup,
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
                   requested_departure_id=None, pickup_stop=None,
                   delivery_stop=None):
        """Plan a complete shipment route from pickup to delivery coordinates.

        requested_departure_id: the calendar-selected pickup departure.
        Server-re-validated inside _build_leg — it must belong to the
        corridor serving this route on the requested date, be active and
        still scheduled; otherwise the route is refused (never silently
        substituted with a different departure).

        pickup_stop / delivery_stop: optional customer-facility dicts
        (saved_location_id, timezone, timing fields) — when supplied, the
        leg ETAs are facility-hours-aware (never before facility opens).

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
            postal=(pickup_stop or {}).get("postal_code", ""),
        )
        snapshot["steps"].append({
            "step": "resolve_pickup",
            "outcome": pickup_result.outcome,
            "region": pickup_result.matched_region_code,
            "method": pickup_result.match_method,
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
            postal=(delivery_stop or {}).get("postal_code", ""),
        )
        snapshot["steps"].append({
            "step": "resolve_delivery",
            "outcome": delivery_result.outcome,
            "region": delivery_result.matched_region_code,
            "method": delivery_result.match_method,
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
            # Direction-aware default pickup day (no explicit date was
            # supplied): roll forward to the next ACTUAL scheduled
            # departure that can carry origin_region → destination_region
            # on the direct corridor in the correct corridor direction.
            # The old blind "tomorrow" default let the direction-blind
            # pickup-day check pass for a lane whose westbound corridor
            # runs a different day — QC-LANAUDIERE → ON-GTA passes Tuesday
            # (corridor 9 eastbound serves Lanaudière as a destination)
            # while corridor 11 westbound departs Wednesday → NO_LEGS even
            # though direct service exists. Explicitly requested dates are
            # NEVER moved (strict branch above).
            pickup_date = self._default_pickup_date(origin_region, dest_region)
            if pickup_date is None:
                return ShipmentRoute(
                    False, "No scheduled departure serves this lane in the coming weeks.",
                    "NO_LEGS", [], pallets, weight_lbs, None, snapshot)

        pickup_day = pickup_date.strftime("%A").lower()

        # ── Step 4: Validate pickup day against corridor schedule ──
        # Prior-day pickup: the physical pickup date may precede the
        # linehaul departure (Sunday pickup → Monday linehaul). The offset
        # comes from the bound departure when the portal supplied one (the
        # EXACT departure the calendar advertised), else from corridor
        # configuration. Offset 0 = same-day service (existing behavior).
        prior_day_offset = self._resolve_prior_day_offset(
            origin_region, pickup_date, requested_departure_id=requested_departure_id)
        valid_day = self._is_valid_pickup_day(
            origin_region, pickup_day,
            pickup_date=pickup_date if prior_day_offset else None)
        if not valid_day:
            # Route-aware advice (FSA reconciliation follow-up — the old
            # origin-only day scan plus its blind "+7 days" fallback kept
            # suggesting pickup dates for lanes no corridor can ever
            # serve (e.g. Kawarthas → Northumberland), chaining the
            # "next eligible pickup" forever). Distinguish:
            #   A) ROUTE EXISTS, DATE UNAVAILABLE — the lane has an ACTUAL
            #      scheduled departure (the same authority the calendar
            #      uses); advise the nearest one.
            #   B) NO ROUTE AT ALL — no corridor/departure can ever carry
            #      this lane under the current topology; stop suggesting
            #      dates and route the customer to a manual quote.
            next_day = self._default_pickup_date(origin_region, dest_region)
            if next_day is None:
                return ShipmentRoute(
                    False,
                    "No scheduled corridor currently serves this route. "
                    "Please request a manual quote.",
                    "NO_SCHEDULED_ROUTE", [], pallets, weight_lbs, None,
                    snapshot)
            return ShipmentRoute(
                False,
                f"Pickup day '{pickup_day}' is not served for {origin_region.code}. "
                f"Next eligible pickup: {next_day.strftime('%A')} {next_day.strftime('%Y-%m-%d')}.",
                "REQUESTED_PICKUP_DATE_NOT_SERVED", [], pallets, weight_lbs, None,
                {**snapshot, "next_eligible_pickup": next_day.strftime("%Y-%m-%d")},
            )

        # ── Step 5: Direct vs Hub decision ─────────────────────────
        # The direct/hub decision keys on the LINEHAUL day for prior-day
        # pickups — never the physical pickup day.
        linehaul_day = (pickup_date + timedelta(days=prior_day_offset)).strftime("%A").lower() \
            if prior_day_offset else pickup_day
        routing = direct_svc.decide(
            origin_region.id, dest_region.id,
            pickup_day=linehaul_day,
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
                pickup_stop=pickup_stop, delivery_stop=delivery_stop,
                prior_day_offset=prior_day_offset,
            )
            if leg:
                legs.append(leg)

        elif routing.decision == "HUB_TRANSFER_REQUIRED":
            # Check if delivery IS the Hub
            if dest_region.id == (hub.canonical_region_id.id if hub.canonical_region_id else None):
                # One leg: pickup → Hub (hub is a network facility — no
                # customer operating-hours ETA on this side).
                leg = self._build_leg(
                    sequence=1, leg_type="feeder_to_hub",
                    origin_region=origin_region, dest_region=dest_region,
                    pallets=pallets, weight_lbs=weight_lbs,
                    pickup_date=pickup_date, pickup_day=pickup_day,
                    equipment=equipment, transfer_hub=None,
                    pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                    requested_departure_id=requested_departure_id,
                    pickup_stop=pickup_stop, delivery_stop=None,
                    prior_day_offset=prior_day_offset,
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
                    pickup_stop=None, delivery_stop=delivery_stop,
                    prior_day_offset=prior_day_offset,
                )
                if leg:
                    legs.append(leg)
            else:
                # Leg 1: pickup → Hub (hub side = network facility, no
                # customer-hours ETA).
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
                    pickup_stop=pickup_stop, delivery_stop=None,
                    prior_day_offset=prior_day_offset,
                )
                if leg1:
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
                            requested_departure_id=requested_departure_id,
                            pickup_stop=None, delivery_stop=delivery_stop,
                        )
                        if leg2:
                            # Max custody hold from REAL configured times —
                            # the gap between the feeder's ARRIVAL at the
                            # hub (custody handoff) and the onward corridor
                            # departure. Mirrors _probe_legs exactly, so
                            # calendar and Get Price always agree.
                            leg1_arrival_dt = (self._parse_iso_dt(leg1.delivery_datetime)
                                               or self._parse_iso_dt(leg1.pickup_datetime))
                            leg2_dep_dt = self._parse_iso_dt(leg2.corridor_departure_datetime)
                            hold_ok = True
                            if leg1_arrival_dt and leg2_dep_dt:
                                hold_hours = (leg2_dep_dt - leg1_arrival_dt).total_seconds() / 3600.0
                                hold_ok = hold_hours <= 24
                            if hold_ok:
                                # A route is available ONLY if the final leg
                                # reaches the requested destination — never a
                                # feeder-only pickup → Hub route. Mirrors
                                # _probe_legs exactly, so calendar and Get
                                # Price always agree.
                                leg1 = leg1._replace(hub_ready_at=leg1.delivery_datetime)
                                legs.append(leg1)
                                legs.append(leg2)

        if not legs:
            # Route-aware advice — the same authority as the calendar.
            # The requested date can be a valid pickup day for the origin
            # region yet have NO actual departure for THIS lane (e.g.
            # GTA → Niagara on a Wednesday — corridor 12 runs Mon/Thu),
            # which used to surface as a bare NO_LEGS. Distinguish:
            #   A) the lane has a real future departure — advise it;
            #   B) no corridor can ever carry the lane — stop suggesting
            #      dates, route to a manual quote.
            # A next_day equal to the requested date itself means the
            # failure is capacity/equipment on a served date — the bare
            # NO_LEGS message stays honest there.
            next_day = self._default_pickup_date(origin_region, dest_region)
            if next_day is not None and next_day != pickup_date:
                return ShipmentRoute(
                    False,
                    f"No scheduled departure serves {origin_region.code} → "
                    f"{dest_region.code} on {pickup_day} {pickup_date.strftime('%Y-%m-%d')}. "
                    f"Next eligible pickup: {next_day.strftime('%A')} "
                    f"{next_day.strftime('%Y-%m-%d')}.",
                    "REQUESTED_PICKUP_DATE_NOT_SERVED", [], pallets, weight_lbs,
                    None,
                    {**snapshot, "next_eligible_pickup": next_day.strftime("%Y-%m-%d")})
            if next_day is None:
                return ShipmentRoute(
                    False,
                    "No scheduled corridor currently serves this route. "
                    "Please request a manual quote.",
                    "NO_SCHEDULED_ROUTE", [], pallets, weight_lbs, None, snapshot)
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
            # Billable distance for a dedicated truck = the ACTUAL
            # point-to-point road distance between the real pickup and
            # delivery facilities when their coordinates are known — a
            # dedicated FTL truck drives door-to-door, not along the
            # corridor's scheduled route. Google Routes (with the built-in
            # straight-line ×1.4 fallback) is the same road-distance
            # authority the corridor distance recalculation uses; the
            # corridor's segment distance remains the estimate for
            # coordinate-less (FSA-only) requests — never the full
            # corridor length.
            actual_km = 0.0
            if pickup_lat and pickup_lng and delivery_lat and delivery_lng:
                try:
                    from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
                    actual_legs = DispatchRouteService(self.env).get_sequential_travel([
                        (float(pickup_lat), float(pickup_lng)),
                        (float(delivery_lat), float(delivery_lng)),
                    ])
                    if actual_legs:
                        actual_km = float(actual_legs[0].get("distance_km") or 0.0)
                except (TypeError, ValueError):
                    actual_km = 0.0
            segment = corridor.resolve_region_segment(origin_region, dest_region)
            distance = actual_km or (
                segment["distance_km"] if segment else (legs[0].estimated_distance_km or 0.0))
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
            # Single-leg LTL: when both facilities have real coordinates,
            # bill the ACTUAL point-to-point road distance — the same
            # Google-first authority the FTL branch uses above. A
            # door-to-door LTL shipment on a multi-stop corridor is not
            # measured by the corridor's full segment span (London and
            # Windsor share the SWON segment at 228.8 km today). The
            # corridor's segment distance remains the estimate for
            # coordinate-less (FSA-only) requests, and multi-leg
            # hub-transfer routes stay segment-priced per leg — only a
            # direct single-leg move gets actual distance. The price
            # freezes into the route snapshot, so quote and confirm
            # always agree.
            if len(legs) == 1 and pickup_lat and pickup_lng \
                    and delivery_lat and delivery_lng:
                actual_km = 0.0
                try:
                    from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
                    actual_legs = DispatchRouteService(self.env).get_sequential_travel([
                        (float(pickup_lat), float(pickup_lng)),
                        (float(delivery_lat), float(delivery_lng)),
                    ])
                    if actual_legs:
                        actual_km = float(actual_legs[0].get("distance_km") or 0.0)
                except (TypeError, ValueError):
                    actual_km = 0.0
                if actual_km:
                    from ..services.pricing_service import PricingService
                    rate_per_km = corridor.rate_per_km if corridor else 4.0
                    planned_pallets = corridor.planned_pallets if corridor else 8
                    included_weight = corridor.included_weight_per_pallet \
                        if corridor else 500.0
                    excess_rate = corridor.excess_weight_rate_per_lb \
                        if corridor else 0.0
                    if not excess_rate:
                        excess_rate = float(
                            self.env["ir.config_parameter"].sudo().get_param(
                                "logistics.default_excess_weight_rate", "0.0") or 0.0
                        )
                    pricing = PricingService.calculate_leg_per_km(
                        actual_km, rate_per_km, max(planned_pallets, 1), pallets,
                        max(included_weight, 1.0), weight_lbs,
                        currency=corridor.currency_id if corridor else None,
                        excess_weight_rate_per_lb=excess_rate or None,
                    )
                    legs[0] = legs[0]._replace(
                        estimated_distance_km=round(actual_km, 1),
                        estimated_drive_hrs=round(actual_km / 80, 1),
                        rate_per_km=rate_per_km,
                        pallet_rate_per_km=round(pricing["pallet_rate_per_km"], 4),
                        leg_price=round(pricing["subtotal"], 2),
                    )
                    snapshot["actual_distance_km"] = round(actual_km, 1)
                    leg_total_raw = sum(leg.leg_price for leg in legs)
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

    # FTL multi-stop surcharge fallbacks for lanes with NO exact FTL
    # Regional Pricing rule (the base price falls back to the corridor
    # FTL $/km — the surcharges fall back to the same standard defaults
    # the rule rows carry). Used ONLY for FTL movements; LTL never reads
    # these.
    FTL_DEFAULT_SAME_REGION_STOP_CHARGE = 50.0
    FTL_DEFAULT_REGIONAL_STOP_CHARGE = 75.0

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
        # Google pin) OR on the stop's canonical facility — the portal
        # passes route_stops through with saved_location_id = facility id
        # only. Client coords win; the facility pin supplements when they
        # are missing (SAVED LOCATION CONSOLIDATION 18.0.13.25.0).
        Facility = self.env["prema.dispatch.location"]

        def _enrich(stop):
            stop = dict(stop)
            if not (stop.get("latitude") and stop.get("longitude")):
                loc_id = stop.get("saved_location_id")
                if loc_id:
                    fac = Facility.browse(int(loc_id))
                    if fac.exists() and fac.pin_lat and fac.pin_lng:
                        stop["latitude"] = fac.pin_lat
                        stop["longitude"] = fac.pin_lng
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
                pickup_stop=origin, delivery_stop=stop,
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
                    pickup_stop=origin, delivery_stop=stop,
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

        # ── FTL multi-stop pricing ─────────────────────────────────────
        # ONE server-side calculation (never re-derived in the portal,
        # invoice or confirmation). Base = the existing FTL regional rule
        # ORIGIN REGION → FURTHEST DELIVERY REGION — already computed by
        # the furthest per-stop plan_route above (snapshot ftl_pricing) —
        # never a sum of independent per-destination FTL prices. The first
        # delivery event in the base region is INCLUDED in the base price;
        # the first delivery event in any other region carries the rule's
        # Regional Stop fee; every later delivery in an already-served
        # region carries the Same-Region Stop fee. Backtracking / detour /
        # unreachable itineraries (already flagged above) REFUSE the FTL
        # quote for manual review instead of auto-adding fees — the
        # existing routing/manual-review authority is preserved.
        ftl_multistop = None
        if shipment_type == "ftl":
            ftl_multistop = self._compute_ftl_multistop(
                origin, deliveries, furthest_entry, furthest_route,
                manual_review_reasons,
            )
            if ftl_multistop.get("manual_review"):
                snapshot = dict(furthest_route.routing_snapshot)
                snapshot["ftl_multistop"] = ftl_multistop
                snapshot["milk_run"] = {
                    "basis": basis, "per_stop": per_stop,
                    "manual_review_required": True,
                    "manual_review_reasons": manual_review_reasons,
                }
                return ShipmentRoute(
                    False,
                    "Full Truckload multi-stop route requires manual review: %s"
                    % "; ".join(manual_review_reasons),
                    "MANUAL_REVIEW", [], pallets, weight_lbs, None, snapshot,
                )

        # ── Merge the canonical route with milk-run metadata ───────────
        snapshot = dict(furthest_route.routing_snapshot)
        if ftl_multistop:
            # Freeze the FTL multi-stop calculation into the route
            # snapshot: the base price, stop counts/rates/totals and the
            # final transportation (base + surcharges) — the pricing
            # session and the confirmation read THIS, never live config.
            snapshot["ftl_multistop"] = ftl_multistop
            pricing = dict(snapshot.get("pricing") or {})
            pricing["final_transportation"] = round(
                ftl_multistop["final_transportation"], 2)
            snapshot["pricing"] = pricing
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

    def _compute_ftl_multistop(self, origin, deliveries, furthest_entry,
                               furthest_route, manual_review_reasons):
        """One authoritative FTL multi-stop fee calculation (server-side).

        Base price = the existing FTL regional rule ORIGIN REGION →
        FURTHEST DELIVERY REGION, already computed by the furthest
        per-stop plan_route (snapshot ftl_pricing) — never re-derived
        here and never a sum of per-destination FTL prices. The base
        price includes 1 pickup + 1 delivery (the first delivery event
        in the base region — never surcharged).

        Every delivery is classified by its CANONICAL region (Region
        Resolver — never raw city strings), in itinerary order:

            first delivery in the base region      → INCLUDED (no fee)
            first delivery in any other region     → Regional Stop fee
            later delivery in an already-served
            region                                 → Same-Region Stop fee

        Rates come from the exact ORIGIN → FURTHEST rule row; lanes
        without an exact rule fall back to the standard defaults.

        Returns the frozen ftl_multistop snapshot dict, or
        {"manual_review": True, "manual_review_reasons": [...]} when the
        itinerary is already flagged (backtracking / unreachable /
        unimplemented basis) — the caller then REFUSES the FTL quote
        instead of auto-adding fees.
        """
        if manual_review_reasons:
            return {
                "manual_review": True,
                "manual_review_reasons": list(manual_review_reasons),
            }

        from ..services.region_resolver import RegionResolver
        resolver = RegionResolver(self.env)

        def _canonical_region(latitude, longitude):
            if not latitude or not longitude:
                return None
            match = resolver.resolve(float(latitude), float(longitude))
            region = match.matched_region
            return resolver.canonical_region(region) if region else None

        origin_region = _canonical_region(
            origin.get("latitude"), origin.get("longitude"))
        if not origin_region:
            return {
                "manual_review": True,
                "manual_review_reasons": [
                    "pickup region could not be resolved for FTL multi-stop "
                    "pricing",
                ],
            }

        by_key = {}
        for stop in deliveries:
            region = _canonical_region(
                stop.get("latitude"), stop.get("longitude"))
            if not region:
                return {
                    "manual_review": True,
                    "manual_review_reasons": [
                        "delivery '%s' region could not be resolved for FTL "
                        "multi-stop pricing" % (stop.get("stop_key") or ""),
                    ],
                }
            by_key[stop.get("stop_key", "")] = region

        base_region = by_key.get(furthest_entry["stop_key"])
        if not base_region:
            return {
                "manual_review": True,
                "manual_review_reasons": [
                    "furthest delivery region could not be resolved for FTL "
                    "multi-stop pricing",
                ],
            }

        # Surcharge rates from the exact ORIGIN → FURTHEST rule; lanes
        # without one use the standard defaults (the base price itself
        # falls back to the corridor FTL $/km exactly as before).
        corridor = (
            self.env["logistics.corridor"].browse(
                furthest_route.legs[0].corridor_id)
            if furthest_route.legs and furthest_route.legs[0].corridor_id
            else False
        )
        base_rule = (
            corridor.get_ftl_regional_rule(origin_region, base_region)
            if corridor else False
        )
        same_rate = float(
            base_rule.same_region_additional_stop_charge
            if base_rule and base_rule.same_region_additional_stop_charge
            else self.FTL_DEFAULT_SAME_REGION_STOP_CHARGE)
        regional_rate = float(
            base_rule.regional_additional_stop_charge
            if base_rule and base_rule.regional_additional_stop_charge
            else self.FTL_DEFAULT_REGIONAL_STOP_CHARGE)

        # Base price: the furthest per-stop route's FTL price (furthest
        # destination rule → corridor FTL $/km fallback) — already
        # authoritative in its snapshot (pricing.final_transportation —
        # for FTL that IS the leg price: no volume discount, no booking
        # minimum), never recomputed.
        ftl_pricing = furthest_route.routing_snapshot.get("ftl_pricing") or {}
        route_pricing = furthest_route.routing_snapshot.get("pricing") or {}
        base_price = float(route_pricing.get("final_transportation") or 0.0)

        served = set()
        per_stop_fees = []
        regional_count = 0
        same_count = 0
        regional_total = 0.0
        same_total = 0.0
        for stop in deliveries:
            key = stop.get("stop_key", "")
            region = by_key[key]
            city = stop.get("city") or stop.get("location_name") or key or "Delivery"
            if region.id == base_region.id and region.id not in served:
                # The included delivery — the ONE delivery in the base
                # region covered by the base FTL price. Never surcharged.
                served.add(region.id)
                fee_type = "included"
                amount = 0.0
            elif region.id in served:
                # Region already served by a previous delivery event.
                fee_type = "same_region"
                same_count += 1
                amount = same_rate
                same_total += same_rate
            else:
                # First delivery event in an additional en-route region.
                fee_type = "regional"
                regional_count += 1
                amount = regional_rate
                regional_total += regional_rate
                served.add(region.id)
            per_stop_fees.append({
                "stop_key": key,
                "city": city,
                "region_code": region.code or "",
                "region_name": region.name or "",
                "fee_type": fee_type,
                "amount": round(amount, 2),
            })

        return {
            "base_rule_id": (
                ftl_pricing.get("regional_rule_id")
                or (base_rule.id if base_rule else False)),
            "base_destination_region_id": base_region.id,
            "base_price": round(base_price, 2),
            "base_rate_per_km": float(ftl_pricing.get("rate_per_km") or 0.0),
            "base_distance_km": float(ftl_pricing.get("distance_km") or 0.0),
            "regional_stop_count": regional_count,
            "regional_stop_rate": round(regional_rate, 2),
            "regional_stop_total": round(regional_total, 2),
            "same_region_stop_count": same_count,
            "same_region_stop_rate": round(same_rate, 2),
            "same_region_stop_total": round(same_total, 2),
            "per_stop": per_stop_fees,
            "final_transportation": round(base_price + regional_total + same_total, 2),
        }

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
                                   horizon_weeks=8, shipment_type="ltl"):
        """Eligible pickup dates for a single-pickup / single-delivery
        movement (legacy signature). Delegates to the full-route engine —
        one code path for every booking shape."""
        stops = [
            {"stop_type": "pickup", "latitude": pickup_lat, "longitude": pickup_lng},
            {"stop_type": "delivery", "latitude": delivery_lat, "longitude": delivery_lng},
        ]
        return self.get_eligible_pickup_dates_for_route(
            stops, physical_pallets=pallets, weight_lbs=weight_lbs,
            equipment=equipment, horizon_weeks=horizon_weeks,
            shipment_type=shipment_type)

    def get_eligible_pickup_dates_for_route(self, stops, physical_pallets=1,
                                            weight_lbs=500, equipment="dry",
                                            horizon_weeks=8, shipment_type="ltl"):
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

        shipment_type="ftl": dedicated direct movement only — every
        delivery stop must probe as EXACTLY ONE leg (leg_count == 1, the
        same single-leg rule Get Price enforces via FTL_REQUIRES_DIRECT);
        a two-leg feeder + onward transfer is never offered, while a
        single-corridor movement whose origin is the hub region stays
        eligible (Get Price prices it FTL). The serving corridor must also
        carry a priceable FTL rate (Get Price's FTL_RATE_NOT_CONFIGURED).
        Never inferred from pallet count.

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

            # Pickup day must be servable for the origin region — same-day
            # corridor service first (offset 0 = existing behavior), then
            # prior-day pickup offsets (Sunday pickup → Monday linehaul)
            # up to the corridor's configured max; first feasible wins.
            max_off = self._max_prior_day_offset(origin_region)
            entry = None
            for offset in range(0, max_off + 1):
                if not self._is_valid_pickup_day(
                        origin_region, day_name,
                        pickup_date=current if offset else None):
                    continue
                entry = self._route_entry_at_offset(
                    origin_region, delivery_plans, hub, origin,
                    current, day_name, date_str, offset,
                    physical_pallets, weight_lbs, equipment, shipment_type)
                if entry:
                    break
            if entry:
                eligible.append(entry)

            current += timedelta(days=1)

        return eligible

    def _route_entry_at_offset(self, origin_region, delivery_plans, hub, origin,
                               current, day_name, date_str, offset,
                               physical_pallets, weight_lbs, equipment,
                               shipment_type):
        """Evaluate the FULL route on one pickup date at one prior-day
        offset. Returns the eligible calendar entry dict, or None when the
        route cannot move (any stop infeasible, exact departures ineligible
        or over capacity). offset 0 = same-day service (existing behavior);
        offset N = freight physically picked up N days before the linehaul
        departure (Sunday pickup → Monday linehaul, capacity still
        evaluated against the EXACT Monday departure)."""
        from ..services.departure_resolver import DepartureResolver
        from ..services.direct_delivery_service import DirectDeliveryService

        departure_svc = DepartureResolver(self.env)
        Departure = self.env["logistics.corridor.departure"]
        # The direct/hub decision keys on the LINEHAUL day for prior-day
        # pickups — never the physical pickup day.
        linehaul_day = (current + timedelta(days=offset)).strftime("%A").lower() \
            if offset else day_name
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
        # FTL rate-gate cache keyed by (corridor, origin, dest) — the
        # corridor's FTL pricing configuration is static per segment.
        ftl_rate_ok = {}
        for plan in delivery_plans:
            # Re-decide per pickup DAY — the direct/hub decision can
            # differ by day (rule allowed_service_days), and the quote
            # path decides with the same pickup_day.
            routing = direct_svc.decide(
                origin_region.id, plan["dest"].id, pickup_day=linehaul_day)
            legs_info = self._probe_legs(
                origin_region, plan["dest"], hub, routing,
                current, day_name,
                float(origin["latitude"]), float(origin["longitude"]),
                float(plan["stop"]["latitude"]), float(plan["stop"]["longitude"]),
                physical_pallets, weight_lbs, equipment,
                pickup_stop=origin, delivery_stop=plan["stop"],
                prior_day_offset=offset,
            )
            if not legs_info or not legs_info.get("feasible"):
                route_feasible = False
                per_stop.append({
                    "stop_key": plan["stop"].get("stop_key", ""),
                    "city": plan["stop"].get("city", ""),
                    "feasible": False,
                })
                continue
            if shipment_type == "ftl" and legs_info.get("leg_count", 1) != 1:
                # FTL is a dedicated direct movement — EXACTLY the
                # single-leg rule Get Price enforces via
                # FTL_REQUIRES_DIRECT (len(legs) != 1). leg_count == 2
                # means feeder + onward transfer; leg_count == 1
                # includes the origin-is-the-hub single-corridor case
                # (e.g. GTA -> Niagara on one corridor), which Get
                # Price prices as FTL. No calendar date may advertise
                # a transfer for FTL.
                route_feasible = False
                per_stop.append({
                    "stop_key": plan["stop"].get("stop_key", ""),
                    "city": plan["stop"].get("city", ""),
                    "feasible": False,
                })
                continue
            if shipment_type == "ftl" and legs_info.get("corridor_id"):
                # FTL rate gate: mirror plan_route's
                # FTL_RATE_NOT_CONFIGURED verdict — a direct FTL date
                # must be PRICEABLE at Get Price, otherwise calendar
                # and quote disagree. Read-only configuration check;
                # never a price calculation.
                key = (legs_info["corridor_id"], origin_region.id,
                       plan["dest"].id)
                if key not in ftl_rate_ok:
                    corridor = self.env["logistics.corridor"].browse(
                        legs_info["corridor_id"])
                    if corridor.enable_ftl:
                        ftl = corridor.compute_ftl_price(
                            origin_region, plan["dest"], 0.0)
                        if ftl["pricing_type"] == "flat_rate":
                            ftl_rate_ok[key] = bool(ftl["regional_rule"]) and (
                                ftl["regional_rule"].flat_rate or 0.0) > 0
                        else:
                            ftl_rate_ok[key] = (ftl.get("rate_per_km") or 0.0) > 0
                    else:
                        # Corridor does not enable FTL — Get Price
                        # prices the shipment as LTL (existing
                        # behavior) and succeeds.
                        ftl_rate_ok[key] = True
                if not ftl_rate_ok[key]:
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
            return None

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
            return None
        all_departures_eligible = True
        for dep_id in unique_dep_ids:
            dep = Departure.sudo().browse(dep_id)
            matching_legs = [
                leg for leg in all_legs if leg.get("departure_id") == dep_id
            ]
            leg = matching_legs[0] if matching_legs else {}
            # Leg-scoped names — must NOT shadow the outer pickup-stop
            # dict `origin` used by the probe loop above (shadowing
            # crashed the next iteration with KeyError 'latitude').
            leg_origin_region = self._canonical_region(
                leg.get("origin_region_id") or leg.get("origin_region"))
            leg_destination_region = self._canonical_region(
                leg.get("dest_region_id") or leg.get("dest_region")
            )
            ok, _reason, _vehicle = departure_svc.evaluate_departure(
                dep, equipment, physical_pallets, weight_lbs,
                service_type=shipment_type,
                origin_region=leg_origin_region,
                dest_region=leg_destination_region,
            )
            if not ok:
                all_departures_eligible = False
                break
        if not all_departures_eligible:
            return None

        # NOTE: exact pallet capacity is deliberately NOT exposed here.
        # Every returned date already fits the requested quantity (the
        # eligibility loop above filters on capacity server-side), so the
        # payload carries only generic state — never max/reserved/
        # remaining positions or layout details. Server-side validation
        # at Get Price / confirm remains the authority.
        first_leg = all_legs[0]
        return {
            "date": date_str,
            "day_name": day_name.capitalize(),
            "prior_day_pickup": bool(offset),
            # The exact service option this date sells:
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
            # Hub transfer: only the hub's PUBLIC name (customer-safe by
            # design) — never internal corridor/leg language.
            "transfer": bool(leg_count == 2),
            "transfer_hub_name": transfer_hub_name,
            "capacity_state": "available",
        }

    def _probe_legs(self, origin, dest, hub, routing, pickup_date, pickup_day,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    pallets, weight_lbs, equipment, pickup_stop=None,
                    delivery_stop=None, prior_day_offset=0):
        """Quick-probe leg feasibility for a candidate pickup date. Returns
        dict with corridor/departure info if feasible, or empty dict.

        A leg is feasible ONLY with an actual scheduled departure — the
        corridor's operating-day checkbox alone is never sufficient.
        Every returned leg carries its exact departure_id; the caller
        evaluates capacity against those exact departures.

        prior_day_offset: days between the physical pickup and the linehaul
        departure — applied to the FIRST (pickup) leg only; the onward
        transfer leg keeps its own next-departure pickup date.

        pickup_stop / delivery_stop: customer-facility dicts threaded to
        _build_leg so calendar ETAs use the SAME facility-hours authority
        as Get Price."""
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
                pickup_stop=pickup_stop, delivery_stop=delivery_stop,
                prior_day_offset=prior_day_offset,
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
                    pickup_stop=pickup_stop, delivery_stop=None,
                    prior_day_offset=prior_day_offset,
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
                    pickup_stop=None, delivery_stop=delivery_stop,
                    prior_day_offset=prior_day_offset,
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
                    pickup_stop=pickup_stop, delivery_stop=None,
                    prior_day_offset=prior_day_offset,
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
                    pickup_stop=None, delivery_stop=delivery_stop,
                )
                if not leg2 or not leg2.departure_id:
                    return {}

                # Max custody hold from REAL configured times — never a
                # hardcoded 8 AM pickup assumption. The hold is the actual
                # gap between the feeder's ARRIVAL at the hub (the custody
                # handoff) and the onward leg's corridor departure
                # datetime. Measuring from the origin pickup overstates
                # the hold: a midnight feeder departure would turn a real
                # Wed→Thu connection (~20h at the hub) into a fake 30h.
                leg1_arrival_dt = (self._parse_iso_dt(leg1.delivery_datetime)
                                   or self._parse_iso_dt(leg1.pickup_datetime))
                leg2_dep_dt = self._parse_iso_dt(leg2.corridor_departure_datetime)
                if leg1_arrival_dt and leg2_dep_dt:
                    hold_hours = (leg2_dep_dt - leg1_arrival_dt).total_seconds() / 3600.0
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

    def _is_valid_pickup_day(self, region, day_name, pickup_date=None):
        """Check if any corridor serving this region operates on the given day.

        pickup_date: when supplied, the day is ALSO valid when some
        corridor serving this region allows prior-day pickup and operates
        within prior_day_pickup_max_days AFTER this date (Sunday pickup →
        Monday linehaul). Without it (or with the feature off) the check
        is exactly the original same-day scan — behavior unchanged."""
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
        if corridors:
            return True

        # Prior-day pickup: some corridor serving this region allows
        # collecting freight before its scheduled linehaul departure.
        if pickup_date:
            if isinstance(pickup_date, str):
                pickup_date = datetime.strptime(pickup_date[:10], "%Y-%m-%d")
            prior = Corridor.search([
                ("id", "in", corridor_ids),
                ("active", "=", True),
                ("allow_prior_day_pickup", "=", True),
            ])
            for corridor in prior:
                max_off = corridor.prior_day_pickup_max_days or 0
                for off in range(1, max_off + 1):
                    linehaul = pickup_date + timedelta(days=off)
                    if getattr(corridor, f"operate_{linehaul.strftime('%A').lower()}"):
                        return True
        return False

    def _max_prior_day_offset(self, region):
        """Highest prior-day-pickup offset any corridor serving this region
        allows (0 = feature off everywhere). Drives the calendar's offset
        scan: offsets 1..max are tried only when the same-day entry is
        infeasible, so enabling prior-day pickup never masks a normal
        same-day service day."""
        region = self._canonical_region(region)
        if not region:
            return 0
        Corridor = self.env["logistics.corridor"]
        Stop = self.env["logistics.corridor.stop"]
        stops = Stop.search([
            ("region_id", "=", region.id),
            ("active", "=", True),
            ("pickup_allowed", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        if not corridor_ids:
            return 0
        prior = Corridor.search([
            ("id", "in", corridor_ids),
            ("active", "=", True),
            ("allow_prior_day_pickup", "=", True),
        ])
        return max((c.prior_day_pickup_max_days or 0) for c in prior) if prior else 0

    def _resolve_prior_day_offset(self, origin_region, pickup_date,
                                  requested_departure_id=None):
        """Prior-day offset (days the physical pickup precedes the linehaul
        departure) for a pickup date, or 0 = same-day service.

        With requested_departure_id the offset is DERIVED from that
        departure's linehaul date — the portal binds the EXACT departure
        the calendar advertised — and the departure's own corridor must
        allow the offset. Without it, the smallest offset whose linehaul
        day is actually operated by a prior-day-enabled corridor serving
        the origin region wins. Never exceeds any corridor's configured
        max; returns 0 when the date is not a prior-day pickup date."""
        if requested_departure_id:
            dep = self.env["logistics.corridor.departure"].sudo().browse(
                requested_departure_id)
            if dep and dep.departure_date:
                linehaul = dep.departure_date
                # Same-type comparison: date - datetime raises TypeError.
                if isinstance(pickup_date, datetime):
                    pickup_date = pickup_date.date()
                offset = (linehaul - pickup_date).days
                corridor = dep.corridor_id
                if (offset >= 1 and corridor.allow_prior_day_pickup
                        and offset <= (corridor.prior_day_pickup_max_days or 0)):
                    return offset
            return 0
        max_off = self._max_prior_day_offset(origin_region)
        for off in range(1, max_off + 1):
            linehaul = pickup_date + timedelta(days=off)
            if self._prior_day_corridor_for(origin_region, linehaul, off):
                return off
        return 0

    def _prior_day_corridor_for(self, region, linehaul_date, offset):
        """True when some active corridor serving this region allows
        prior-day pickup and operates on the linehaul date at the given
        offset. Same-day (offset 0) is NEVER matched here — offset-0
        service is the original same-day scan's job."""
        region = self._canonical_region(region)
        if not region:
            return False
        if isinstance(linehaul_date, str):
            linehaul_date = datetime.strptime(linehaul_date[:10], "%Y-%m-%d")
        day_field = f"operate_{linehaul_date.strftime('%A').lower()}"
        Corridor = self.env["logistics.corridor"]
        Stop = self.env["logistics.corridor.stop"]
        stops = Stop.search([
            ("region_id", "=", region.id),
            ("active", "=", True),
            ("pickup_allowed", "=", True),
        ])
        corridor_ids = stops.mapped("corridor_id").ids
        if not corridor_ids:
            return False
        return bool(Corridor.search([
            ("id", "in", corridor_ids),
            ("active", "=", True),
            ("allow_prior_day_pickup", "=", True),
            ("prior_day_pickup_max_days", ">=", offset),
            (day_field, "=", True),
        ]))

    def _next_valid_pickup_day(self, region, from_date):
        """Find next date when this region is served for pickup."""
        dt = from_date + timedelta(days=1)
        for _ in range(14):  # search 2 weeks
            day = dt.strftime("%A").lower()
            if self._is_valid_pickup_day(region, day):
                return dt
            dt += timedelta(days=1)
        return from_date + timedelta(days=7)  # fallback

    def _default_pickup_date(self, origin_region, dest_region, horizon_days=56):
        """Direction-aware default pickup day for a lane.

        When the customer did NOT pick an explicit pickup date, plan_route
        must roll forward to the next ACTUAL scheduled departure that can
        carry origin_region → destination_region in the correct corridor
        direction — never a blind "tomorrow" that only proves the origin
        region is served somewhere on the network that day.

        Uses the SAME canonical direct-service authority as the calendar
        (logistics.corridor.find_direct_service) and requires an actual
        departure row (active, not cancelled/completed, vehicle assigned)
        — exactly the departure-level proof _build_leg enforces, so the
        rolled-forward date always builds.

        Lanes without a direct corridor (hub-transfer / rule-based lanes)
        roll forward to the next ACTUAL scheduled departure the canonical
        DepartureResolver picks — the SAME authority the calendar and the
        phone-quote path use. The legacy blind "tomorrow" default could
        return a day the origin region is not served on, making a bare
        Get Price call refuse a lane the calendar shows as bookable.

        Returns a datetime date, or None when the lane is direct but has
        no scheduled departure within the horizon (the caller converts
        that to NO_LEGS).
        """
        origin = self._canonical_region(origin_region)
        dest = self._canonical_region(dest_region)
        Corridor = self.env["logistics.corridor"]
        if origin and dest and origin.id == dest.id:
            # Same-region lanes are carried ONLY by local corridors
            # (resolve_region_segment refuses same-region on linehaul
            # corridors that revisit the hub region). find_direct_service's
            # same-region early return hands back the FIRST corridor with a
            # stop in the region regardless of direction — trusting it
            # chained the "next eligible pickup" forever for lanes like
            # Belleville → Kingston (both ON-SOUTHEASTERN — no local
            # corridor) by proposing the next c9/c13 departure that can
            # never carry the lane. Search segment-first, the way
            # DepartureResolver._candidate_corridors does.
            direct = next(
                (c for c in Corridor.search([("active", "=", True)])
                 if c.resolve_region_segment(origin, dest)), False)
        else:
            direct = Corridor.find_direct_service(origin_region, dest_region)
            if direct and not direct.resolve_region_segment(origin, dest):
                # find_direct_service can return a corridor whose segment
                # cannot be built for this lane — never advise a date on it.
                direct = False
        if not direct:
            # Hub transfers never serve same-region lanes — the transfer
            # resolver would propose a meaningless round trip through the
            # hub (e.g. SEO → GTA on corridor 11, GTA → SEO on 13). No
            # route at all.
            if origin and dest and origin.id == dest.id:
                return None
            from ..services.departure_resolver import DepartureResolver
            resolution = DepartureResolver(self.env).resolve(
                origin_region, dest_region, "dry", 1, 500,
                earliest_pickup_date=self._op_today() + timedelta(days=1),
                service_type="ltl",
            )
            if not resolution.available:
                return None
            return resolution.legs[0].departure.departure_date
        start = self._op_today() + timedelta(days=1)
        end = start + timedelta(days=horizon_days)
        dep = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "=", direct.id),
            ("departure_date", ">=", start.strftime("%Y-%m-%d")),
            ("departure_date", "<=", end.strftime("%Y-%m-%d")),
            ("active", "=", True),
            ("status", "not in", ("cancelled", "completed")),
            ("vehicle_id", "!=", False),
        ], order="departure_date", limit=1)
        if not dep:
            return None
        return datetime.strptime(str(dep.departure_date)[:10], "%Y-%m-%d")

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
                   requested_departure_id=None, pickup_stop=None,
                   delivery_stop=None, prior_day_offset=0):
        """Build a ProposedLeg with corridor, departure, distance, and price.

        A leg is feasible ONLY with an actual scheduled departure row
        (active, not cancelled/completed, with a vehicle assigned). The
        corridor's operating-day checkbox alone is never a departure.

        When the calendar sent a requested_departure_id (so the quote binds
        to the exact departure the customer selected), it is server-
        re-validated here: it must exist, belong to this corridor, be on the
        LINEHAUL date (the departure's own date — for prior-day pickup this
        is the day AFTER the physical pickup), and be active/viable. Any
        mismatch → None — an arbitrary portal-supplied departure id is
        never trusted.

        prior_day_offset: days between the physical pickup and the linehaul
        departure (0 = same-day service, existing behavior). The corridor
        and departure are selected on the LINEHAUL day/date; pickup_date /
        pickup_day stay the physical collection day. The corridor must
        allow prior-day pickup at this offset (allow_prior_day_pickup +
        prior_day_pickup_max_days) — never silently substituted."""
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

        # Prior-day pickup: the corridor + departure are selected on the
        # LINEHAUL day/date; pickup_date stays the physical collection day.
        if not hasattr(pickup_date, "strftime"):
            pickup_date = datetime.strptime(str(pickup_date)[:10], "%Y-%m-%d")
        linehaul_date = pickup_date
        linehaul_day = pickup_day
        if prior_day_offset:
            linehaul_date = pickup_date + timedelta(days=prior_day_offset)
            linehaul_day = linehaul_date.strftime("%A").lower()

        # Find corridor serving this origin→dest. Direction compatibility is
        # checked BEFORE day availability: a reverse-direction corridor must
        # never be substituted merely because it operates on the requested
        # day. The operating-day field is the LINEHAUL day when the pickup
        # happens earlier (Sunday pickup → corridor must operate Monday).
        day_field = f"operate_{linehaul_day.lower()}"
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
        # Pick the FIRST corridor that can actually carry the lane end to
        # end: direction compatibility, prior-day allowance, an exact
        # scheduled departure on the linehaul date, and a valid span must
        # ALL hold. A corridor that passes direction/day but fails the
        # departure or span check is skipped — never a hard failure (a
        # later, higher-id corridor may be the true service). Candidates
        # are ordered by id; the lowest-id fully-valid corridor wins, so
        # precedence is deterministic across calendar, Get Price, and the
        # booking engine.
        from .departure_span_validator import DepartureSpanValidator
        date_str = linehaul_date.strftime("%Y-%m-%d")
        corridor = False
        departure = False
        for candidate in candidates:
            if not self._directionally_compatible(
                    candidate, origin_region, dest_region):
                continue
            if prior_day_offset and not (
                    candidate.allow_prior_day_pickup
                    and prior_day_offset <= (candidate.prior_day_pickup_max_days or 0)):
                # This corridor does not permit prior-day pickup at the
                # offset — try the next candidate rather than silently
                # moving the pickup.
                continue
            # Find departure — exact match on corridor + LINEHAUL date,
            # and ONLY a real scheduled departure (active, not
            # cancelled/completed, vehicle assigned) makes the leg
            # feasible.
            dep = False
            if requested_departure_id:
                # Server-side re-validation of the departure the customer
                # selected on the calendar: must EXIST, belong to THIS
                # corridor, be on the LINEHAUL date, and be active/viable.
                # An arbitrary or stale portal-supplied id falls through
                # to the corridor's own exact-date departure — never a
                # different date or corridor.
                requested = Departure.browse(int(requested_departure_id)).exists()
                if (requested
                        and requested.corridor_id.id == candidate.id
                        and str(requested.departure_date)[:10] == date_str
                        and requested.active
                        and requested.status not in ("cancelled", "completed")
                        and requested.vehicle_id):
                    dep = requested
            if not dep:
                dep = Departure.search([
                    ("corridor_id", "=", candidate.id),
                    ("departure_date", "=", date_str),
                    ("active", "=", True),
                    ("status", "not in", ("cancelled", "completed")),
                    ("vehicle_id", "!=", False),
                ], limit=1)
            if not dep:
                # No actual scheduled departure on this date — the
                # candidate is not feasible even if it operates today.
                # The calendar must never show a date the truck is not
                # scheduled.
                continue
            span = DepartureSpanValidator(self.env).validate(
                dep, origin_region, dest_region,
            )
            if not span["valid"]:
                continue
            corridor = candidate
            departure = dep
            break
        if not corridor:
            # No directionally-compatible corridor with an actual
            # scheduled departure can carry this lane on the linehaul
            # day. Never fall back to a reverse-direction corridor or a
            # corridor-less synthetic leg.
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

        # Pricing — the canonical weight-aware calculator (PricingService.
        # calculate_leg_per_km) is the ONE LTL pricing authority shared by
        # calendar preview, portal Get Price, phone/internal booking,
        # custom quote, recurring booking, pricing session and final
        # booking. Config comes from the corridor record: rate_per_km,
        # planned_pallets, included_weight_per_pallet and the configured
        # excess-weight $/lb (corridor override → global Dispatch Settings
        # default). Never a hardcoded rate; never a second pricing engine.
        from ..services.pricing_service import PricingService

        rate_per_km = corridor.rate_per_km if corridor else 4.0
        planned_pallets = corridor.planned_pallets if corridor else 8
        included_weight = corridor.included_weight_per_pallet if corridor else 500.0
        excess_rate = corridor.excess_weight_rate_per_lb if corridor else 0.0
        if not excess_rate:
            excess_rate = float(
                self.env["ir.config_parameter"].sudo().get_param(
                    "logistics.default_excess_weight_rate", "0.0") or 0.0
            )
        pricing = PricingService.calculate_leg_per_km(
            est_km, rate_per_km, max(planned_pallets, 1), pallets,
            max(included_weight, 1.0), weight_lbs,
            currency=corridor.currency_id if corridor else None,
            excess_weight_rate_per_lb=excess_rate or None,
        )
        pallet_rate = pricing["pallet_rate_per_km"]
        leg_price = pricing["subtotal"]

        # Real pickup / delivery times from the configured corridor stop
        # times (never a hardcoded 8 AM), with the corridor's departure time
        # and the canonical travel calculation as identified fallbacks.
        # When the corridor stop has no planned time, the customer-facing
        # ETA comes from the facility's own hours (ItineraryPlanner) — the
        # SAME authority the calendar probes through this same method.
        # Real pickup / delivery times: the pickup anchors on the PHYSICAL
        # pickup day (Sunday), the corridor departure + delivery on the
        # linehaul day (Monday) — same-day service collapses both.
        timings = self._leg_timings(
            corridor, departure, origin_region, dest_region, est_km,
            pickup_date,
            pickup_stop=pickup_stop, delivery_stop=delivery_stop,
            linehaul_date=linehaul_date if prior_day_offset else None,
        )

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
            pricing_formula=pricing,
            pickup_date=self._date_part(timings["pickup_datetime"]) or date_str,
            prior_day_pickup=bool(prior_day_offset),
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

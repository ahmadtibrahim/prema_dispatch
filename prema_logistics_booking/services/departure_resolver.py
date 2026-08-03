"""DepartureResolver — the SOLE authority for turning a commercially-resolved
route leg (an origin logistics.region and a destination logistics.region,
already priced via RouteResolver/PricingService) into an exact, dated,
capacity-validated logistics.corridor.departure.

No other module may search logistics.corridor.departure for quoting or
booking purposes. ScheduledAvailabilityService (calendar search) and
BookingOrchestrationService (confirmation) must both call this resolver so
that "what we quoted" and "what we reserve" are always the same departure.

Rules enforced here (see PREMA_DISPATCH_MASTER.md / unification brief §5, §10):
  - A departure with no assigned vehicle is never eligible — no 12-pallet /
    11,000 lb fallback is ever substituted for a missing vehicle.
  - Capacity is evaluated against the ACTUAL vehicle_id's fields via
    CapacityEngine, per departure, per leg, independently.
  - Dry/Reefer compatibility is evaluated against the actual vehicle
    (vehicle.x_reefer), never a hardcoded truck-type assumption.
  - Direct route = a single corridor whose stop sequence visits the origin
    region before the destination region (pickup/delivery allowed there).
  - Transfer route = leg 1 (origin region -> hub region) and leg 2 (hub
    region -> destination region), each resolved independently, with leg 2
    required to depart no earlier than leg 1's arrival at the hub plus the
    hub's configured transfer_cutoff_time — never a hardcoded "+1 day".
  - Departure candidates are chosen deterministically: earliest eligible
    departure_date (then lowest id), never an unordered/unqualified
    search(limit=1).
"""
import datetime
import logging

from .temperature_compat import to_canonical_temperature_mode, REEFER

_logger = logging.getLogger(__name__)

MAX_LOOKAHEAD_DAYS = 45


class ResolvedLeg:
    """One physical, capacity-validated operational leg."""

    def __init__(self, departure, vehicle, origin_region, dest_region, hub=None):
        self.departure = departure
        self.vehicle = vehicle
        self.origin_region = origin_region
        self.dest_region = dest_region
        self.hub = hub  # set only when this leg's destination/origin is the transfer hub


class DepartureResolution:
    def __init__(self, available, reason=None, legs=None):
        self.available = available
        self.reason = reason
        self.legs = legs or []  # list[ResolvedLeg], in travel order


class DepartureResolver:
    def __init__(self, env):
        self.env = env(su=True)

    # ── Public API ────────────────────────────────────────────────────

    def resolve(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                earliest_pickup_date=None, allow_pinwheel_override=False):
        """Resolve exact departure(s) for one commercial shipment leg-pair.
        Mirrors RouteResolver's own direct-then-hub-transfer priority so the
        two never disagree about topology."""
        equipment = to_canonical_temperature_mode(equipment)
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()

        direct = self._resolve_direct(
            origin_region, dest_region, equipment, pallets, weight_lbs,
            earliest_pickup_date, allow_pinwheel_override,
        )
        if direct.available:
            return direct

        transfer = self._resolve_transfer(
            origin_region, dest_region, equipment, pallets, weight_lbs,
            earliest_pickup_date, allow_pinwheel_override,
        )
        if transfer.available:
            return transfer

        return DepartureResolution(False, reason=direct.reason or transfer.reason or "no_departure_available")

    # ── Single leg (boundary already known, e.g. from RouteResolver) ───

    def resolve_single_leg(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                            earliest_pickup_date=None, allow_pinwheel_override=False):
        """Resolve exactly one physical leg between two already-determined
        regions. Used when the caller (e.g. PricingService, which already
        knows the commercial leg boundaries from RouteResolver — direct or
        one hop of a hub transfer) has already decided the topology and only
        needs the exact departure for this specific origin/destination pair."""
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()
        equipment = to_canonical_temperature_mode(equipment)
        return self._resolve_direct(
            origin_region, dest_region, equipment, pallets, weight_lbs,
            earliest_pickup_date, allow_pinwheel_override,
        )

    # ── Direct ────────────────────────────────────────────────────────

    def _resolve_direct(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                         earliest_pickup_date, allow_pinwheel_override):
        corridors = self._candidate_corridors(origin_region, dest_region)
        if not corridors:
            return DepartureResolution(False, reason="no_corridor_for_regions")

        dep, vehicle, reason = self._find_eligible_departure(
            corridors, earliest_pickup_date, equipment, pallets, weight_lbs, allow_pinwheel_override,
        )
        if not dep:
            return DepartureResolution(False, reason=reason)

        return DepartureResolution(True, legs=[
            ResolvedLeg(dep, vehicle, origin_region, dest_region),
        ])

    # ── Hub transfer ──────────────────────────────────────────────────

    def _resolve_transfer(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                           earliest_pickup_date, allow_pinwheel_override):
        Hub = self.env["logistics.hub"]
        hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub or not hub.canonical_region_id:
            return DepartureResolution(False, reason="no_default_hub_configured")
        hub_region = hub.canonical_region_id
        if origin_region == hub_region or dest_region == hub_region:
            # Not a transfer case — direct resolution already covers travel
            # through/ending at the hub region itself.
            return DepartureResolution(False, reason="not_a_transfer_case")

        leg1_corridors = self._candidate_corridors(origin_region, hub_region)
        if not leg1_corridors:
            return DepartureResolution(False, reason="no_corridor_to_hub")

        dep1, vehicle1, reason1 = self._find_eligible_departure(
            leg1_corridors, earliest_pickup_date, equipment, pallets, weight_lbs, allow_pinwheel_override,
        )
        if not dep1:
            return DepartureResolution(False, reason=reason1)

        # Leg 2 must depart no earlier than leg 1's arrival at the hub, plus
        # the hub's configured connection time — computed from the ACTUAL
        # scheduled leg-1 departure, never a hardcoded "+1 day".
        leg2_earliest = self._earliest_connecting_date(dep1, hub)

        leg2_corridors = self._candidate_corridors(hub_region, dest_region)
        if not leg2_corridors:
            return DepartureResolution(False, reason="no_corridor_from_hub")

        dep2, vehicle2, reason2 = self._find_eligible_departure(
            leg2_corridors, leg2_earliest, equipment, pallets, weight_lbs, allow_pinwheel_override,
        )
        if not dep2:
            return DepartureResolution(False, reason=reason2)

        return DepartureResolution(True, legs=[
            ResolvedLeg(dep1, vehicle1, origin_region, hub_region, hub=hub),
            ResolvedLeg(dep2, vehicle2, hub_region, dest_region, hub=hub),
        ])

    def earliest_connecting_date(self, dep1, hub=None):
        """Public wrapper: earliest date a second leg may depart, given the
        actual first-leg departure. Looks up the default hub if none given."""
        if hub is None:
            hub = self.env["logistics.hub"].search([("is_default", "=", True), ("active", "=", True)], limit=1)
        return self._earliest_connecting_date(dep1, hub)

    def _earliest_connecting_date(self, dep1, hub):
        """The earliest calendar date leg 2 may depart, given leg 1's actual
        scheduled arrival and the hub's transfer cutoff. If leg 1 arrives at
        or before cutoff, a same-day connection is allowed; otherwise the
        connection is only valid from the next day onward."""
        cutoff = hub.transfer_cutoff_time or 16.0
        # Departure records only carry a departure_time (not a distinct
        # arrival_time); treat departure_time as the hub-arrival proxy for
        # single-day corridor runs, which matches how corridor.departure is
        # actually populated (no separate arrival column exists yet).
        if (dep1.departure_time or 0.0) <= cutoff:
            return dep1.departure_date
        return dep1.departure_date + datetime.timedelta(days=1)

    # ── Corridor + departure search ──────────────────────────────────

    def _candidate_corridors(self, origin_region, dest_region):
        """Corridors whose stop sequence visits origin before destination,
        with pickup/delivery allowed at those exact stops."""
        Corridor = self.env["logistics.corridor"]
        candidates = []
        for corridor in Corridor.search([("active", "=", True)]):
            stops = corridor.stop_ids.sorted("sequence")
            origin_idx = dest_idx = None
            for i, stop in enumerate(stops):
                if stop.region_id == origin_region and stop.pickup_allowed and origin_idx is None:
                    origin_idx = i
                if stop.region_id == dest_region and stop.delivery_allowed:
                    dest_idx = i
            if origin_idx is not None and dest_idx is not None and dest_idx > origin_idx:
                candidates.append(corridor)
        return candidates

    def _find_eligible_departure(self, corridors, earliest_date, equipment, pallets, weight_lbs,
                                  allow_pinwheel_override):
        """Deterministic search: earliest eligible departure_date, then
        lowest id, across all candidate corridors. Never limit(1) without
        this explicit ordering + eligibility filter."""
        Departure = self.env["logistics.corridor.departure"]
        horizon_end = earliest_date + datetime.timedelta(days=MAX_LOOKAHEAD_DAYS)
        deps = Departure.search([
            ("corridor_id", "in", [c.id for c in corridors]),
            ("departure_date", ">=", earliest_date),
            ("departure_date", "<=", horizon_end),
            ("status", "=", "scheduled"),
            ("active", "=", True),
        ], order="departure_date asc, id asc")

        last_reason = "no_scheduled_departure_in_window"
        for dep in deps:
            eligible, reason, vehicle = self._eligible_departure(
                dep, equipment, pallets, weight_lbs, allow_pinwheel_override,
            )
            if eligible:
                return dep, vehicle, None
            last_reason = reason
        return None, None, last_reason

    def _eligible_departure(self, departure, equipment, pallets, weight_lbs, allow_pinwheel_override):
        """A departure is eligible ONLY with a real, capable, sufficiently
        free vehicle. No capacity number is ever fabricated."""
        vehicle = departure.vehicle_id
        if not vehicle:
            return False, "no_vehicle_assigned", None
        if not vehicle.x_operational_logistics:
            return False, "vehicle_not_operational", None

        from .temperature_compat import vehicle_accepts
        if not vehicle_accepts(vehicle_is_reefer=bool(vehicle.x_reefer), requested_mode=equipment):
            return False, "temperature_incompatible", None

        straight = vehicle.straight_pallet_capacity or 0
        pinwheel = vehicle.pin_wheel_pallet_capacity or 0
        payload = vehicle.x_max_payload_lbs or 0.0
        if not straight or not pinwheel or not payload:
            return False, "vehicle_capacity_not_configured", None

        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        peak = engine.compute_departure_peak(departure)
        projected_pallets = peak["peak_pallets"] + pallets
        projected_weight = peak["peak_weight"] + weight_lbs

        if projected_weight > payload:
            return False, "payload_exceeded", None
        if projected_pallets > pinwheel:
            return False, "pallet_capacity_exceeded", None
        if projected_pallets > straight and not allow_pinwheel_override:
            return False, "pinwheel_override_required", None

        return True, None, vehicle

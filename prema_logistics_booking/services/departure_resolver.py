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

MAX_LOOKAHEAD_DAYS = 56


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
        hubs = self.env["logistics.hub"].search([
            ("active", "=", True), ("canonical_region_id", "!=", False),
        ], order="is_default desc, id asc")
        if not hubs:
            return DepartureResolution(False, reason="no_hub_configured")

        candidates = []
        last_reason = "no_complete_hub_connection"
        for hub in hubs:
            hub_region = hub.canonical_region_id
            if origin_region == hub_region or dest_region == hub_region:
                continue
            leg1_corridors = self._candidate_corridors(origin_region, hub_region)
            leg2_corridors = self._candidate_corridors(hub_region, dest_region)
            if not leg1_corridors or not leg2_corridors:
                continue

            for dep1, vehicle1 in self._eligible_departures(
                leg1_corridors, earliest_pickup_date, equipment, pallets,
                weight_lbs, allow_pinwheel_override,
            ):
                # The connection date is based on this corridor segment's
                # configured arrival day/time at the hub, not merely the
                # departure's start time.
                leg2_earliest = self._earliest_connecting_date(
                    dep1, hub, origin_region=origin_region, dest_region=hub_region,
                )
                dep2, vehicle2, reason2 = self._find_eligible_departure(
                    leg2_corridors, leg2_earliest, equipment, pallets,
                    weight_lbs, allow_pinwheel_override,
                )
                if not dep2:
                    last_reason = reason2 or last_reason
                    continue
                candidates.append((
                    dep2.departure_date,
                    dep1.departure_date,
                    not hub.is_default,
                    hub.id,
                    dep1.id,
                    dep2.id,
                    hub,
                    dep1,
                    vehicle1,
                    dep2,
                    vehicle2,
                ))
                # Later first-leg departures for this hub cannot produce an
                # earlier complete itinerary than its first valid connection.
                break

        if not candidates:
            return DepartureResolution(False, reason=last_reason)

        (_delivery_date, _pickup_date, _non_default, _hub_id, _dep1_id, _dep2_id,
         hub, dep1, vehicle1, dep2, vehicle2) = min(candidates)
        hub_region = hub.canonical_region_id
        return DepartureResolution(True, legs=[
            ResolvedLeg(dep1, vehicle1, origin_region, hub_region, hub=hub),
            ResolvedLeg(dep2, vehicle2, hub_region, dest_region, hub=hub),
        ])

    def earliest_connecting_date(self, dep1, hub=None, origin_region=None, dest_region=None):
        """Public wrapper: earliest date a second leg may depart, given the
        actual first-leg departure. Looks up the default hub if none given."""
        if hub is None:
            hub = self.env["logistics.hub"].search([("is_default", "=", True), ("active", "=", True)], limit=1)
        return self._earliest_connecting_date(
            dep1, hub, origin_region=origin_region, dest_region=dest_region,
        )

    def _earliest_connecting_date(self, dep1, hub, origin_region=None, dest_region=None):
        """The earliest calendar date leg 2 may depart, given leg 1's actual
        scheduled arrival and the hub's transfer cutoff. If leg 1 arrives at
        or before cutoff, a same-day connection is allowed; otherwise the
        connection is only valid from the next day onward."""
        cutoff = hub.transfer_cutoff_time or 16.0
        arrival_date = dep1.departure_date
        arrival_hour = dep1.departure_time or 0.0
        if origin_region and dest_region:
            segment = dep1.corridor_id.resolve_region_segment(origin_region, dest_region)
            if segment:
                arrival_date += datetime.timedelta(days=segment["delivery_day_offset"] or 0)
                configured_hour = segment.get("destination_arrival_time")
                if configured_hour is not False and configured_hour is not None:
                    arrival_hour = configured_hour or 0.0
                elif not segment.get("destination_stop"):
                    # A Hub endpoint without a reviewed arrival time must not
                    # invent a same-day connection from the departure time.
                    return arrival_date + datetime.timedelta(days=1)
        if arrival_hour <= cutoff:
            return arrival_date
        return arrival_date + datetime.timedelta(days=1)

    # ── Corridor + departure search ──────────────────────────────────

    def _candidate_corridors(self, origin_region, dest_region):
        """Corridors whose stop sequence visits origin before destination,
        or whose same-day return loop reaches destination after pickup."""
        Corridor = self.env["logistics.corridor"]
        return [
            corridor for corridor in Corridor.search([("active", "=", True)])
            if corridor.resolve_region_segment(origin_region, dest_region)
        ]

    def _find_eligible_departure(self, corridors, earliest_date, equipment, pallets, weight_lbs,
                                  allow_pinwheel_override):
        """Deterministic search: earliest eligible departure_date, then
        lowest id, across all candidate corridors. Never limit(1) without
        this explicit ordering + eligibility filter."""
        last_reason = "no_scheduled_departure_in_window"
        for dep, vehicle in self._eligible_departures(
            corridors, earliest_date, equipment, pallets, weight_lbs,
            allow_pinwheel_override,
        ):
            return dep, vehicle, None

        # Re-run only to preserve the most useful rejection reason for the UI.
        Departure = self.env["logistics.corridor.departure"]
        horizon_end = earliest_date + datetime.timedelta(days=MAX_LOOKAHEAD_DAYS)
        deps = Departure.search([
            ("corridor_id", "in", [c.id for c in corridors]),
            ("departure_date", ">=", earliest_date),
            ("departure_date", "<=", horizon_end),
            ("status", "=", "scheduled"),
            ("active", "=", True),
        ], order="departure_date asc, id asc")

        for dep in deps:
            eligible, reason, _vehicle = self._eligible_departure(
                dep, equipment, pallets, weight_lbs, allow_pinwheel_override,
            )
            if not eligible:
                last_reason = reason
        return None, None, last_reason

    def _eligible_departures(self, corridors, earliest_date, equipment, pallets,
                             weight_lbs, allow_pinwheel_override):
        """Yield all eligible departures in deterministic chronological order."""
        if not corridors:
            return
        horizon_end = earliest_date + datetime.timedelta(days=MAX_LOOKAHEAD_DAYS)
        departures = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "in", [corridor.id for corridor in corridors]),
            ("departure_date", ">=", earliest_date),
            ("departure_date", "<=", horizon_end),
            ("status", "=", "scheduled"),
            ("active", "=", True),
        ], order="departure_date asc, id asc")
        for departure in departures:
            eligible, _reason, vehicle = self._eligible_departure(
                departure, equipment, pallets, weight_lbs, allow_pinwheel_override,
            )
            if eligible:
                yield departure, vehicle

    def evaluate_departure(self, departure, equipment, pallets, weight_lbs,
                           allow_pinwheel_override=False):
        """Public wrapper: is THIS exact departure eligible for the request?

        Returns (eligible: bool, reason: str|None, vehicle: recordset).
        Used by the calendar path so a date is offered only when the exact
        scheduled departure on that corridor is real, equipped, and has
        capacity — the same authority the quote path uses."""
        if not departure:
            return False, "no_departure", None
        return self._eligible_departure(
            departure, equipment, pallets, weight_lbs, allow_pinwheel_override)

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

        # Canonical dynamic capacity: the assigned vehicle's active pallet
        # layouts decide whether the projected total fits, and pinwheel (or
        # any future layout) is selected automatically — nothing hardcoded.
        from .vehicle_capacity_service import VehicleCapacityService
        capacity = VehicleCapacityService(self.env)
        payload = vehicle.x_max_payload_lbs or 0.0
        if capacity.maximum_capacity(vehicle) <= 0 or not payload:
            return False, "vehicle_capacity_not_configured", None

        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        peak = engine.compute_departure_peak(departure)
        projected_pallets = peak["peak_pallets"] + pallets
        projected_weight = peak["peak_weight"] + weight_lbs

        if projected_weight > payload:
            return False, "payload_exceeded", None
        valid, _layout = capacity.select_layout(vehicle, projected_pallets)
        if not valid:
            return False, "pallet_capacity_exceeded", None

        return True, None, vehicle

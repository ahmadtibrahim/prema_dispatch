"""Exact departure resolver for already-configured Service Route topology.

RouteResolver decides topology first (direct or explicitly connected hub
transfer).  DepartureResolver then dates that exact topology and validates the
actual assigned vehicle/capacity.  It must never discover a different hub path
on its own.
"""
import datetime

from .temperature_compat import to_canonical_temperature_mode

MAX_LOOKAHEAD_DAYS = 56


class ResolvedLeg:
    def __init__(self, departure, vehicle, origin_region, dest_region, hub=None):
        self.departure = departure
        self.vehicle = vehicle
        self.origin_region = origin_region
        self.dest_region = dest_region
        self.hub = hub


class DepartureResolution:
    def __init__(self, available, reason=None, legs=None):
        self.available = available
        self.reason = reason
        self.legs = legs or []


class DepartureResolver:
    def __init__(self, env):
        self.env = env(su=True)

    def resolve(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                earliest_pickup_date=None, allow_pinwheel_override=False):
        equipment = to_canonical_temperature_mode(equipment)
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()

        from .route_resolver import RouteResolver
        topology = RouteResolver(self.env).resolve_regions(origin_region, dest_region)
        if not topology.available:
            return DepartureResolution(False, reason=topology.reason)

        if len(topology.legs) == 1:
            leg = topology.legs[0]
            corridor = self.env["logistics.corridor"].browse(leg["corridor_id"])
            dep, vehicle, reason = self._find_eligible_departure(
                corridor, earliest_pickup_date, equipment, pallets, weight_lbs,
                allow_pinwheel_override,
            )
            if not dep:
                return DepartureResolution(False, reason=reason)
            return DepartureResolution(True, legs=[
                ResolvedLeg(dep, vehicle, origin_region, dest_region),
            ])

        if len(topology.legs) != 2:
            return DepartureResolution(False, reason="unsupported_topology_leg_count")

        first = topology.legs[0]
        second = topology.legs[1]
        hub_id = first.get("hub_id") or second.get("hub_id")
        hub = self.env["logistics.hub"].browse(hub_id).exists()
        if not hub:
            return DepartureResolution(False, reason="transfer_hub_not_configured")

        first_corridor = self.env["logistics.corridor"].browse(first["corridor_id"])
        second_corridor = self.env["logistics.corridor"].browse(second["corridor_id"])
        hub_region = hub.canonical_region_id
        if not first_corridor or not second_corridor or not hub_region:
            return DepartureResolution(False, reason="configured_connection_incomplete")

        last_reason = "no_complete_hub_connection"
        candidates = []
        for dep1, vehicle1 in self._eligible_departures(
            first_corridor, earliest_pickup_date, equipment, pallets,
            weight_lbs, allow_pinwheel_override,
        ):
            leg2_earliest = self._earliest_connecting_date(
                dep1, hub, origin_region=origin_region, dest_region=hub_region,
            )
            dep2, vehicle2, reason2 = self._find_eligible_departure(
                second_corridor, leg2_earliest, equipment, pallets,
                weight_lbs, allow_pinwheel_override,
            )
            if not dep2:
                last_reason = reason2 or last_reason
                continue
            candidates.append((
                dep2.departure_date, dep1.departure_date,
                dep1.id, dep2.id, dep1, vehicle1, dep2, vehicle2,
            ))
            break

        if not candidates:
            return DepartureResolution(False, reason=last_reason)

        _delivery, _pickup, _d1, _d2, dep1, vehicle1, dep2, vehicle2 = min(candidates)
        return DepartureResolution(True, legs=[
            ResolvedLeg(dep1, vehicle1, origin_region, hub_region, hub=hub),
            ResolvedLeg(dep2, vehicle2, hub_region, dest_region, hub=hub),
        ])

    def resolve_single_leg(self, origin_region, dest_region, equipment, pallets, weight_lbs,
                            earliest_pickup_date=None, allow_pinwheel_override=False):
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()
        equipment = to_canonical_temperature_mode(equipment)
        corridors = self._candidate_corridors(origin_region, dest_region)
        dep, vehicle, reason = self._find_eligible_departure(
            corridors, earliest_pickup_date, equipment, pallets, weight_lbs,
            allow_pinwheel_override,
        )
        if not dep:
            return DepartureResolution(False, reason=reason)
        return DepartureResolution(True, legs=[
            ResolvedLeg(dep, vehicle, origin_region, dest_region),
        ])

    def earliest_connecting_date(self, dep1, hub=None, origin_region=None, dest_region=None):
        if hub is None:
            hub = self.env["logistics.hub"].search([
                ("is_default", "=", True), ("active", "=", True)
            ], limit=1)
        return self._earliest_connecting_date(
            dep1, hub, origin_region=origin_region, dest_region=dest_region,
        )

    def _earliest_connecting_date(self, dep1, hub, origin_region=None, dest_region=None):
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
                    return arrival_date + datetime.timedelta(days=1)
        return arrival_date if arrival_hour <= cutoff else arrival_date + datetime.timedelta(days=1)

    def _candidate_corridors(self, origin_region, dest_region):
        return self.env["logistics.corridor"].search([("active", "=", True)]).filtered(
            lambda corridor: bool(corridor.resolve_region_segment(origin_region, dest_region))
        )

    def _find_eligible_departure(self, corridors, earliest_date, equipment, pallets, weight_lbs,
                                  allow_pinwheel_override):
        corridors = corridors if hasattr(corridors, "ids") else self.env["logistics.corridor"].browse([
            c.id for c in corridors
        ])
        last_reason = "no_scheduled_departure_in_window"
        for dep, vehicle in self._eligible_departures(
            corridors, earliest_date, equipment, pallets, weight_lbs,
            allow_pinwheel_override,
        ):
            return dep, vehicle, None

        horizon_end = earliest_date + datetime.timedelta(days=MAX_LOOKAHEAD_DAYS)
        deps = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "in", corridors.ids),
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
        if not corridors:
            return
        corridor_ids = corridors.ids if hasattr(corridors, "ids") else [c.id for c in corridors]
        horizon_end = earliest_date + datetime.timedelta(days=MAX_LOOKAHEAD_DAYS)
        departures = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "in", corridor_ids),
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

    def _eligible_departure(self, departure, equipment, pallets, weight_lbs,
                            allow_pinwheel_override):
        vehicle = departure.vehicle_id
        if not vehicle:
            return False, "no_vehicle_assigned", None
        if not vehicle.x_operational_logistics:
            return False, "vehicle_not_operational", None

        from .temperature_compat import vehicle_accepts
        if not vehicle_accepts(bool(vehicle.x_reefer), equipment):
            return False, "temperature_incompatible", None

        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        straight, pinwheel, _turned, payload = engine._vehicle_capacities(vehicle)
        if not straight or not pinwheel or not payload:
            return False, "vehicle_capacity_not_configured", None

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

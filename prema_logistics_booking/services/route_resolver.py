"""Resolve configured corridor topology without creating a second route authority.

Corridors own movement, stop order, direction, distance and customer $/km
pricing.  Technical ``logistics.lane`` and historical Rate Plans remain in
the database only so old bookings keep their references; new resolution does
not read them.
"""
from collections import namedtuple

from .temperature_compat import DRY, REEFER, to_canonical_temperature_mode


ResolvedRoute = namedtuple(
    "ResolvedRoute", ["available", "reason", "legs", "total_pallets", "total_weight_lbs"]
)

VALID_SHIPMENT_TYPES = {"ltl", "ftl"}
VALID_EQUIPMENT = {DRY, REEFER}
LEGACY_CHILLED_FROZEN = {"chilled", "frozen"}


class RouteResolver:
    """Find a direct corridor segment or a proven two-leg hub connection."""

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def resolve(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment="dry", partner=None, shipment_type="ltl", reference_dt=None):
        del partner, reference_dt  # Corridor pricing is intentionally customer-independent.
        equipment = to_canonical_temperature_mode(equipment)
        if shipment_type not in VALID_SHIPMENT_TYPES:
            return ResolvedRoute(False, "invalid_shipment_type", [], pallets, weight_lbs)
        if equipment not in VALID_EQUIPMENT:
            return ResolvedRoute(False, "invalid_equipment", [], pallets, weight_lbs)
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return ResolvedRoute(False, "pickup_fsa_not_supported", [], pallets, weight_lbs)
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return ResolvedRoute(False, "delivery_fsa_not_supported", [], pallets, weight_lbs)
        origin = pickup_fsa.region_id
        destination = delivery_fsa.region_id
        if not origin or not destination:
            return ResolvedRoute(False, "fsa_not_mapped_to_region", [], pallets, weight_lbs)
        # FSA rows point at the legacy lane regions (ids 1-20); corridors
        # and hubs are keyed by the official LTL regions (142+). Canonicalize
        # through the same bridge the coordinate path uses.
        from .region_resolver import RegionResolver
        region_resolver = RegionResolver(self.env)
        origin = region_resolver.canonical_region(origin)
        destination = region_resolver.canonical_region(destination)
        if not origin or not destination:
            return ResolvedRoute(False, "fsa_not_mapped_to_region", [], pallets, weight_lbs)

        direct = self.resolve_regions(origin, destination)
        if not direct.available:
            return ResolvedRoute(False, direct.reason, [], pallets, weight_lbs)
        return ResolvedRoute(True, None, direct.legs, pallets, weight_lbs)

    def resolve_regions(self, origin_region, destination_region):
        """Topology-only resolution used by pricing, departures and the map."""
        direct = self._best_direct_segment(origin_region, destination_region)
        if direct:
            return ResolvedRoute(True, None, [self._leg_dict(direct)], 0, 0.0)

        transfer_candidates = []
        hubs = self.env["logistics.hub"].search([
            ("active", "=", True),
            ("canonical_region_id", "!=", False),
        ], order="is_default desc, id asc")
        for hub in hubs:
            hub_region = hub.canonical_region_id
            if origin_region == hub_region or destination_region == hub_region:
                continue
            first = self._best_direct_segment(origin_region, hub_region)
            second = self._best_direct_segment(hub_region, destination_region)
            if first and second:
                transfer_candidates.append((first["distance_km"] + second["distance_km"], hub, first, second))
        if not transfer_candidates:
            return ResolvedRoute(False, "no_configured_corridor", [], 0, 0.0)

        _, hub, first, second = min(
            transfer_candidates,
            key=lambda candidate: (candidate[0], not candidate[1].is_default, candidate[1].id),
        )
        return ResolvedRoute(True, None, [
            self._leg_dict(first, hub=hub),
            self._leg_dict(second, hub=hub),
        ], 0, 0.0)

    def configured_destinations(self, origin_region):
        """All regions reachable from an origin, regardless of live capacity."""
        destinations = self.env["logistics.region"]
        for region in self.env["logistics.region"].search([("active", "=", True)]):
            if region != origin_region and self.resolve_regions(origin_region, region).available:
                destinations |= region
        return destinations

    def _candidate_segments(self, origin_region, destination_region):
        segments = []
        corridors = self.env["logistics.corridor"].search([("active", "=", True)])
        for corridor in corridors:
            segment = corridor.resolve_region_segment(origin_region, destination_region)
            if not segment:
                continue
            # Allow intra-region (distance 0) segments for local corridors.
            if origin_region == destination_region or (segment.get("distance_km") or 0) > 0:
                segments.append(segment)
        return segments

    def _best_direct_segment(self, origin_region, destination_region):
        segments = self._candidate_segments(origin_region, destination_region)
        if not segments:
            return False
        return min(segments, key=lambda segment: (
            segment["delivery_day_offset"] - segment["pickup_day_offset"],
            segment["distance_km"],
            segment["corridor"].id,
        ))

    @staticmethod
    def _leg_dict(segment, hub=None):
        corridor = segment["corridor"]
        origin = segment["origin_region"]
        destination = segment["destination_region"]
        return {
            "corridor": corridor,
            "corridor_id": corridor.id,
            "corridor_name": corridor.name,
            "origin_region": origin.code,
            "origin_region_id": origin.id,
            "dest_region": destination.code,
            "dest_region_id": destination.id,
            "distance_km": segment["distance_km"],
            "pickup_day_offset": segment["pickup_day_offset"],
            "delivery_day_offset": segment["delivery_day_offset"],
            "rate_per_km": corridor.rate_per_km,
            "planned_pallets": corridor.planned_pallets,
            "included_weight_per_pallet": corridor.included_weight_per_pallet,
            "minimum_booking_charge": corridor.minimum_booking_charge,
            "currency_id": corridor.currency_id.id,
            "currency_code": corridor.currency_id.name,
            "hub": hub,
            "hub_id": hub.id if hub else False,
            "hub_name": hub.public_name if hub else "",
            "hub_location_id": hub.saved_location_id.id if hub and hub.saved_location_id else False,
            # Compatibility keys intentionally empty for historical columns.
            "lane": False,
            "lane_id": False,
            "offering_id": False,
            "rate_plan": False,
            "rate_plan_id": False,
            "rate_plan_name": "",
            "rate_plan_version": 0,
        }

    def find_rate_plan_for_regions(self, *args, **kwargs):
        """Deprecated compatibility API: new pricing never resolves a Rate Plan."""
        del args, kwargs
        return self.env["logistics.rate.plan"]

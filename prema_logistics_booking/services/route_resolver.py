"""Canonical network topology resolver.

Service Routes (``logistics.corridor``) and their Ordered Regions are the only
live routing authority.  A movement stays on the same truck whenever origin
and destination are valid in travel order.  Hub transfer is considered only
when the two corridor movements are explicitly connected through configured
feeder/final-mile policy; the resolver never invents a transfer merely because
two routes happen to touch the same hub.
"""
from collections import namedtuple

from .temperature_compat import DRY, REEFER, to_canonical_temperature_mode


ResolvedRoute = namedtuple(
    "ResolvedRoute", ["available", "reason", "legs", "total_pallets", "total_weight_lbs"]
)

VALID_SHIPMENT_TYPES = {"ltl", "ftl"}
VALID_EQUIPMENT = {DRY, REEFER}


class RouteResolver:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def resolve(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment="dry", partner=None, shipment_type="ltl", reference_dt=None):
        del partner, reference_dt
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

        direct = self.resolve_regions(origin, destination)
        if not direct.available:
            return ResolvedRoute(False, direct.reason, [], pallets, weight_lbs)
        if shipment_type == "ftl" and len(direct.legs) != 1:
            return ResolvedRoute(False, "ftl_requires_dedicated_direct_service", [], pallets, weight_lbs)
        return ResolvedRoute(True, None, direct.legs, pallets, weight_lbs)

    def resolve_regions(self, origin_region, destination_region):
        """Topology-only resolution: direct first, then explicit hub connection."""
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
            if not first or not second:
                continue
            if not self._connection_allowed(
                first["corridor"], second["corridor"],
                origin_region, destination_region,
            ):
                continue
            transfer_candidates.append((
                first["distance_km"] + second["distance_km"],
                not hub.is_default,
                hub.id,
                first["corridor"].id,
                second["corridor"].id,
                hub, first, second,
            ))

        if not transfer_candidates:
            return ResolvedRoute(False, "no_configured_corridor_connection", [], 0, 0.0)

        *_sort, hub, first, second = min(transfer_candidates)
        return ResolvedRoute(True, None, [
            self._leg_dict(first, hub=hub),
            self._leg_dict(second, hub=hub),
        ], 0, 0.0)

    @staticmethod
    def _connection_allowed(first_corridor, second_corridor, origin_region, destination_region):
        """Require an explicit operational relationship between two movements.

        Supported configuration paths intentionally reuse fields already in the
        database so this refactor is migration-safe:

        * first.feeds_corridor_id == second
        * second mainline enables transit pricing and explicitly allows the
          shipment origin region as a feeder region
        * first mainline enables transit pricing and explicitly allows the
          final destination region for a final-mile connection

        Paired return service by itself is NOT a transfer relationship.
        """
        if first_corridor == second_corridor:
            return False
        if first_corridor.feeds_corridor_id == second_corridor:
            return True
        if (
            second_corridor.enable_transit_pricing
            and origin_region in second_corridor.allowed_feeder_region_ids
        ):
            return True
        if (
            first_corridor.enable_transit_pricing
            and destination_region in first_corridor.allowed_feeder_region_ids
        ):
            return True
        return False

    def configured_destinations(self, origin_region):
        destinations = self.env["logistics.region"]
        for region in self.env["logistics.region"].search([
            ("active", "=", True),
            ("is_official_ltl_region", "=", True),
        ]):
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
            if origin_region == destination_region or (segment.get("distance_km") or 0) > 0:
                segments.append(segment)
        return segments

    def _best_direct_segment(self, origin_region, destination_region):
        segments = self._candidate_segments(origin_region, destination_region)
        if not segments:
            return False
        # Ordered-regions topology decides eligibility.  Once eligible, choose
        # the fastest configured service, then shortest travelled distance.
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
            "lane": False,
            "lane_id": False,
            "offering_id": False,
            "rate_plan": False,
            "rate_plan_id": False,
            "rate_plan_name": "",
            "rate_plan_version": 0,
        }

    def find_rate_plan_for_regions(self, *args, **kwargs):
        del args, kwargs
        return self.env["logistics.rate.plan"]

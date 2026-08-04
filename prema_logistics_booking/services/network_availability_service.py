"""Configured reachability plus optional exact departure availability for the map."""

import datetime

from .departure_resolver import DepartureResolver
from .route_resolver import RouteResolver
from .temperature_compat import to_canonical_temperature_mode


class NetworkAvailabilityService:
    def __init__(self, env):
        self.env = env(su=True)

    @staticmethod
    def _region_point(region):
        return {
            "region_id": region.id,
            "name": region.name,
            "lat": region.marker_latitude,
            "lng": region.marker_longitude,
        }

    @staticmethod
    def _hub_point(hub):
        return {
            "region_id": hub.canonical_region_id.id,
            "name": hub.public_name or hub.name,
            "lat": hub.latitude or hub.saved_location_id.pin_lat or False,
            "lng": hub.longitude or hub.saved_location_id.pin_lng or False,
        }

    def list_destinations_from(self, origin, equipment="dry", earliest_pickup_date=None):
        """Return configured routes even when no departure is currently bookable."""
        origin_region = self._as_region(origin)
        if not origin_region:
            return []
        equipment = to_canonical_temperature_mode(equipment)
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()
        topology_resolver = RouteResolver(self.env)
        departure_resolver = DepartureResolver(self.env)
        selected_origin_hub = origin if origin._name == "logistics.hub" else self.env["logistics.hub"]
        destinations = []

        for destination in self.env["logistics.region"].search([
            ("active", "=", True), ("customer_visible", "=", True),
        ], order="display_sequence, name"):
            if destination == origin_region:
                continue
            topology = topology_resolver.resolve_regions(origin_region, destination)
            if not topology.available:
                continue

            exact = departure_resolver.resolve(
                origin_region, destination, equipment, 1, 1.0,
                earliest_pickup_date=earliest_pickup_date,
            )
            configured_legs = topology.legs
            if exact.available:
                # Use the exact itinerary for both dates and line geometry so
                # a currently unavailable direct service can never be shown
                # as the bookable path when the actual option is via a Hub.
                configured_legs = []
                for exact_leg in exact.legs:
                    segment = exact_leg.departure.corridor_id.resolve_region_segment(
                        exact_leg.origin_region, exact_leg.dest_region,
                    )
                    if segment:
                        configured_legs.append(RouteResolver._leg_dict(
                            segment, hub=exact_leg.hub,
                        ))
            legs = []
            for leg_index, configured_leg in enumerate(configured_legs):
                origin_leg = self.env["logistics.region"].browse(configured_leg["origin_region_id"])
                destination_leg = self.env["logistics.region"].browse(configured_leg["dest_region_id"])
                transfer_hub = self.env["logistics.hub"].browse(
                    configured_leg.get("hub_id") or False
                ).exists()
                exact_leg = exact.legs[leg_index] if exact.available and leg_index < len(exact.legs) else None

                origin_point = self._region_point(origin_leg)
                destination_point = self._region_point(destination_leg)
                if selected_origin_hub and origin_leg == selected_origin_hub.canonical_region_id:
                    origin_point = self._hub_point(selected_origin_hub)
                if transfer_hub:
                    if origin_leg == transfer_hub.canonical_region_id:
                        origin_point = self._hub_point(transfer_hub)
                    if destination_leg == transfer_hub.canonical_region_id:
                        destination_point = self._hub_point(transfer_hub)
                legs.append({
                    "origin_region_id": origin_leg.id,
                    "dest_region_id": destination_leg.id,
                    "corridor_id": configured_leg["corridor_id"],
                    "corridor_name": configured_leg["corridor_name"],
                    "distance_km": configured_leg["distance_km"],
                    "origin": origin_point,
                    "destination": destination_point,
                    "hub_id": configured_leg.get("hub_id") or False,
                    "hub_name": configured_leg.get("hub_name") or "",
                    "departure_id": exact_leg.departure.id if exact_leg else False,
                    "departure_date": str(exact_leg.departure.departure_date) if exact_leg else False,
                    "departure_time": exact_leg.departure.departure_time if exact_leg else False,
                    "vehicle_id": exact_leg.vehicle.id if exact_leg else False,
                })

            destinations.append({
                "region_id": destination.id,
                "region_name": destination.name,
                "main_city": destination.main_city or "",
                "lat": destination.marker_latitude,
                "lng": destination.marker_longitude,
                "status": "direct" if len(legs) == 1 else "hub_transfer",
                "bookable": bool(exact.available and len(legs) == len(exact.legs)),
                "reason": None if exact.available else exact.reason,
                "legs": legs,
            })
        return destinations

    @staticmethod
    def _as_region(origin):
        if origin._name == "logistics.hub":
            return origin.canonical_region_id
        if origin._name == "logistics.region":
            return origin
        return None

"""NetworkAvailabilityService — region-to-region destination discovery for the
Where We Go map. Built entirely on DepartureResolver (the sole exact-departure
authority) so the map can never show a route that real booking couldn't also
find, and never draws a route without proving the full topology (direct
corridor, or both legs of a hub transfer) actually exists.

Deliberately region-level, not FSA-level: PricingService/RouteResolver need a
specific FSA to quote a real customer, but this service answers a coarser
"what can this pickup region reach" question for the network overview map —
using a nominal 1-pallet/1-lb probe purely to exercise capacity/vehicle
eligibility, never to price anything (Rate Plans remain the only pricing
authority; this service returns no price).
"""
import datetime

from .departure_resolver import DepartureResolver
from .temperature_compat import to_canonical_temperature_mode

PROBE_PALLETS = 1
PROBE_WEIGHT_LBS = 1.0


class NetworkAvailabilityService:
    def __init__(self, env):
        self.env = env(su=True)

    def list_destinations_from(self, origin, equipment="dry", earliest_pickup_date=None):
        """origin: a logistics.region OR logistics.hub record (the hub itself
        is a valid selectable pickup — resolved to its canonical region).
        Returns a list of dicts, one per other active/customer-visible
        region, each classified as direct / hub_transfer / unavailable."""
        origin_region = self._as_region(origin)
        if not origin_region:
            return []

        equipment = to_canonical_temperature_mode(equipment)
        earliest_pickup_date = earliest_pickup_date or datetime.date.today()
        resolver = DepartureResolver(self.env)

        Region = self.env["logistics.region"]
        destinations = []
        for dest_region in Region.search([("active", "=", True), ("customer_visible", "=", True)]):
            if dest_region.id == origin_region.id:
                continue

            result = resolver.resolve(
                origin_region, dest_region, equipment,
                PROBE_PALLETS, PROBE_WEIGHT_LBS,
                earliest_pickup_date=earliest_pickup_date,
            )

            if not result.available:
                destinations.append({
                    "region_id": dest_region.id,
                    "region_name": dest_region.name,
                    "status": "unavailable",
                    "reason": result.reason,
                    "legs": [],
                })
                continue

            legs = [{
                "origin_region_id": leg.origin_region.id,
                "dest_region_id": leg.dest_region.id,
                "departure_id": leg.departure.id,
                "departure_date": str(leg.departure.departure_date),
                "vehicle_id": leg.vehicle.id,
                "hub_id": leg.hub.id if leg.hub else False,
            } for leg in result.legs]

            destinations.append({
                "region_id": dest_region.id,
                "region_name": dest_region.name,
                "status": "direct" if len(legs) == 1 else "hub_transfer",
                "reason": None,
                "legs": legs,
            })

        return destinations

    def _as_region(self, origin):
        if origin._name == "logistics.hub":
            return origin.canonical_region_id
        if origin._name == "logistics.region":
            return origin
        return None

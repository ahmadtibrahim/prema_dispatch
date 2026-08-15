"""Compatibility adapter for coordinate-based booking screens.

This class intentionally contains NO independent routing, pricing, capacity,
hub or schedule policy.  It resolves coordinates to official regions/FSA
adapters, then delegates to PricingService -> RouteResolver -> DepartureResolver
-> CapacityEngine.  This removes the historical second booking engine that
could disagree with postal/FSA booking results.
"""
from collections import namedtuple
from datetime import date, datetime, timedelta


ShipmentRoute = namedtuple("ShipmentRoute", [
    "available", "reason", "reason_code", "legs", "total_pallets",
    "total_weight_lbs", "estimated_delivery", "routing_snapshot",
])

ProposedLeg = namedtuple("ProposedLeg", [
    "sequence", "leg_type", "origin_region_code", "dest_region_code",
    "corridor_id", "corridor_name", "departure_id", "departure_date",
    "estimated_distance_km", "estimated_drive_hrs", "rate_per_km",
    "pallet_rate_per_km", "pallets", "leg_price", "transfer_hub_id",
    "hub_ready_at",
])


class ShipmentRoutingService:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def _region_for_point(self, lat, lng):
        from .region_resolver import RegionResolver
        result = RegionResolver(self.env).resolve(float(lat), float(lng))
        if not result.matched_region or result.outcome not in ("SCHEDULED_MATCH",):
            return self.env["logistics.region"]
        return RegionResolver(self.env).canonical_region(result.matched_region)

    def _fsa_adapter(self, region, pickup):
        if not region:
            return self.env["logistics.fsa"]
        domain = [
            ("region_id", "=", region.id),
            ("active", "=", True),
            ("pickup_supported" if pickup else "delivery_supported", "=", True),
        ]
        return self.env["logistics.fsa"].search(domain, order="fsa", limit=1)

    @staticmethod
    def _parse_date(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    def _quote_from_points(self, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                           pallets, weight_lbs, requested_pickup_date=None,
                           equipment="dry"):
        origin = self._region_for_point(pickup_lat, pickup_lng)
        destination = self._region_for_point(delivery_lat, delivery_lng)
        if not origin:
            return None, "MANUAL_QUOTE_PICKUP"
        if not destination:
            return None, "MANUAL_QUOTE_DELIVERY"
        pickup_fsa = self._fsa_adapter(origin, pickup=True)
        delivery_fsa = self._fsa_adapter(destination, pickup=False)
        if not pickup_fsa or not delivery_fsa:
            return None, "FSA_ADAPTER_NOT_CONFIGURED"

        from .pricing_service import PricingService
        requested = self._parse_date(requested_pickup_date) or date.today()
        result = PricingService(self.env).calculate(
            pickup_fsa, delivery_fsa, "ltl", equipment,
            int(pallets or 1), float(weight_lbs or 0.0),
            resolve_departures=True, reference_dt=requested,
            required_temperature_c=15.0 if str(equipment).lower() in ("reefer", "chilled", "frozen") else None,
        )
        return result, None

    def plan_route(self, pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                   pallets=1, weight_lbs=0, requested_pickup_date=None,
                   equipment="dry", pickup_country=None, pickup_state=None,
                   delivery_country=None, delivery_state=None):
        del pickup_country, pickup_state, delivery_country, delivery_state
        result, pre_reason = self._quote_from_points(
            pickup_lat, pickup_lng, delivery_lat, delivery_lng,
            pallets, weight_lbs, requested_pickup_date, equipment,
        )
        if not result:
            return ShipmentRoute(
                False, "Movement is outside the configured scheduled network.",
                pre_reason or "NO_ROUTE", [], pallets, weight_lbs, None, {},
            )
        if not result.available:
            return ShipmentRoute(
                False, result.reason or "No scheduled service available.",
                (result.reason or "NO_ROUTE").upper(), [], pallets, weight_lbs,
                None, getattr(result, "route_snapshot", {}) or {},
            )

        requested = self._parse_date(requested_pickup_date)
        if requested and result.pickup_date != requested:
            return ShipmentRoute(
                False,
                f"Requested pickup date is not served. Next eligible pickup is {result.pickup_date}.",
                "REQUESTED_PICKUP_DATE_NOT_SERVED", [], pallets, weight_lbs,
                None, result.route_snapshot,
            )

        legs = []
        for leg in (result.route_snapshot or {}).get("legs", []):
            formula = leg.get("pricing_formula") or {}
            rate = leg.get("rate_per_km") or 0.0
            target = leg.get("planned_pallets") or 1
            legs.append(ProposedLeg(
                sequence=leg.get("sequence") or len(legs) + 1,
                leg_type=leg.get("leg_role") or "mainline",
                origin_region_code=leg.get("origin_region") or "",
                dest_region_code=leg.get("dest_region") or "",
                corridor_id=leg.get("corridor_id") or None,
                corridor_name=leg.get("corridor_name") or "",
                departure_id=leg.get("departure_id") or None,
                departure_date=leg.get("departure_date") or "",
                estimated_distance_km=leg.get("distance_km") or 0.0,
                estimated_drive_hrs=round((leg.get("distance_km") or 0.0) / 60.0, 2),
                rate_per_km=rate,
                pallet_rate_per_km=formula.get("pallet_rate_per_km") or (rate / target if target else 0.0),
                pallets=int(pallets or 1),
                leg_price=leg.get("price") or 0.0,
                transfer_hub_id=leg.get("hub_id") or None,
                hub_ready_at=None,
            ))

        return ShipmentRoute(
            True,
            f"Route planned from configured Service Routes: {len(legs)} leg(s).",
            "ROUTE_PLANNED",
            legs,
            int(pallets or 1),
            float(weight_lbs or 0.0),
            str(result.delivery_date_estimate) if result.delivery_date_estimate else None,
            result.route_snapshot,
        )

    def get_eligible_pickup_dates(self, pickup_lat, pickup_lng,
                                   delivery_lat, delivery_lng,
                                   pallets=1, weight_lbs=500,
                                   equipment="dry", horizon_weeks=8):
        """Exact customer calendar from the same quote engine used at confirm."""
        origin = self._region_for_point(pickup_lat, pickup_lng)
        destination = self._region_for_point(delivery_lat, delivery_lng)
        pickup_fsa = self._fsa_adapter(origin, pickup=True)
        delivery_fsa = self._fsa_adapter(destination, pickup=False)
        if not pickup_fsa or not delivery_fsa:
            return []

        from .pricing_service import PricingService
        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        pricing = PricingService(self.env)
        eligible = []
        today = date.today()
        horizon_end = today + timedelta(weeks=min(max(int(horizon_weeks or 8), 1), 8))
        current = today

        while current <= horizon_end:
            result = pricing.calculate(
                pickup_fsa, delivery_fsa, "ltl", equipment,
                int(pallets or 1), float(weight_lbs or 0.0),
                resolve_departures=True, reference_dt=current,
                required_temperature_c=15.0 if str(equipment).lower() in ("reefer", "chilled", "frozen") else None,
            )
            # Pricing returns the first departure >= current. Only expose it
            # on its actual pickup date so one departure is not repeated on
            # every preceding calendar day.
            if result.available and result.pickup_date == current:
                snapshots = (result.route_snapshot or {}).get("legs", [])
                dep_ids = [s.get("departure_id") for s in snapshots if s.get("departure_id")]
                departures = self.env["logistics.corridor.departure"].browse(dep_ids).exists()
                remaining_values = []
                capacity_values = []
                for departure in departures:
                    vehicle = departure.vehicle_id
                    if not vehicle:
                        remaining_values = [0]
                        capacity_values = [0]
                        break
                    capacity = engine.vehicle_booking_capacity(vehicle, allow_pinwheel_override=False)
                    peak = engine.compute_departure_peak(departure)
                    capacity_values.append(capacity)
                    remaining_values.append(max(0, capacity - peak["peak_pallets"]))
                remaining = min(remaining_values) if remaining_values else 0
                max_capacity = min(capacity_values) if capacity_values else 0
                if remaining >= int(pallets or 1):
                    names = [s.get("corridor_name") or "" for s in snapshots]
                    eligible.append({
                        "date": current.isoformat(),
                        "day_name": current.strftime("%A"),
                        "feeder_corridor": names[0] if names else "",
                        "onward_corridor": names[1] if len(names) > 1 else "",
                        "departure_date": current.isoformat(),
                        "estimated_delivery": str(result.delivery_date_estimate or ""),
                        "leg_count": len(snapshots),
                        "remaining_capacity": remaining,
                        "max_capacity": max_capacity,
                        "price": result.calculated_price,
                        "pricing_anchor_corridor_id": (result.route_snapshot or {}).get("pricing_anchor_corridor_id"),
                    })
            current += timedelta(days=1)
        return eligible

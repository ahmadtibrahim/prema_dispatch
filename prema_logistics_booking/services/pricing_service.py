"""Corridor-distance pricing for the scheduled LTL network.

The operational Corridor is the single active authority for route topology,
distance, $/km, planned pallets and the booking minimum.  Rate Plans remain
readable only for historical bookings.
"""

import datetime


class PricingResult:
    def __init__(self, available, reason=None, **kwargs):
        self.available = available
        self.reason = reason
        self.lane = kwargs.get("lane")
        self.service_offering = kwargs.get("service_offering")
        self.rate_plan = kwargs.get("rate_plan")
        self.schedule = kwargs.get("schedule")
        self.corridor = kwargs.get("corridor")
        self.pickup_date = kwargs.get("pickup_date")
        self.delivery_date_estimate = kwargs.get("delivery_date_estimate")
        self.price_lines = kwargs.get("price_lines", [])
        self.calculated_price = kwargs.get("calculated_price", 0.0)
        self.route_snapshot = kwargs.get("route_snapshot", {})  # immutable at confirm


class PricingService:
    def __init__(self, env):
        self.env = env(su=True)

    def calculate(self, pickup_fsa, delivery_fsa, shipment_type, temperature_mode,
                   pallets, weight_lbs, liftgate_pickup=False, liftgate_delivery=False,
                   appointment=False, residential=False, same_day_requested=False,
                   partner=None, reference_dt=None, required_temperature_c=None,
                   resolve_departures=False):
        """Price a configured corridor itinerary and optionally freeze departures."""
        # Validate inputs
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return PricingResult(False, reason="pickup_fsa_not_supported")
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return PricingResult(False, reason="delivery_fsa_not_supported")
        if not pickup_fsa.region_id or not delivery_fsa.region_id:
            return PricingResult(False, reason="fsa_not_mapped_to_region")
        if pallets < 1:
            return PricingResult(False, reason="invalid_pallet_count")
        if weight_lbs < 0:
            return PricingResult(False, reason="invalid_weight")

        from .temperature_compat import to_canonical_temperature_mode, REEFER
        equipment = to_canonical_temperature_mode(temperature_mode)
        if equipment == REEFER and required_temperature_c is None:
            return PricingResult(False, reason="required_temperature_c_missing")
        origin_region = pickup_fsa.region_id
        destination_region = delivery_fsa.region_id
        topology_legs = []
        pickup_date = delivery_date = None
        if resolve_departures:
            from .departure_resolver import DepartureResolver
            dep_resolver = DepartureResolver(self.env)
            earliest = (
                reference_dt.date() if hasattr(reference_dt, "date")
                else reference_dt or datetime.date.today()
            )
            resolution = dep_resolver.resolve(
                origin_region, destination_region, equipment, pallets, weight_lbs,
                earliest_pickup_date=earliest,
            )
            if not resolution.available:
                return PricingResult(False, reason=resolution.reason)
            for resolved in resolution.legs:
                segment = resolved.departure.corridor_id.resolve_region_segment(
                    resolved.origin_region, resolved.dest_region,
                )
                if not segment:
                    return PricingResult(False, reason="departure_corridor_topology_changed")
                topology_legs.append(self._priced_leg_source(
                    segment, hub=resolved.hub,
                    departure=resolved.departure, vehicle=resolved.vehicle,
                ))
        else:
            from .route_resolver import RouteResolver
            route = RouteResolver(self.env).resolve(
                pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment=equipment, partner=partner,
                shipment_type=shipment_type, reference_dt=reference_dt,
            )
            if not route.available:
                return PricingResult(False, reason=route.reason)
            topology_legs = route.legs

        leg_snapshots = []
        all_lines = []
        minimum_charge = max((leg["minimum_booking_charge"] for leg in topology_legs), default=0.0)
        currency = self.env["res.currency"].browse(
            topology_legs[0]["currency_id"]
        ).exists() or self.env.company.currency_id
        for index, leg in enumerate(topology_legs):
            breakdown = self.calculate_leg_per_km(
                leg["distance_km"], leg["rate_per_km"], leg["planned_pallets"],
                pallets, leg["included_weight_per_pallet"], weight_lbs, currency=currency,
            )
            lines = [{
                "label": (
                    f"{leg['distance_km']:.1f} km × {pallets} pallet(s) × "
                    f"${breakdown['pallet_rate_per_km']:.4f}/pallet-km"
                ),
                "amount": breakdown["base_leg_charge"],
            }]
            if breakdown["extra_weight_charge"]:
                lines.append({"label": "Excess weight", "amount": breakdown["extra_weight_charge"]})
            snapshot = dict(leg)
            for non_json_key in ("corridor", "hub", "lane", "rate_plan"):
                snapshot.pop(non_json_key, None)
            snapshot.update({
                "sequence": index + 1,
                "price": breakdown["subtotal"],
                "price_lines": lines,
                "pricing_formula": breakdown,
                "pallets": pallets,
                "weight_lbs": weight_lbs,
            })
            leg_snapshots.append(snapshot)
            if len(topology_legs) > 1:
                all_lines.append({
                    "label": f"Leg {index + 1}: {leg['origin_region']} → {leg['dest_region']}",
                    "amount": breakdown["subtotal"],
                })
            all_lines.extend(lines)

        subtotal = currency.round(sum(leg["price"] for leg in leg_snapshots))
        minimum_top_up = currency.round(max(0.0, minimum_charge - subtotal))
        if minimum_top_up:
            leg_snapshots[-1]["price"] = currency.round(leg_snapshots[-1]["price"] + minimum_top_up)
            leg_snapshots[-1]["price_lines"].append({
                "label": "Booking minimum adjustment", "amount": minimum_top_up,
            })
            all_lines.append({"label": "Minimum booking charge", "amount": minimum_top_up})
        total = currency.round(subtotal + minimum_top_up)

        if leg_snapshots and leg_snapshots[0].get("departure_date"):
            pickup_date = datetime.date.fromisoformat(leg_snapshots[0]["pickup_date"])
            delivery_date = datetime.date.fromisoformat(leg_snapshots[-1]["delivery_date"])

        transfer_hub_id = next((leg.get("hub_id") for leg in leg_snapshots if leg.get("hub_id")), False)

        route_snapshot = {
            "legs": leg_snapshots,
            "leg_count": len(leg_snapshots),
            "calculated_price": total,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "temperature_mode": equipment,
            "required_temperature_c": required_temperature_c,
            "shipment_type": shipment_type,
            "transfer_hub_id": transfer_hub_id,
            "minimum_booking_charge": minimum_charge,
            "pricing_authority": "corridor_per_km",
        }

        primary_corridor = topology_legs[0]["corridor"]
        return PricingResult(
            True,
            corridor=primary_corridor,
            lane=False,
            service_offering=False,
            rate_plan=False,
            schedule=False,
            pickup_date=pickup_date,
            delivery_date_estimate=delivery_date,
            price_lines=all_lines,
            calculated_price=total,
            route_snapshot=route_snapshot,
        )

    @staticmethod
    def _priced_leg_source(segment, hub=None, departure=None, vehicle=None):
        corridor = segment["corridor"]
        origin = segment["origin_region"]
        destination = segment["destination_region"]
        departure_date = departure.departure_date if departure else None
        pickup_date = (
            departure_date + datetime.timedelta(days=segment["pickup_day_offset"])
            if departure_date else None
        )
        delivery_date = (
            departure_date + datetime.timedelta(days=segment["delivery_day_offset"])
            if departure_date else None
        )
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
            "departure_id": departure.id if departure else False,
            "departure_date": str(departure_date) if departure_date else False,
            "departure_time": departure.departure_time if departure else False,
            "pickup_date": str(pickup_date) if pickup_date else False,
            "delivery_date": str(delivery_date) if delivery_date else False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_name": (vehicle.name or vehicle.license_plate or "") if vehicle else "",
            "hub_id": hub.id if hub else False,
            "hub_name": hub.public_name if hub else "",
            "hub_location_id": hub.saved_location_id.id if hub and hub.saved_location_id else False,
            "lane_id": False,
            "offering_id": False,
            "rate_plan_id": False,
            "rate_plan_name": "",
            "rate_plan_version": 0,
        }

    def calculate_leg_per_km(self, distance_km, rate_per_km, target_pallets,
                              booked_pallets, included_weight_per_pallet,
                              actual_weight_lbs, currency=None):
        """Canonical per-km pricing formula for one booking leg.

        D = chargeable road distance in km
        R = configured truck target rate in $/km
        T = target pallets (default 8)
        P = booked pallets
        I = included weight per pallet (default 500 lb)
        W = actual shipment weight

        Pallet rate per km = R / T
        Base leg charge = D × P × R / T
        Weight rate per lb/km = R / (T × I)
        Shipment included weight = P × I
        Extra weight = MAX(0, W − P × I)
        Extra weight charge = Extra weight × D × Weight rate per lb/km
        Leg subtotal = Base leg charge + Extra weight charge

        Uses Odoo currency rounding if currency is provided, otherwise
        falls back to 2-decimal Python rounding.

        Returns dict with all intermediate values.
        """
        # Reject invalid configuration — do not silently default
        if target_pallets <= 0:
            raise ValueError("target_pallets must be positive")
        if included_weight_per_pallet <= 0:
            raise ValueError("included_weight_per_pallet must be positive")
        if distance_km < 0:
            raise ValueError("distance_km must be non-negative")
        if rate_per_km < 0:
            raise ValueError("rate_per_km must be non-negative")

        T = target_pallets
        I = included_weight_per_pallet
        D = distance_km
        P = max(booked_pallets, 0)
        W = max(actual_weight_lbs, 0.0)

        pallet_rate_per_km = rate_per_km / T
        base_leg_charge = D * P * pallet_rate_per_km

        weight_rate_per_lb_km = rate_per_km / (T * I)
        shipment_included_weight = P * I
        extra_weight = max(0.0, W - shipment_included_weight)
        extra_weight_charge = extra_weight * D * weight_rate_per_lb_km

        subtotal = base_leg_charge + extra_weight_charge

        # Use Odoo currency rounding when available, fall back to Python round
        if currency:
            base_leg_charge = currency.round(base_leg_charge)
            extra_weight_charge = currency.round(extra_weight_charge)
            subtotal = currency.round(subtotal)
        else:
            base_leg_charge = round(base_leg_charge, 2)
            extra_weight_charge = round(extra_weight_charge, 2)
            subtotal = round(subtotal, 2)

        return {
            "distance_km": D,
            "rate_per_km": rate_per_km,
            "target_pallets": T,
            "booked_pallets": P,
            "included_weight_per_pallet": I,
            "actual_weight_lbs": W,
            "pallet_rate_per_km": round(pallet_rate_per_km, 6),
            "base_leg_charge": base_leg_charge,
            "weight_rate_per_lb_km": round(weight_rate_per_lb_km, 8),
            "shipment_included_weight": shipment_included_weight,
            "extra_weight_lbs": round(extra_weight, 2),
            "extra_weight_charge": extra_weight_charge,
            "subtotal": subtotal,
            "pricing_method": "per_km_distance",
        }

    def calculate_itinerary_price(self, legs_data):
        """Sum per-km leg charges for a multi-leg itinerary.

        legs_data: list of dicts, each with keys:
            distance_km, rate_per_km, target_pallets, booked_pallets,
            included_weight_per_pallet, actual_weight_lbs

        Returns total subtotal across all legs.
        """
        total = 0.0
        leg_results = []
        for leg in legs_data:
            result = self.calculate_leg_per_km(**leg)
            leg_results.append(result)
            total += result["subtotal"]
        return {
            "legs": leg_results,
            "leg_count": len(leg_results),
            "total_subtotal": round(total, 2),
        }

    # calculate_simple() and suggest_revenue_target() were removed. Corridors
    # are the sole active pricing authority; no legacy DEFAULT_TARGETS fallback.

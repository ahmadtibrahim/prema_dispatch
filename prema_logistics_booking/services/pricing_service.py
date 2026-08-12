"""Canonical customer pricing for the scheduled corridor network.

Pricing authority is the configured Service Route (``logistics.corridor``).
The engine prices the exact itinerary returned by RouteResolver / DepartureResolver,
then applies commercial adjustments ONCE at booking level:

1. transportation legs (feeder/mainline/final-mile roles)
2. pallet-volume discount
3. transportation minimum
4. excess-weight charge once
5. snapshot the exact calculation for confirmation/invoicing

FTL is intentionally different from shared LTL.  An FTL shipment must resolve to
one direct dedicated movement; a hub-transfer itinerary is not silently sold as FTL.
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
        self.route_snapshot = kwargs.get("route_snapshot", {})
        self.manual_review_required = kwargs.get("manual_review_required", False)
        self.recommend_ftl = kwargs.get("recommend_ftl", False)


class PricingService:
    def __init__(self, env):
        self.env = env(su=True)

    @staticmethod
    def _round(value, currency=None):
        return currency.round(value) if currency else round(value or 0.0, 2)

    @staticmethod
    def _apply_booking_minimum(subtotal, minimum_charge, currency=None):
        subtotal = PricingService._round(subtotal, currency)
        minimum_charge = PricingService._round(minimum_charge, currency)
        if subtotal >= minimum_charge:
            return subtotal, 0.0
        adjustment = PricingService._round(minimum_charge - subtotal, currency)
        return minimum_charge, adjustment

    @staticmethod
    def _leg_role(index, leg_count):
        if leg_count == 1:
            return "mainline"
        if index == 0:
            return "feeder"
        if index == leg_count - 1:
            return "mainline" if leg_count == 2 else "final_mile"
        return "mainline"

    def _select_pricing_anchor(self, topology_legs, origin_region):
        """Return the commercial mainline corridor, never simply 'leg 1'.

        A corridor explicitly allowing the pickup region as a feeder wins.  If
        no explicit feeder rule applies, the longest physical leg is the most
        stable deterministic mainline fallback.
        """
        if len(topology_legs) == 1:
            return topology_legs[0]["corridor"], 0

        for idx, leg in enumerate(topology_legs):
            corridor = leg["corridor"]
            if corridor.enable_transit_pricing and origin_region in corridor.allowed_feeder_region_ids:
                return corridor, idx

        idx, leg = max(
            enumerate(topology_legs),
            key=lambda pair: (pair[1].get("distance_km") or 0.0, -pair[0]),
        )
        return leg["corridor"], idx

    def _volume_discount_pct(self, corridor, pallets):
        if not corridor.enable_volume_discounts or pallets < 2:
            return 0.0
        Tier = self.env["logistics.pallet.volume.tier"]
        return max(0.0, min(100.0, Tier.get_discount_for_pallets(corridor.id, pallets)))

    def _global_excess_weight_rate(self):
        ICP = self.env["ir.config_parameter"]
        raw = ICP.get_param("logistics.default_excess_weight_rate", "0") or "0"
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def _feeder_charge(self, leg, normal_charge, mainline_corridor, pallets, currency):
        """Apply the pricing anchor's configured feeder/final-mile policy."""
        if not mainline_corridor.enable_transit_pricing:
            return normal_charge, "connected_corridor"

        method = mainline_corridor.feeder_pricing_method or "percentage"
        if method == "percentage":
            discount = max(0.0, min(100.0, mainline_corridor.feeder_discount_pct or 0.0))
            charge = normal_charge * (1.0 - discount / 100.0)
        elif method == "dedicated_km":
            override_rate = mainline_corridor.feeder_rate_per_km or leg["rate_per_km"]
            target = max(mainline_corridor.planned_pallets or leg["planned_pallets"] or 1, 1)
            charge = (leg["distance_km"] or 0.0) * pallets * override_rate / target
        else:  # connected_corridor
            charge = normal_charge

        charge = self._round(charge, currency)
        feeder_min = mainline_corridor.feeder_minimum_charge or 0.0
        if feeder_min:
            charge = max(charge, self._round(feeder_min, currency))
        return charge, method

    def calculate(self, pickup_fsa, delivery_fsa, shipment_type, temperature_mode,
                   pallets, weight_lbs, liftgate_pickup=False, liftgate_delivery=False,
                   appointment=False, residential=False, same_day_requested=False,
                   partner=None, reference_dt=None, required_temperature_c=None,
                   resolve_departures=False):
        del liftgate_pickup, liftgate_delivery, appointment, residential, same_day_requested

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
            earliest = (
                reference_dt.date() if hasattr(reference_dt, "date")
                else reference_dt or datetime.date.today()
            )
            resolution = DepartureResolver(self.env).resolve(
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

        if not topology_legs:
            return PricingResult(False, reason="no_pricing_legs")

        currency = self.env["res.currency"].browse(topology_legs[0]["currency_id"]).exists()
        currency = currency or self.env.company.currency_id
        pricing_anchor, anchor_index = self._select_pricing_anchor(topology_legs, origin_region)

        # FTL is an exclusive direct-truck product, not a renamed shared transfer.
        threshold_hit = bool(
            pricing_anchor.enable_ftl
            and pricing_anchor.ftl_threshold_pallets
            and pallets >= pricing_anchor.ftl_threshold_pallets
        )
        requested_ftl = shipment_type == "ftl"
        recommend_ftl = threshold_hit and pricing_anchor.ftl_behavior == "recommend" and not requested_ftl
        if threshold_hit and pricing_anchor.ftl_behavior == "dispatcher_approval" and not requested_ftl:
            return PricingResult(
                False, reason="ftl_dispatcher_approval_required",
                manual_review_required=True, corridor=pricing_anchor,
            )
        use_ftl = requested_ftl or (threshold_hit and pricing_anchor.ftl_behavior == "auto_price")
        if use_ftl and len(topology_legs) != 1:
            return PricingResult(
                False, reason="ftl_requires_dedicated_direct_service",
                manual_review_required=True, corridor=pricing_anchor,
            )
        if use_ftl and not pricing_anchor.enable_ftl:
            return PricingResult(False, reason="ftl_not_enabled_for_corridor", corridor=pricing_anchor)
        if use_ftl and pricing_anchor.ftl_rate_per_km <= 0:
            return PricingResult(False, reason="ftl_rate_not_configured", corridor=pricing_anchor)

        leg_snapshots = []
        price_lines = []
        transportation_subtotal = 0.0

        if use_ftl:
            leg = topology_legs[0]
            charge = self._round((leg["distance_km"] or 0.0) * pricing_anchor.ftl_rate_per_km, currency)
            transportation_subtotal = max(charge, self._round(pricing_anchor.ftl_minimum_charge or 0.0, currency))
            snapshot = self._snapshot_leg(leg, 1, pallets, weight_lbs)
            snapshot.update({
                "leg_role": "mainline",
                "pricing_mode": "ftl",
                "price": transportation_subtotal,
                "price_lines": [{
                    "label": f"Dedicated FTL: {leg['distance_km']:.1f} km × ${pricing_anchor.ftl_rate_per_km:.2f}/km",
                    "amount": transportation_subtotal,
                }],
            })
            leg_snapshots.append(snapshot)
            price_lines.extend(snapshot["price_lines"])
        else:
            for index, leg in enumerate(topology_legs):
                role = "mainline" if index == anchor_index else self._leg_role(index, len(topology_legs))
                breakdown = self.calculate_leg_per_km(
                    leg["distance_km"], leg["rate_per_km"], leg["planned_pallets"],
                    pallets, leg["included_weight_per_pallet"], 0.0, currency=currency,
                )
                normal_charge = breakdown["base_leg_charge"]
                pricing_method = "corridor_per_km"
                charge = normal_charge
                if index != anchor_index:
                    charge, pricing_method = self._feeder_charge(
                        leg, normal_charge, pricing_anchor, pallets, currency,
                    )
                transportation_subtotal += charge
                line = {
                    "label": (
                        f"{role.replace('_', ' ').title()}: {leg['distance_km']:.1f} km × "
                        f"{pallets} pallet(s)"
                    ),
                    "amount": charge,
                }
                snapshot = self._snapshot_leg(leg, index + 1, pallets, weight_lbs)
                snapshot.update({
                    "leg_role": role,
                    "pricing_mode": pricing_method,
                    "price": charge,
                    "price_lines": [line],
                    "pricing_formula": breakdown,
                })
                leg_snapshots.append(snapshot)
                price_lines.append(line)

            transportation_subtotal = self._round(transportation_subtotal, currency)

            # Volume tiers are a BOOKING-level commercial discount based on
            # physical pallet count; never apply per leg or per stop allocation.
            discount_pct = self._volume_discount_pct(pricing_anchor, pallets)
            discount_amount = self._round(
                transportation_subtotal * discount_pct / 100.0, currency,
            )
            discounted_transport = self._round(transportation_subtotal - discount_amount, currency)
            if discount_amount:
                price_lines.append({
                    "label": f"Pallet volume discount ({discount_pct:g}%)",
                    "amount": -discount_amount,
                })

            minimum_candidates = [leg.get("minimum_booking_charge") or 0.0 for leg in topology_legs]
            minimum_candidates.extend([
                origin_region.minimum_booking_charge or 0.0,
                destination_region.minimum_booking_charge or 0.0,
            ])
            booking_minimum = max(minimum_candidates or [0.0])
            transportation_subtotal, minimum_adjustment = self._apply_booking_minimum(
                discounted_transport, booking_minimum, currency,
            )
            if minimum_adjustment:
                price_lines.append({
                    "label": "Minimum transportation charge adjustment",
                    "amount": minimum_adjustment,
                })
        
        # Excess weight is intentionally charged once for the shipment, not
        # once on every transfer leg.
        included_weight_per_pallet = pricing_anchor.included_weight_per_pallet or 0.0
        included_weight = pallets * included_weight_per_pallet
        excess_weight = max(0.0, weight_lbs - included_weight)
        excess_rate = pricing_anchor.excess_weight_rate_per_lb or self._global_excess_weight_rate()
        excess_charge = self._round(excess_weight * excess_rate, currency)
        if excess_charge:
            price_lines.append({
                "label": f"Excess weight: {excess_weight:,.0f} lb × ${excess_rate:.2f}/lb",
                "amount": excess_charge,
            })

        total = self._round(transportation_subtotal + excess_charge, currency)

        if leg_snapshots and leg_snapshots[0].get("departure_date"):
            pickup_date = datetime.date.fromisoformat(leg_snapshots[0]["pickup_date"])
            delivery_date = datetime.date.fromisoformat(leg_snapshots[-1]["delivery_date"])

        transfer_hub_id = next((leg.get("hub_id") for leg in leg_snapshots if leg.get("hub_id")), False)
        route_snapshot = {
            "legs": leg_snapshots,
            "leg_count": len(leg_snapshots),
            "calculated_price": total,
            "transportation_subtotal": transportation_subtotal,
            "pallets": pallets,
            "physical_pallets": pallets,
            "weight_lbs": weight_lbs,
            "included_weight_lbs": included_weight,
            "excess_weight_lbs": excess_weight,
            "excess_weight_rate_per_lb": excess_rate,
            "excess_weight_charge": excess_charge,
            "temperature_mode": equipment,
            "required_temperature_c": required_temperature_c,
            "shipment_type": "ftl" if use_ftl else "ltl",
            "transfer_hub_id": transfer_hub_id,
            "pricing_anchor_corridor_id": pricing_anchor.id,
            "pricing_anchor_corridor_name": pricing_anchor.name,
            "pricing_authority": "corridor_v2",
            "recommend_ftl": recommend_ftl,
        }

        return PricingResult(
            True,
            corridor=pricing_anchor,
            lane=False,
            service_offering=False,
            rate_plan=False,
            schedule=False,
            pickup_date=pickup_date,
            delivery_date_estimate=delivery_date,
            price_lines=price_lines,
            calculated_price=total,
            route_snapshot=route_snapshot,
            recommend_ftl=recommend_ftl,
        )

    @staticmethod
    def _snapshot_leg(leg, sequence, pallets, weight_lbs):
        snapshot = dict(leg)
        for non_json_key in ("corridor", "hub", "lane", "rate_plan"):
            snapshot.pop(non_json_key, None)
        snapshot.update({
            "sequence": sequence,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
        })
        return snapshot

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
        """Base corridor math for one physical leg.

        ``actual_weight_lbs`` is accepted for API compatibility, but the
        booking-level engine intentionally does not levy excess-weight here.
        Callers using this helper directly still receive the intermediate
        excess-weight values for diagnostics only.
        """
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
        shipment_included_weight = P * I
        extra_weight = max(0.0, W - shipment_included_weight)

        base_leg_charge = self._round(base_leg_charge, currency)
        return {
            "distance_km": D,
            "rate_per_km": rate_per_km,
            "target_pallets": T,
            "booked_pallets": P,
            "included_weight_per_pallet": I,
            "actual_weight_lbs": W,
            "pallet_rate_per_km": round(pallet_rate_per_km, 6),
            "base_leg_charge": base_leg_charge,
            "shipment_included_weight": shipment_included_weight,
            "extra_weight_lbs": round(extra_weight, 2),
            "extra_weight_charge": 0.0,
            "subtotal": base_leg_charge,
            "pricing_method": "per_km_distance",
        }

    def calculate_itinerary_price(self, legs_data):
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

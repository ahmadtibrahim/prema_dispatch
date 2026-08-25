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
        self.manual_review_required = kwargs.get("manual_review_required", False)
        self.recommend_ftl = kwargs.get("recommend_ftl", False)


class PricingService:
    def __init__(self, env):
        self.env = env(su=True)

    @staticmethod
    def _apply_booking_minimum(subtotal, minimum_charge, currency=None):
        """Apply a pricing floor ONCE per booking (not once per leg).

        Returns (total, adjustment_amount).
        """
        if currency:
            subtotal = currency.round(subtotal)
            minimum_charge = currency.round(minimum_charge)
        else:
            subtotal = round(subtotal or 0.0, 2)
            minimum_charge = round(minimum_charge or 0.0, 2)

        if subtotal >= minimum_charge:
            return subtotal, 0.0

        adjustment = minimum_charge - subtotal
        if currency:
            adjustment = currency.round(adjustment)
        else:
            adjustment = round(adjustment, 2)
        return minimum_charge, adjustment

    @staticmethod
    def _select_pricing_anchor(topology_legs, origin_region):
        """Pick the corridor that owns pricing for this shipment: the leg
        serving the origin region, otherwise the first leg."""
        for index, leg in enumerate(topology_legs):
            if origin_region and (
                leg.get("origin_region_id") == origin_region.id
                or leg.get("dest_region_id") == origin_region.id
            ):
                return leg["corridor"], index
        return topology_legs[0]["corridor"], 0

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
        # FSA rows point at the legacy lane regions (ids 1-20); corridors
        # and hubs are keyed by the official LTL regions (142+). Canonicalize
        # through the same bridge the coordinate path uses, so an FSA-only
        # request (typed postal, no facility) resolves the same corridor the
        # coordinate-based quote would.
        from .region_resolver import RegionResolver
        region_resolver = RegionResolver(self.env)
        origin_region = region_resolver.canonical_region(origin_region)
        destination_region = region_resolver.canonical_region(destination_region)
        if not origin_region or not destination_region:
            return PricingResult(False, reason="fsa_not_mapped_to_region")
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
                service_type=shipment_type,
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
        currency = self.env["res.currency"].browse(
            topology_legs[0]["currency_id"]
        ).exists() or self.env.company.currency_id

        # Booking minimum: corridor min + optional endpoint region overrides
        mins = [
            (leg.get("minimum_booking_charge") or 0.0) for leg in topology_legs
            if isinstance(leg, dict)
        ]
        try:
            mins.append(origin_region.minimum_booking_charge or 0.0)
            mins.append(destination_region.minimum_booking_charge or 0.0)
        except Exception:
            # Defensive: in case region model is customized/missing the field
            pass
        booking_minimum_charge = max(mins or [0.0])

        # ── FTL classification ────────────────────────────────────────────
        # The corridor's Enable Full Truckload / FTL Threshold / "When
        # threshold reached" configuration is the sole authority here.
        # FTL is an exclusive direct-truck product: a hub-transfer itinerary
        # is never silently sold as FTL. When Full Truckload is disabled the
        # booking continues through normal LTL pricing untouched.
        pricing_anchor, _anchor_index = self._select_pricing_anchor(topology_legs, origin_region)
        requested_ftl = shipment_type == "ftl"
        threshold_hit = bool(
            pricing_anchor.enable_ftl
            and pricing_anchor.ftl_threshold_pallets
            and pallets >= pricing_anchor.ftl_threshold_pallets
        )
        recommend_ftl = False
        use_ftl = False
        if pricing_anchor.enable_ftl:
            if threshold_hit and pricing_anchor.ftl_behavior == "dispatcher_approval" and not requested_ftl:
                return PricingResult(
                    False, reason="ftl_dispatcher_approval_required",
                    manual_review_required=True, corridor=pricing_anchor,
                )
            recommend_ftl = bool(
                threshold_hit and pricing_anchor.ftl_behavior == "recommend" and not requested_ftl
            )
            use_ftl = requested_ftl or (threshold_hit and pricing_anchor.ftl_behavior == "auto_price")
        if use_ftl and len(topology_legs) != 1:
            return PricingResult(
                False, reason="ftl_requires_dedicated_direct_service",
                manual_review_required=True, corridor=pricing_anchor,
            )

        for index, leg in enumerate(topology_legs):
            if use_ftl:
                # One source of truth: the corridor's FTL regional pricing
                # method owns the calculation, including rule lookup and the
                # flat-rate / per-km / corridor-default logic. Legacy
                # minimum-charge fields are never consulted.
                ftl = pricing_anchor.compute_ftl_price(
                    origin_region, destination_region, leg["distance_km"] or 0.0,
                )
                if ftl["pricing_type"] == "flat_rate":
                    if not ftl["regional_rule"] or ftl["regional_rule"].flat_rate <= 0:
                        return PricingResult(
                            False, reason="ftl_rate_not_configured", corridor=pricing_anchor,
                        )
                elif ftl["rate_per_km"] <= 0:
                    return PricingResult(
                        False, reason="ftl_rate_not_configured", corridor=pricing_anchor,
                    )
                price = ftl["price"]
                if ftl["pricing_type"] == "flat_rate":
                    lines = [{"label": "Dedicated FTL (flat rate)", "amount": price}]
                else:
                    lines = [{
                        "label": (
                            f"Dedicated FTL: {leg['distance_km']:.1f} km × "
                            f"${ftl['rate_per_km']:.2f}/km"
                        ),
                        "amount": price,
                    }]
                if ftl["regional_rule"]:
                    if ftl["pricing_type"] == "flat_rate":
                        rule_label = "Flat Rate ${:,.2f}".format(ftl["regional_rule"].flat_rate)
                    elif ftl["pricing_type"] == "per_km":
                        rule_label = "${:.2f}/km".format(ftl["regional_rule"].ftl_rate_per_km_override)
                    else:
                        rule_label = "Corridor Default"
                    lines.append({
                        "label": (
                            f"FTL regional pricing "
                            f"({ftl['regional_rule'].origin_region_id.code} → "
                            f"{ftl['regional_rule'].destination_region_id.code}): "
                            f"{rule_label}"
                        ),
                        "amount": 0.0,
                    })
                snapshot = dict(leg)
                for non_json_key in ("corridor", "hub", "lane", "rate_plan"):
                    snapshot.pop(non_json_key, None)
                snapshot.update({
                    "sequence": index + 1,
                    "price": price,
                    "price_lines": lines,
                    "pricing_formula": {
                        "pricing_method": "ftl_regional_minimum",
                        "distance_km": leg["distance_km"],
                        "rate_per_km": ftl["rate_per_km"],
                        "distance_price": ftl["distance_price"],
                        "regional_rule_id": ftl["regional_rule"].id if ftl["regional_rule"] else False,
                        "pricing_type": ftl["pricing_type"],
                        "flat_rate": (
                            ftl["regional_rule"].flat_rate if ftl["regional_rule"] else 0.0
                        ),
                    },
                    "pallets": pallets,
                    "weight_lbs": weight_lbs,
                })
                leg_snapshots.append(snapshot)
                all_lines.extend(lines)
                continue
            # The corridor's configured excess-weight $/lb is the live
            # authority (corridor override → global Dispatch Settings
            # default). Never a hardcoded rate, never the legacy
            # distance-scaled estimate.
            excess_rate = 0.0
            corridor_rec = leg.get("corridor")
            if corridor_rec:
                excess_rate = corridor_rec.excess_weight_rate_per_lb or 0.0
            if not excess_rate:
                excess_rate = float(
                    self.env["ir.config_parameter"].sudo().get_param(
                        "logistics.default_excess_weight_rate", "0.0") or 0.0
                )
            breakdown = self.calculate_leg_per_km(
                leg["distance_km"], leg["rate_per_km"], leg["planned_pallets"],
                pallets, leg["included_weight_per_pallet"], weight_lbs,
                currency=currency, excess_weight_rate_per_lb=excess_rate or None,
            )
            lines = [{
                "label": (
                    f"{leg['distance_km']:.1f} km × {pallets} pallet(s) × "
                    f"${breakdown['pallet_rate_per_km']:.4f}/pallet-km"
                ),
                "amount": breakdown["base_leg_charge"],
            }]
            if breakdown["extra_weight_charge"]:
                lines.append({
                    "label": (
                        f"Excess weight "
                        f"({breakdown['extra_weight_lbs']:.0f} lb over "
                        f"{breakdown['shipment_included_weight']:.0f} lb included × "
                        f"${breakdown['excess_weight_rate_per_lb']:.2f}/lb)"
                    ),
                    "amount": breakdown["extra_weight_charge"],
                })
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
        # Pallet-volume discount: applied ONCE on the booking's LTL freight
        # total (never per leg, never to FTL). The pricing-anchor corridor
        # owns the tier configuration.
        volume_discount_pct = 0.0
        volume_discount_amount = 0.0
        if not use_ftl and pricing_anchor.enable_volume_discounts:
            volume_discount_pct = self.env["logistics.pallet.volume.tier"].get_discount_for_pallets(
                pricing_anchor.id, pallets,
            )
            if volume_discount_pct:
                discount_amount = currency.round(subtotal * volume_discount_pct / 100.0)
                volume_discount_amount = -discount_amount
                subtotal = currency.round(subtotal - discount_amount)
                all_lines.append({
                    "label": "Pallet volume discount (%g%%)" % volume_discount_pct,
                    "amount": -discount_amount,
                })
        # FTL price carries its own floor (the regional minimum) and never
        # the LTL booking minimum.
        total, minimum_adjustment = self._apply_booking_minimum(
            subtotal, 0.0 if use_ftl else booking_minimum_charge, currency=currency,
        )
        if minimum_adjustment:
            leg_snapshots[-1]["price"] = currency.round(leg_snapshots[-1]["price"] + minimum_adjustment)
            leg_snapshots[-1]["price_lines"].append({
                "label": "Minimum booking adjustment", "amount": minimum_adjustment,
            })
            all_lines.append({"label": "Minimum booking adjustment", "amount": minimum_adjustment})

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
            "minimum_booking_charge": 0.0 if use_ftl else booking_minimum_charge,
            "pricing_authority": "corridor_per_km",
            "ftl_priced": use_ftl,
            "recommend_ftl": recommend_ftl,
            "volume_discount_pct": volume_discount_pct,
            "volume_discount_amount": volume_discount_amount,
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
            recommend_ftl=recommend_ftl,
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

    @staticmethod
    def calculate_leg_per_km(distance_km, rate_per_km, target_pallets,
                             booked_pallets, included_weight_per_pallet,
                             actual_weight_lbs, currency=None,
                             excess_weight_rate_per_lb=None):
        """Canonical per-km pricing formula for one booking leg — the ONE
        LTL pricing calculator for calendar preview, portal Get Price,
        phone booking, internal booking, custom quote, recurring booking,
        pricing session and final booking.

        D = chargeable road distance in km
        R = configured truck target rate in $/km
        T = target pallets (default 8)
        P = booked pallets
        I = included weight per pallet (default 500 lb)
        W = actual shipment weight

        Pallet rate per km = R / T
        Base leg charge = D × P × R / T
        Shipment included weight = P × I
        Extra weight = MAX(0, W − P × I)

        Excess-weight charge authority (corridor configuration):
        - When excess_weight_rate_per_lb is provided and > 0 (the
          corridor's configured "$ / lb" rate — corridor override
          falling back to the global Dispatch Settings default), the
          charge is FLAT per excess pound:
              Extra weight charge = Extra weight × excess_weight_rate_per_lb
        - When it is absent/zero, no weight charge applies at all —
          the shipment's weight up to P × I is fully included and any
          excess is carried at no extra cost:
              Extra weight charge = 0
              weight_pricing_method = "included_no_charge"
          (The old distance-scaled "per lb/km" formula was retired:
          it priced 2 pallets at 5000 lb identically to 1000 lb.)

        Leg subtotal = Base leg charge + Extra weight charge.
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

        shipment_included_weight = P * I
        extra_weight = max(0.0, W - shipment_included_weight)

        # ONE canonical excess-weight formula: flat configured $/lb rate.
        # The distance-scaled legacy estimate was retired — every call site
        # resolves the rate from corridor → global Dispatch Settings default,
        # so a zero rate means "no excess-weight charge", never a silent
        # distance-scaled fee.
        if excess_weight_rate_per_lb:
            excess_rate = float(excess_weight_rate_per_lb)
            extra_weight_charge = extra_weight * excess_rate
            weight_pricing_method = "per_lb_excess_rate"
        else:
            excess_rate = 0.0
            extra_weight_charge = 0.0
            weight_pricing_method = "included_no_charge"

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
            "weight_rate_per_lb_km": round(rate_per_km / (T * I), 8),
            "excess_weight_rate_per_lb": excess_rate,
            "weight_pricing_method": weight_pricing_method,
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

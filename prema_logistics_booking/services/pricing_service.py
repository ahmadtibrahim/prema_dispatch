"""Freight pricing engine — simplified Scheduled Shared LTL pricing.

Phase 9-11: Two pricing modes supported:
    simple  → Revenue Target / Planned Pallets = Price per Pallet
              No tiers, no surcharges, no FSA adjustments.
    tiered  → Full legacy pipeline (tier tables, surcharges, discounts).

Phase 10: Revenue targets can be auto-suggested from region rate_per_km × road_km.
Phase 11: Default revenue targets seeded for major corridors.

Formula (simple mode):
    Customer Price per Pallet = Revenue Target ÷ Planned Pallets
    Total = Customer Price per Pallet × Pallets
"""

import math
from .schedule_service import ScheduleService

# DEFAULT_TARGETS removed — pricing authority is Rate Plans only.
# Use RouteResolver + Rate Plans for all price resolution.


class PricingResult:
    def __init__(self, available, reason=None, **kwargs):
        self.available = available
        self.reason = reason
        self.lane = kwargs.get("lane")
        self.service_offering = kwargs.get("service_offering")
        self.rate_plan = kwargs.get("rate_plan")
        self.schedule = kwargs.get("schedule")
        self.pickup_date = kwargs.get("pickup_date")
        self.delivery_date_estimate = kwargs.get("delivery_date_estimate")
        self.price_lines = kwargs.get("price_lines", [])
        self.calculated_price = kwargs.get("calculated_price", 0.0)
        self.route_snapshot = kwargs.get("route_snapshot", {})  # immutable at confirm


class PricingService:
    def __init__(self, env):
        self.env = env(su=True)
        self.schedule_service = ScheduleService(env)

    def calculate(self, pickup_fsa, delivery_fsa, shipment_type, temperature_mode,
                   pallets, weight_lbs, liftgate_pickup=False, liftgate_delivery=False,
                   appointment=False, residential=False, same_day_requested=False,
                   partner=None, reference_dt=None):
        """Resolve pricing through the ordered-lane RouteResolver.

        Rate Plans are the sole pricing authority. No per-km, AI, road-distance,
        or DEFAULT_TARGETS fallback. Returns PricingResult.
        """
        # Validate inputs
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return PricingResult(False, reason="pickup_fsa_not_supported")
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return PricingResult(False, reason="delivery_fsa_not_supported")
        if not pickup_fsa.region_id or not delivery_fsa.region_id:
            return PricingResult(False, reason="fsa_not_mapped_to_region")
        if pallets > 12:
            return PricingResult(False, reason="pallets_exceed_standard_capacity")

        # Route through the ordered-lane resolver
        from .route_resolver import RouteResolver
        resolver = RouteResolver(self.env)
        route = resolver.resolve(
            pickup_fsa, delivery_fsa, pallets, weight_lbs,
            equipment=temperature_mode or "dry", partner=partner,
        )

        if not route.available:
            return PricingResult(False, reason=route.reason)

        # Sum leg prices and build route snapshot
        total = sum(leg["price"] for leg in route.legs)
        all_lines = []
        leg_snapshots = []
        for i, leg in enumerate(route.legs):
            if len(route.legs) > 1:
                all_lines.append({
                    "label": f"Leg {i+1}: {leg['origin_region']} → {leg['dest_region']}",
                    "amount": leg["price"],
                })
            all_lines.extend(leg["price_lines"])
            leg_snapshots.append({
                "sequence": i + 1,
                "origin_region": leg["origin_region"],
                "dest_region": leg["dest_region"],
                "rate_plan_id": leg["rate_plan"].id,
                "rate_plan_version": leg["rate_plan"].version,
                "lane_id": leg["lane"].id,
                "price": leg["price"],
                "price_lines": leg["price_lines"],
                "pallets": pallets,
                "weight_lbs": weight_lbs,
            })

        # Resolve schedule through the primary leg's offering
        primary_leg = route.legs[0]
        offering = primary_leg["rate_plan"].service_offering_id
        sched_result = self.schedule_service.next_pickup_and_delivery(offering, reference_dt)
        pickup_date = sched_result.pickup_date if sched_result.available else None
        delivery_date = sched_result.delivery_date if sched_result.available else None

        route_snapshot = {
            "legs": leg_snapshots,
            "leg_count": len(leg_snapshots),
            "calculated_price": total,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "temperature_mode": temperature_mode,
            "shipment_type": shipment_type,
        }

        return PricingResult(
            True,
            lane=primary_leg["lane"],
            service_offering=offering,
            rate_plan=primary_leg["rate_plan"],
            schedule=sched_result.schedule if sched_result.available else None,
            pickup_date=pickup_date,
            delivery_date_estimate=delivery_date,
            price_lines=all_lines,
            calculated_price=total,
            route_snapshot=route_snapshot,
        )

    def _active_rate_plan(self, offering, on_date):
        RatePlan = self.env["logistics.rate.plan"]
        return RatePlan.search([
            ("service_offering_id", "=", offering.id),
            ("active", "=", True),
            "|", ("effective_from", "=", False), ("effective_from", "<=", on_date),
            "|", ("effective_to", "=", False), ("effective_to", ">=", on_date),
        ], order="version desc", limit=1)

    def _compute_v4_formula(self, rate_plan, pallets, weight_lbs=0.0):
        """Canonical V4 LTL Hub Pricing formula — single source of truth.

        1. Base Pallet Rate = Revenue Target / Target Load Quantity
        2. Included Weight = Pallets × included_weight_per_pallet (default 500 lb)
        3. Excess Weight = MAX(0, Shipment Weight − Included Weight)
        4. Excess Weight Rate = Revenue Target / Safe Weight Capacity
        5. Leg Base Charge = Pallets × Base Pallet Rate
        6. Weight Surcharge = Excess Weight × Excess Weight Rate
        7. Final = ROUND((Leg Base + Weight Surcharge) / 5) × 5

        Returns a dict with all intermediate values. Every pricing display method
        must derive its output from this dict — do not duplicate the formula.
        """
        tlq = max(rate_plan.target_load_quantity, 1)
        swc = max(rate_plan.safe_weight_capacity or 11000.0, 1.0)
        incl_per_pallet = rate_plan.included_weight_per_pallet or 500.0

        base_rate = rate_plan.revenue_target / tlq
        leg_base = pallets * base_rate

        included_weight = pallets * incl_per_pallet
        excess_weight = max(0.0, weight_lbs - included_weight)
        excess_rate = rate_plan.revenue_target / swc
        weight_surcharge = excess_weight * excess_rate

        subtotal = leg_base + weight_surcharge
        final = round(subtotal / 5.0) * 5.0

        return {
            "tlq": tlq,
            "swc": swc,
            "incl_per_pallet": incl_per_pallet,
            "base_rate": base_rate,
            "leg_base": leg_base,
            "included_weight": included_weight,
            "excess_weight": excess_weight,
            "excess_rate": excess_rate,
            "weight_surcharge": weight_surcharge,
            "subtotal": subtotal,
            "final": final,
        }

    def _compute_price(self, rate_plan, pallets, temperature_mode, weight_lbs=0.0):
        """V4 LTL Hub Pricing — returns (price_lines_list, final_price)."""
        v = self._compute_v4_formula(rate_plan, pallets, weight_lbs)
        lines = []
        lines.append({
            "label": f"Base ({pallets} pallet(s) × ${v['base_rate']:,.2f})",
            "amount": round(v["leg_base"], 2),
        })
        if v["weight_surcharge"] > 0:
            lines.append({
                "label": f"Excess Weight ({v['excess_weight']:,.0f} lb × ${v['excess_rate']:,.3f}/lb)",
                "amount": round(v["weight_surcharge"], 2),
            })
        lines.append({"label": "Subtotal", "amount": round(v["subtotal"], 2)})
        lines.append({"label": "Final Freight Price (nearest $5)", "amount": v["final"]})
        return lines, v["final"]

    def calculate_leg_price(self, rate_plan, pallets, weight_lbs=0.0):
        """Calculate price for a single Booking Leg using the V4 formula.
        Returns dict with full breakdown. Delegates to _compute_v4_formula."""
        v = self._compute_v4_formula(rate_plan, pallets, weight_lbs)
        return {
            "rate_plan_id": rate_plan.id,
            "rate_plan_name": rate_plan.name,
            "revenue_target": rate_plan.revenue_target,
            "target_load_quantity": v["tlq"],
            "base_rate_per_pallet": round(v["base_rate"], 4),
            "pallets": pallets,
            "leg_base_charge": round(v["leg_base"], 2),
            "included_weight_per_pallet": v["incl_per_pallet"],
            "included_weight_total": v["included_weight"],
            "actual_weight_lbs": weight_lbs,
            "excess_weight_lbs": round(v["excess_weight"], 2),
            "excess_weight_rate": round(v["excess_rate"], 6),
            "weight_surcharge": round(v["weight_surcharge"], 2),
            "subtotal": round(v["subtotal"], 2),
            "final_price": v["final"],
            "pricing_method": "v4_ltl_hub",
        }

    # ── PHASE 6: Canonical Per-Kilometre Pricing ─────────────────────

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

    # calculate_simple() and suggest_revenue_target() removed.
    # Rate Plans are the sole pricing authority — no DEFAULT_TARGETS fallback.

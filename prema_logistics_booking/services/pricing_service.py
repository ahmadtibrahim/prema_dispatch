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

# Phase 11: Default revenue targets per corridor (Mississauga ↔ destination)
# Rounded to nearest $25. Dispatcher may override.
DEFAULT_TARGETS = {
    ("R1", "R8"):  1600.00,   # Mississauga ↔ Montreal
    ("R1", "R10"): 2300.00,   # Mississauga ↔ Quebec City
    ("R1", "R7"):  1200.00,   # Mississauga ↔ Ottawa
    ("R1", "R4"):  1200.00,   # Mississauga ↔ Sudbury (via R4 Central ON)
    ("R1", "R3"):   350.00,   # Mississauga ↔ Niagara (minimum)
    ("R8", "R1"):  1600.00,   # Montreal ↔ Mississauga (return)
    ("R10", "R1"): 2300.00,   # Quebec City ↔ Mississauga (return)
    ("R7", "R1"):  1200.00,   # Ottawa ↔ Mississauga (return)
}


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


class PricingService:
    def __init__(self, env):
        self.env = env(su=True)
        self.schedule_service = ScheduleService(env)

    def calculate(self, pickup_fsa, delivery_fsa, shipment_type, temperature_mode,
                   pallets, weight_lbs, liftgate_pickup=False, liftgate_delivery=False,
                   appointment=False, residential=False, same_day_requested=False,
                   partner=None, reference_dt=None):
        """Returns a PricingResult."""

        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return PricingResult(False, reason="pickup_fsa_not_supported")
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return PricingResult(False, reason="delivery_fsa_not_supported")
        if not pickup_fsa.region_id or not delivery_fsa.region_id:
            return PricingResult(False, reason="fsa_not_mapped_to_region")

        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", pickup_fsa.region_id.id),
            ("destination_region_id", "=", delivery_fsa.region_id.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return PricingResult(False, reason="lane_not_supported")
        if shipment_type == "ltl" and not lane.ltl_capable:
            return PricingResult(False, reason="lane_ltl_not_capable")
        if shipment_type == "ftl" and not lane.ftl_capable:
            return PricingResult(False, reason="lane_ftl_not_capable")

        from .capacity_engine import CapacityEngine
        cap_engine = CapacityEngine(self.env)
        equipment = lane.equipment_profile_id
        vehicle = equipment.fleet_vehicle_id if equipment else None
        cap = cap_engine.evaluate(pallets, weight_lbs, vehicle=vehicle)
        if not cap.eligible:
            return PricingResult(False, reason=cap.reason_code or "pallet_capacity_exceeded", lane=lane)

        # Only DRY offerings — temperature is a surcharge, not a separate plan
        Offering = self.env["logistics.service.offering"]
        candidates = Offering.search([
            ("lane_id", "=", lane.id),
            ("active", "=", True),
            ("temperature_mode", "=", "dry"),
            "|", ("shipment_type", "=", shipment_type), ("shipment_type", "=", "both"),
        ])
        if not candidates:
            return PricingResult(False, reason="no_service_offering")

        best = None
        for offering in candidates:
            sched_result = self.schedule_service.next_pickup_and_delivery(offering, reference_dt)
            if not sched_result.available:
                continue
            rate_plan = self._active_rate_plan(offering, sched_result.pickup_date)
            if not rate_plan:
                continue
            if best is None or sched_result.pickup_date < best[1].pickup_date:
                best = (offering, sched_result, rate_plan)

        if not best:
            return PricingResult(False, reason="not_configured", lane=lane)

        offering, sched_result, rate_plan = best
        price_lines, total = self._compute_price(
            rate_plan, pallets, temperature_mode, weight_lbs=weight_lbs,
        )

        return PricingResult(
            True, lane=lane, service_offering=offering, rate_plan=rate_plan,
            schedule=sched_result.schedule, pickup_date=sched_result.pickup_date,
            delivery_date_estimate=sched_result.delivery_date,
            price_lines=price_lines, calculated_price=total,
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
                              actual_weight_lbs):
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

        Returns dict with all intermediate values.
        """
        T = max(target_pallets, 1)
        I = max(included_weight_per_pallet, 1.0)
        D = max(distance_km, 0.0)
        P = max(booked_pallets, 0)
        W = max(actual_weight_lbs, 0.0)

        pallet_rate_per_km = rate_per_km / T
        base_leg_charge = D * P * pallet_rate_per_km

        weight_rate_per_lb_km = rate_per_km / (T * I)
        shipment_included_weight = P * I
        extra_weight = max(0.0, W - shipment_included_weight)
        extra_weight_charge = extra_weight * D * weight_rate_per_lb_km

        subtotal = base_leg_charge + extra_weight_charge

        return {
            "distance_km": D,
            "rate_per_km": rate_per_km,
            "target_pallets": T,
            "booked_pallets": P,
            "included_weight_per_pallet": I,
            "actual_weight_lbs": W,
            "pallet_rate_per_km": round(pallet_rate_per_km, 6),
            "base_leg_charge": round(base_leg_charge, 2),
            "weight_rate_per_lb_km": round(weight_rate_per_lb_km, 8),
            "shipment_included_weight": shipment_included_weight,
            "extra_weight_lbs": round(extra_weight, 2),
            "extra_weight_charge": round(extra_weight_charge, 2),
            "subtotal": round(subtotal, 2),
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

    # ── Phase 9: Explicit Simple Mode ─────────────────────────────────

    def calculate_simple(self, lane, pallets, reference_dt=None):
        """Simple pricing: Revenue Target / Planned Pallets = Price per Pallet.

        No tiers, no temperature surcharge, no liftgate, no FSA adjustments.
        Returns dict: {price_per_pallet, total, revenue_target, planned_pallets}
        """
        revenue_target = lane.revenue_target or lane.preferred_revenue_target or 0.0
        if not revenue_target:
            # Try default targets
            origin_code = lane.origin_region_id.code
            dest_code = lane.destination_region_id.code
            revenue_target = DEFAULT_TARGETS.get((origin_code, dest_code), 0.0)

        planned_pallets = lane.target_load_pallets or 8
        if planned_pallets <= 0:
            planned_pallets = 8

        price_per_pallet = round(revenue_target / planned_pallets, 2)
        total = round(price_per_pallet * pallets, 2)

        return {
            "price_per_pallet": price_per_pallet,
            "total": total,
            "revenue_target": revenue_target,
            "planned_pallets": planned_pallets,
            "formula": f"${revenue_target:,.2f} / {planned_pallets} pallets = ${price_per_pallet:,.2f}/pallet",
        }

    # ── Phase 10: Revenue Target Suggestion ───────────────────────────

    def suggest_revenue_target(self, lane):
        """Auto-suggest revenue target based on: distance × regional rate.

        Formula: road_km × avg(origin.rate_per_km, dest.rate_per_km)
        Rounded to nearest 25. Operator may override.
        """
        if not lane.road_km or lane.road_km <= 0:
            return None

        origin_rate = lane.origin_region_id.rate_per_km or 3.00
        dest_rate = lane.destination_region_id.rate_per_km or 2.80
        avg_rate = (origin_rate + dest_rate) / 2.0

        suggested = lane.road_km * avg_rate
        # Round to nearest 25
        rounded = round(suggested / 25) * 25
        # Minimum floor
        if rounded < 350:
            rounded = 350

        return {
            "suggested_target": float(rounded),
            "raw": round(suggested, 2),
            "distance_km": lane.road_km,
            "rate_per_km": round(avg_rate, 2),
            "origin_rate": origin_rate,
            "dest_rate": dest_rate,
        }

    def apply_suggested_target(self, lane):
        """Apply the suggested revenue target to the lane."""
        suggestion = self.suggest_revenue_target(lane)
        if suggestion:
            lane.write({"revenue_target": suggestion["suggested_target"]})
            return suggestion
        return None

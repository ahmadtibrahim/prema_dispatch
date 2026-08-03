"""Ordered-lane route resolver.

Rate Plans are the sole pricing authority. No per-km, AI, road-distance,
or DEFAULT_TARGETS fallback. Returns request_quote for unresolvable routes.
"""
import logging
from collections import namedtuple
from datetime import date

_logger = logging.getLogger(__name__)

ResolvedRoute = namedtuple("ResolvedRoute", [
    "available", "reason", "legs", "total_pallets", "total_weight_lbs",
])


class RouteResolver:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def resolve(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment="dry", partner=None, shipment_type="ltl"):
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return ResolvedRoute(False, "pickup_fsa_not_supported", [], pallets, weight_lbs)
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return ResolvedRoute(False, "delivery_fsa_not_supported", [], pallets, weight_lbs)
        pu_region = pickup_fsa.region_id
        del_region = delivery_fsa.region_id
        if not pu_region or not del_region:
            return ResolvedRoute(False, "fsa_not_mapped_to_region", [], pallets, weight_lbs)
        if pallets > 12:
            return ResolvedRoute(False, "pallets_exceed_standard_capacity", [], pallets, weight_lbs)

        today = date.today()

        # 1. Customer-specific
        if partner:
            cr = self._resolve_customer_route(
                partner, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today
            )
            if cr and cr.available:
                return cr

        # 2. Direct ordered lane
        direct = self._resolve_direct_lane(pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today)
        if direct and direct.available:
            return direct

        # 3. Hub transfer
        hub = self._resolve_hub_transfer(pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today)
        if hub and hub.available:
            return hub

        return ResolvedRoute(False, "request_quote", [], pallets, weight_lbs)

    # ── Rate Plan lookup ──────────────────────────────────────────────

    def _find_active_rate_plan(self, origin_region, dest_region, equipment, shipment_type, today):
        """Find an active, effective Rate Plan for an exact ordered lane."""
        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", origin_region.id),
            ("destination_region_id", "=", dest_region.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return None, None

        # Equipment capability
        if equipment == "reefer" and not getattr(lane, "reefer_supported", True):
            return lane, None
        if shipment_type == "ltl" and not lane.ltl_capable:
            return lane, None
        if shipment_type == "ftl" and not lane.ftl_capable:
            return lane, None

        # Find active offering (dry temperature only — reefer same base price)
        Offering = self.env["logistics.service.offering"]
        offering = Offering.search([
            ("lane_id", "=", lane.id),
            ("active", "=", True),
            ("temperature_mode", "=", "dry"),
        ], limit=1)
        if not offering:
            return lane, None

        RatePlan = self.env["logistics.rate.plan"]
        rate_plan = RatePlan.search([
            ("service_offering_id", "=", offering.id),
            ("active", "=", True),
            "|", ("effective_from", "=", False), ("effective_from", "<=", today),
            "|", ("effective_to", "=", False), ("effective_to", ">=", today),
        ], order="version desc", limit=1)
        return lane, rate_plan

    # ── Customer route ─────────────────────────────────────────────────

    def _resolve_customer_route(self, partner, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today):
        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", pu_region.id),
            ("destination_region_id", "=", del_region.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return None

        CustomerRate = self.env["logistics.customer.rate"]
        domain = [
            ("partner_id", "=", partner.commercial_partner_id.id),
            ("active", "=", True),
            "|", ("effective_from", "=", False), ("effective_from", "<=", today),
            "|", ("effective_to", "=", False), ("effective_to", ">=", today),
        ]
        cr = CustomerRate.search(domain + [("lane_id", "=", lane.id)], limit=1)
        if not cr:
            return None

        if cr.override_rate_plan_id:
            rp = cr.override_rate_plan_id
            # Validate override belongs to this exact lane
            if rp.lane_id != lane or not rp.active:
                _logger.warning("Customer rate %s override plan %s invalid for lane %s", cr.id, rp.id, lane.id)
                return None
            # Validate effective dates
            if rp.effective_from and rp.effective_from > today:
                return None
            if rp.effective_to and rp.effective_to < today:
                return None
        else:
            _, rp = self._find_active_rate_plan(pu_region, del_region, equipment, shipment_type, today)

        if not rp:
            return None

        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines, total = svc._compute_price(rp, pallets, "dry", weight_lbs=weight_lbs)
        if cr.discount_pct:
            total = total * (1.0 - cr.discount_pct / 100.0)

        return ResolvedRoute(True, None, [{
            "lane": lane, "rate_plan": rp,
            "origin_region": pu_region.code, "dest_region": del_region.code,
            "price": total, "price_lines": lines,
            "customer_rate_applied": True, "discount_pct": cr.discount_pct,
        }], pallets, weight_lbs)

    # ── Direct lane ────────────────────────────────────────────────────

    def _resolve_direct_lane(self, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today):
        lane, rate_plan = self._find_active_rate_plan(pu_region, del_region, equipment, shipment_type, today)
        if not rate_plan:
            return None
        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines, total = svc._compute_price(rate_plan, pallets, "dry", weight_lbs=weight_lbs)
        return ResolvedRoute(True, None, [{
            "lane": lane, "rate_plan": rate_plan,
            "origin_region": pu_region.code, "dest_region": del_region.code,
            "price": total, "price_lines": lines,
            "customer_rate_applied": False, "discount_pct": 0.0,
        }], pallets, weight_lbs)

    # ── Hub transfer ───────────────────────────────────────────────────

    def _resolve_hub_transfer(self, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today):
        Hub = self.env["logistics.hub"]
        hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return None

        # Use explicit canonical_region_id field — never guess
        hub_region = hub.canonical_region_id
        if not hub_region:
            _logger.warning("Hub %s has no canonical_region_id — cannot route transfer", hub.code)
            return None
        if pu_region == hub_region or del_region == hub_region:
            return None

        leg1_lane, leg1_rp = self._find_active_rate_plan(pu_region, hub_region, equipment, shipment_type, today)
        if not leg1_rp:
            return None
        leg2_lane, leg2_rp = self._find_active_rate_plan(hub_region, del_region, equipment, shipment_type, today)
        if not leg2_rp:
            return None

        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines1, total1 = svc._compute_price(leg1_rp, pallets, "dry", weight_lbs=weight_lbs)
        lines2, total2 = svc._compute_price(leg2_rp, pallets, "dry", weight_lbs=weight_lbs)

        return ResolvedRoute(True, None, [
            {"lane": leg1_lane, "rate_plan": leg1_rp,
             "origin_region": pu_region.code, "dest_region": hub_region.code,
             "price": total1, "price_lines": lines1,
             "customer_rate_applied": False, "discount_pct": 0.0},
            {"lane": leg2_lane, "rate_plan": leg2_rp,
             "origin_region": hub_region.code, "dest_region": del_region.code,
             "price": total2, "price_lines": lines2,
             "customer_rate_applied": False, "discount_pct": 0.0},
        ], pallets, weight_lbs)

"""Ordered-lane route resolver.

Resolves pickup/delivery FSAs into ordered lanes, preferring direct routes
and falling back to hub transfers through confirmed canonical hubs only.

An ordered lane is directional: Origin→Destination ≠ Destination→Origin.
"""
import logging
from collections import namedtuple

_logger = logging.getLogger(__name__)

ResolvedRoute = namedtuple("ResolvedRoute", [
    "available", "reason", "legs", "total_pallets", "total_weight_lbs",
])


class RouteResolver:
    """Resolve a shipment into one or more ordered lanes via confirmed hubs."""

    def __init__(self, env):
        # Accept either a regular env or an already-sudoed env
        # env(su=True) returns a new sudo Environment — use it directly
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Public API ────────────────────────────────────────────────────

    def resolve(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment="dry", partner=None):
        """Return a ResolvedRoute for the given shipment.

        Precedence:
          1. Exact customer-specific Rate Plan
          2. Exact ordered-lane default Rate Plan
          3. Hub-transfer through a canonical hub (both legs must have active plans)
          4. Request Quote (not available)
        """
        # Validate FSAs
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return ResolvedRoute(False, "pickup_fsa_not_supported", [], pallets, weight_lbs)
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return ResolvedRoute(False, "delivery_fsa_not_supported", [], pallets, weight_lbs)

        pu_region = pickup_fsa.region_id
        del_region = delivery_fsa.region_id
        if not pu_region or not del_region:
            return ResolvedRoute(False, "fsa_not_mapped_to_region", [], pallets, weight_lbs)

        # Capacity gate: >12 pallets requires custom quote
        if pallets > 12:
            return ResolvedRoute(False, "pallets_exceed_standard_capacity", [], pallets, weight_lbs)

        # 1. Customer-specific rate plan
        if partner:
            customer_route = self._resolve_customer_route(
                partner, pu_region, del_region, equipment, pallets, weight_lbs
            )
            if customer_route and customer_route.available:
                return customer_route

        # 2. Direct ordered lane
        direct = self._resolve_direct_lane(pu_region, del_region, equipment, pallets, weight_lbs)
        if direct and direct.available:
            return direct

        # 3. Hub transfer through canonical hub
        hub_route = self._resolve_hub_transfer(pu_region, del_region, equipment, pallets, weight_lbs)
        if hub_route and hub_route.available:
            return hub_route

        # 4. Request quote
        return ResolvedRoute(False, "request_quote", [], pallets, weight_lbs)

    # ── Internal resolvers ────────────────────────────────────────────

    def _find_active_rate_plan(self, origin_region, dest_region, equipment="dry"):
        """Find the active Rate Plan for an exact ordered lane."""
        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", origin_region.id),
            ("destination_region_id", "=", dest_region.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return None, None

        # Find active rate plan through service offerings
        Offering = self.env["logistics.service.offering"]
        RatePlan = self.env["logistics.rate.plan"]
        # Only dry temperature offerings (reefer priced same)
        offering = Offering.search([
            ("lane_id", "=", lane.id),
            ("active", "=", True),
            ("temperature_mode", "=", "dry"),
        ], limit=1)
        if not offering:
            return lane, None

        rate_plan = RatePlan.search([
            ("service_offering_id", "=", offering.id),
            ("active", "=", True),
        ], order="version desc", limit=1)
        return lane, rate_plan

    def _resolve_customer_route(self, partner, pu_region, del_region, equipment, pallets, weight_lbs):
        """Check for customer-specific rate assignment."""
        CustomerRate = self.env["logistics.customer.rate"]
        today = self.env["logistics.customer.rate"]._fields.get("effective_from") and True
        # Search customer rate for this partner on matching lane
        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", pu_region.id),
            ("destination_region_id", "=", del_region.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return None

        domain = [("partner_id", "=", partner.commercial_partner_id.id), ("active", "=", True)]
        # If lane is specified, filter
        customer_rate = CustomerRate.search(domain + [("lane_id", "=", lane.id)], limit=1)
        if not customer_rate:
            customer_rate = CustomerRate.search(domain + [("lane_id", "=", False)], limit=1)

        if not customer_rate:
            return None

        if customer_rate.override_rate_plan_id:
            # Customer has a specific rate plan override
            rate_plan = customer_rate.override_rate_plan_id
        else:
            _, rate_plan = self._find_active_rate_plan(pu_region, del_region, equipment)

        if not rate_plan:
            return None

        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines, total = svc._compute_price(rate_plan, pallets, "dry", weight_lbs=weight_lbs)
        # Apply customer discount if any
        if customer_rate.discount_pct:
            total = total * (1.0 - customer_rate.discount_pct / 100.0)

        return ResolvedRoute(True, None, [{
            "lane": lane,
            "rate_plan": rate_plan,
            "origin_region": pu_region.code,
            "dest_region": del_region.code,
            "price": total,
            "price_lines": lines,
            "customer_rate_applied": True,
            "discount_pct": customer_rate.discount_pct,
        }], pallets, weight_lbs)

    def _resolve_direct_lane(self, pu_region, del_region, equipment, pallets, weight_lbs):
        """Resolve a direct ordered lane."""
        lane, rate_plan = self._find_active_rate_plan(pu_region, del_region, equipment)
        if not rate_plan:
            return None

        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines, total = svc._compute_price(rate_plan, pallets, "dry", weight_lbs=weight_lbs)

        return ResolvedRoute(True, None, [{
            "lane": lane,
            "rate_plan": rate_plan,
            "origin_region": pu_region.code,
            "dest_region": del_region.code,
            "price": total,
            "price_lines": lines,
            "customer_rate_applied": False,
            "discount_pct": 0.0,
        }], pallets, weight_lbs)

    def _resolve_hub_transfer(self, pu_region, del_region, equipment, pallets, weight_lbs):
        """Resolve through the canonical hub (Mississauga/YYZ-HUB).

        Route: pickup_region → hub_region → delivery_region
        Both legs must have active rate plans.
        """
        Hub = self.env["logistics.hub"]
        hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return None

        # Determine hub region from the hub's supported regions or default to R1
        hub_region = None
        if hub.supported_region_ids:
            hub_region = hub.supported_region_ids[0]
        else:
            # Default: Mississauga Hub → R1 (GTA Central)
            Region = self.env["logistics.region"]
            hub_region = Region.search([("code", "=", "R1")], limit=1)

        if not hub_region:
            return None

        # Don't transfer through hub if origin or destination IS the hub
        if pu_region == hub_region or del_region == hub_region:
            return None

        # Leg 1: pickup → hub
        leg1_lane, leg1_rp = self._find_active_rate_plan(pu_region, hub_region, equipment)
        if not leg1_rp:
            return None

        # Leg 2: hub → delivery
        leg2_lane, leg2_rp = self._find_active_rate_plan(hub_region, del_region, equipment)
        if not leg2_rp:
            return None

        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines1, total1 = svc._compute_price(leg1_rp, pallets, "dry", weight_lbs=weight_lbs)
        lines2, total2 = svc._compute_price(leg2_rp, pallets, "dry", weight_lbs=weight_lbs)

        return ResolvedRoute(True, None, [
            {
                "lane": leg1_lane,
                "rate_plan": leg1_rp,
                "origin_region": pu_region.code,
                "dest_region": hub_region.code,
                "price": total1,
                "price_lines": lines1,
                "customer_rate_applied": False,
                "discount_pct": 0.0,
            },
            {
                "lane": leg2_lane,
                "rate_plan": leg2_rp,
                "origin_region": hub_region.code,
                "dest_region": del_region.code,
                "price": total2,
                "price_lines": lines2,
                "customer_rate_applied": False,
                "discount_pct": 0.0,
            },
        ], pallets, weight_lbs)

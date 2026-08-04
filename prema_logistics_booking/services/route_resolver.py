"""Ordered-lane route resolver — Rate Plans are the sole pricing authority."""
import logging
from collections import namedtuple
from datetime import date

from .temperature_compat import to_canonical_temperature_mode, DRY, REEFER

_logger = logging.getLogger(__name__)

ResolvedRoute = namedtuple("ResolvedRoute", [
    "available", "reason", "legs", "total_pallets", "total_weight_lbs",
])

VALID_SHIPMENT_TYPES = {"ltl", "ftl"}
VALID_EQUIPMENT = {DRY, REEFER}
# Deprecated alias — kept only so any lingering external import doesn't
# hard-crash. New code must use temperature_compat.to_canonical_temperature_mode.
LEGACY_CHILLED_FROZEN = {"chilled", "frozen"}


class RouteResolver:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def resolve(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                equipment="dry", partner=None, shipment_type="ltl", reference_dt=None):
        # Validate
        if shipment_type not in VALID_SHIPMENT_TYPES:
            return ResolvedRoute(False, "invalid_shipment_type", [], pallets, weight_lbs)
        if equipment not in VALID_EQUIPMENT:
            return ResolvedRoute(False, "invalid_equipment", [], pallets, weight_lbs)
        if not pickup_fsa or not pickup_fsa.pickup_supported:
            return ResolvedRoute(False, "pickup_fsa_not_supported", [], pallets, weight_lbs)
        if not delivery_fsa or not delivery_fsa.delivery_supported:
            return ResolvedRoute(False, "delivery_fsa_not_supported", [], pallets, weight_lbs)
        pu_region = pickup_fsa.region_id
        del_region = delivery_fsa.region_id
        if not pu_region or not del_region:
            return ResolvedRoute(False, "fsa_not_mapped_to_region", [], pallets, weight_lbs)
        # Capacity evaluated against departure vehicle, not hardcoded number

        today = reference_dt.date() if reference_dt else date.today()

        # Chilled/Frozen → Reefer for capability checking (canonical adapter)
        equip_check = to_canonical_temperature_mode(equipment)

        # 1. Customer-specific
        if partner:
            cr = self._resolve_customer_route(
                partner, pu_region, del_region, equip_check, shipment_type, pallets, weight_lbs, today
            )
            if cr and cr.available:
                return cr

        # 2. Direct ordered lane
        direct = self._resolve_direct_lane(pu_region, del_region, equip_check, shipment_type, pallets, weight_lbs, today)
        if direct and direct.available:
            return direct

        # 3. Hub transfer
        hub = self._resolve_hub_transfer(pu_region, del_region, equip_check, shipment_type, pallets, weight_lbs, today)
        if hub and hub.available:
            return hub

        return ResolvedRoute(False, "request_quote", [], pallets, weight_lbs)

    # ── Offering + Rate Plan resolution ───────────────────────────────

    def _find_offerings(self, lane, shipment_type, today):
        """Return list of active offerings matching lane, type, and date."""
        domain = [
            ("lane_id", "=", lane.id),
            ("active", "=", True),
        ]
        offerings = self.env["logistics.service.offering"].search(domain)
        # Filter by shipment type
        return [o for o in offerings if o.shipment_type in (shipment_type, "both")]

    def find_rate_plan_for_regions(self, origin_region, dest_region, equipment="dry", shipment_type="ltl", today=None):
        """Public wrapper around _find_active_rate_plan for callers that only
        need to know "is there a commercially active Rate Plan for this
        region pair" (e.g. a corridor's read-only effective-Rate-Plan
        summary) without going through the full FSA-based resolve()."""
        today = today or date.today()
        lane, rate_plan = self._find_active_rate_plan(origin_region, dest_region, equipment, shipment_type, today)
        return rate_plan

    def _find_active_rate_plan(self, origin_region, dest_region, equipment, shipment_type, today):
        """Find the active, effective Rate Plan for an exact ordered lane.
        Returns None if zero or multiple offerings match."""
        Lane = self.env["logistics.lane"]
        lane = Lane.search([
            ("origin_region_id", "=", origin_region.id),
            ("destination_region_id", "=", dest_region.id),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            return None, None

        # Equipment capability: reefer needs reefer_supported
        if equipment == "reefer" and not getattr(lane, "reefer_supported", False):
            return lane, None
        if shipment_type == "ltl" and not lane.ltl_capable:
            return lane, None
        if shipment_type == "ftl" and not lane.ftl_capable:
            return lane, None

        offerings = self._find_offerings(lane, shipment_type, today)
        if not offerings:
            return lane, None
        if len(offerings) > 1:
            _logger.warning("Lane %s: %d offerings match — ambiguous, returning request_quote", lane.id, len(offerings))
            return lane, None

        offering = offerings[0]
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
            if rp.lane_id != lane or not rp.active:
                return None
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
        return ResolvedRoute(True, None, [self._leg_dict(lane, rp, pu_region.code, del_region.code, total, lines, True, cr.discount_pct)], pallets, weight_lbs)

    # ── Direct lane ────────────────────────────────────────────────────

    def _resolve_direct_lane(self, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today):
        lane, rate_plan = self._find_active_rate_plan(pu_region, del_region, equipment, shipment_type, today)
        if not rate_plan:
            return None
        from ..services.pricing_service import PricingService
        svc = PricingService(self.env)
        lines, total = svc._compute_price(rate_plan, pallets, "dry", weight_lbs=weight_lbs)
        return ResolvedRoute(True, None, [self._leg_dict(lane, rate_plan, pu_region.code, del_region.code, total, lines, False, 0.0)], pallets, weight_lbs)

    # ── Hub transfer ───────────────────────────────────────────────────

    def _resolve_hub_transfer(self, pu_region, del_region, equipment, shipment_type, pallets, weight_lbs, today):
        Hub = self.env["logistics.hub"]
        hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return None
        hub_region = hub.canonical_region_id
        if not hub_region:
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
            self._leg_dict(leg1_lane, leg1_rp, pu_region.code, hub_region.code, total1, lines1, False, 0.0),
            self._leg_dict(leg2_lane, leg2_rp, hub_region.code, del_region.code, total2, lines2, False, 0.0),
        ], pallets, weight_lbs)

    @staticmethod
    def _leg_dict(lane, rp, origin, dest, price, lines, cr_applied, discount):
        offering = rp.service_offering_id
        currency = rp.currency_id
        return {
            "lane": lane, "rate_plan": rp,
            "lane_id": lane.id,
            "lane_name": lane.name,
            "offering_id": offering.id if offering else False,
            "offering_name": offering.name if offering else "",
            "rate_plan_id": rp.id,
            "rate_plan_name": rp.name,
            "rate_plan_version": rp.version,
            "currency_id": currency.id if currency else False,
            "currency_code": currency.name if currency else "",
            "origin_region": origin, "dest_region": dest,
            "price": price, "price_lines": lines,
            "customer_rate_applied": cr_applied, "discount_pct": discount,
        }

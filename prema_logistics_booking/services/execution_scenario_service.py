# ════════════════════════════════════════════════════════════════════
# Phases 12 + 16 — Execution Scenario Engine.
#   For each shipment, generate BOUNDED feasible execution alternatives
#   and compare them (own fleet / scheduled network / hub splits /
#   subcontract mixes) following the 16.1 fallback hierarchy. The engine
#   RECOMMENDS; the dispatcher decides (RULE 5). Never returns NO SERVICE
#   before every configured alternative has been evaluated.
#
#   BUY cost hierarchy (12.3): accepted offer → carrier lane rate card →
#   carrier quote → configured market authority → CARRIER RATE REQUIRED.
#   Own-fleet cost reuses the booking's Prema AI estimator authority.
# ════════════════════════════════════════════════════════════════════
import logging

from odoo import _

_logger = logging.getLogger(__name__)

MAX_SCENARIOS = 8


class ExecutionScenarioService:
    def __init__(self, env):
        self.env = env

    # ── Configuration authorities (single ir.config_parameter each) ──
    def _param(self, key, default):
        try:
            return self.env["ir.config_parameter"].sudo().get_param(
                key, str(default))
        except Exception:
            return str(default)

    def hub_transfer_cost(self):
        return float(self._param("logistics.hub_transfer_cost", 50.0) or 0.0)

    def minimum_margin_pct(self):
        return float(self._param("logistics.minimum_margin_pct", 10.0) or 0.0)

    def market_buy_rate_per_km(self):
        """Configured estimated market BUY authority (0.0 = none → a
        subcontract scenario without an accepted rate/lane rate is
        CARRIER RATE REQUIRED — never invented profitability)."""
        return float(self._param(
            "logistics.market_buy_rate_per_km", 0.0) or 0.0)

    def allow_cross_border_subcontract(self):
        """Explicit brokerage/interlining authority. Without it a
        Canada→USA subcontract returns COMPLIANCE REVIEW REQUIRED."""
        return self._param(
            "logistics.allow_cross_border_subcontract", "False").lower() \
            in ("1", "true", "yes")

    # ── Carrier BUY estimation ──────────────────────────────────────

    def _carrier_buy_cost(self, carrier, origin_region, dest_region,
                          equipment="dry", distance_km=0.0, leg=False):
        """Hierarchy: accepted offer → lane rate card → market authority.
        Returns (cost, source) or (False, 'carrier_rate_required')."""
        if leg:
            offer = self.env["logistics.booking.leg.carrier.offer"].search([
                ("booking_leg_id", "=", leg.id),
                ("state", "=", "accepted"),
            ], limit=1)
            if offer and offer.agreed_rate:
                return offer.agreed_rate, "carrier_accepted"
        rate = self.env["logistics.carrier.lane.rate"]._match(
            carrier.id, origin_region.id, dest_region.id, equipment)
        if rate:
            return rate._buy_cost(distance_km), "carrier_rate_card"
        market = self.market_buy_rate_per_km()
        if market > 0:
            return round(market * max(distance_km, 0.0), 2), "carrier_quote"
        return False, "carrier_rate_required"

    # ── Own-fleet BUY estimation (reuse the booking estimator) ──────

    def _own_fleet_cost(self, booking, distance_km=0.0, duration_hrs=0.0):
        """Own-fleet BUY authority: the booking's frozen per-leg estimates
        when they exist (they came from the pricing engine / Prema AI
        estimator), else the live estimator call. Never invents a figure —
        a 0.0 result means the estimator degraded (200 km default + no
        vehicle) and the dispatcher sees it as a low-cost scenario."""
        legs = booking.leg_ids.filtered(
            lambda l: l.execution_mode in ("own_fleet", "unassigned"))
        leg_costs = [l.estimated_leg_cost for l in legs
                     if l.estimated_leg_cost]
        if leg_costs:
            return round(sum(leg_costs), 2), "leg_estimates"
        try:
            est = booking._estimate_cost_from_request(
                {"pallets": getattr(booking, "pallets", None) or 1})
            if est and est.get("total_cost"):
                return float(est["total_cost"]), "own_cost_estimate"
        except Exception:
            pass
        return 0.0, "own_cost_estimate"

    # ── Scenario generation ─────────────────────────────────────────

    def generate(self, booking):
        """Build the bounded scenario set (16.1 order), persist them as
        logistics.execution.scenario records and return them."""
        booking.ensure_one()
        self.env["logistics.execution.scenario"].search([
            ("booking_id", "=", booking.id),
        ]).unlink()
        scenarios = []
        legs = booking.leg_ids
        origin_region = legs[:1].origin_region_id if legs else False
        dest_region = legs and legs[-1:].destination_region_id or False
        equipment = booking.temperature_mode or "dry"
        distance_km = sum(l.estimated_distance_km for l in legs
                          if "estimated_distance_km" in l._fields)
        if not distance_km and len(legs) == 1 and \
                "estimated_distance_km" in legs[0]._fields:
            distance_km = legs[0].estimated_distance_km

        # 1-3. Scheduled PREMAFIRM service (existing routing authority).
        has_route = bool(legs)
        if has_route:
            own = sum(l.estimated_leg_cost or 0.0 for l in legs
                      if l.execution_mode in ("own_fleet", "unassigned"))
            sub = sum(l.accepted_buy_rate or 0.0 for l in legs
                      if l.execution_mode == "subcontracted")
            cost = own + sub
            state = "auto_bookable"
            if any(l.execution_mode == "subcontracted" for l in legs):
                state = "carrier_acceptance_required"
            scenarios.append({
                "rank": 1,
                "title": "Scheduled PREMAFIRM network",
                "state": state,
                "cost": cost,
                "plan": [{
                    "leg": l.id,
                    "execution_mode": l.execution_mode,
                    "carrier_id": l.executing_carrier_id.id
                    if l.executing_carrier_id else False,
                    "buy_rate": l.accepted_buy_rate or l.estimated_leg_cost,
                    "cost_source": l.cost_source,
                } for l in legs],
            })

        # 4. Own-fleet dedicated direct (always evaluated — the fleet may
        # be cheaper than the scheduled network).
        own_cost, src = self._own_fleet_cost(booking, distance_km)
        scenarios.append({
            "rank": 2,
            "title": "Own fleet dedicated direct",
            "state": "auto_bookable" if own_cost or not distance_km
            else "dispatch_confirmation",
            "cost": own_cost,
            "plan": [{
                "leg": False,
                "execution_mode": "own_fleet",
                "buy_rate": own_cost,
                "cost_source": src,
            }],
        })

        # 5-7. Subcontract mixes via the hub split (bounded — one hub).
        hub_cost = self.hub_transfer_cost()
        carriers = self.env["res.partner"].search(
            [("is_transport_carrier", "=", True),
             ("carrier_status", "=", "active")], limit=3)
        for i, carrier in enumerate(carriers):
            buy, src = self._carrier_buy_cost(
                carrier, origin_region, dest_region, equipment, distance_km)
            if src == "carrier_rate_required":
                scenarios.append({
                    "rank": 10 + i,
                    "title": "Subcontract %s — rate required" % carrier.name,
                    "state": "carrier_rate_required",
                    "cost": False,
                    "plan": [{
                        "leg": False,
                        "execution_mode": "subcontracted",
                        "carrier_id": carrier.id,
                        "buy_rate": False,
                        "cost_source": "carrier_rate_required",
                    }],
                })
            else:
                scenarios.append({
                    "rank": 10 + i,
                    "title": "Subcontract %s (%s)" % (carrier.name, src),
                    "state": "carrier_acceptance_required",
                    "cost": buy + hub_cost,
                    "plan": [{
                        "leg": False,
                        "execution_mode": "subcontracted",
                        "carrier_id": carrier.id,
                        "buy_rate": buy,
                        "cost_source": src,
                    }],
                })
        scenarios = scenarios[:MAX_SCENARIOS]

        # 8. Manual Dispatch Review — always the honest fallback.
        scenarios.append({
            "rank": 99,
            "title": "Manual Dispatch Review",
            "state": "manual_review",
            "cost": False,
            "plan": [{"leg": False, "execution_mode": "unassigned",
                      "buy_rate": False, "cost_source": "manual"}],
        })

        # Compliance guard (16.6): cross-border subcontract needs the
        # configured authority — otherwise COMPLIANCE REVIEW REQUIRED.
        revenue = booking.calculated_price or 0.0
        records = self.env["logistics.execution.scenario"]
        for sc in scenarios:
            cost = sc["cost"] if sc["cost"] is not False else 0.0
            margin = revenue - cost if sc["cost"] is not False else False
            state = sc["state"]
            if state in ("carrier_acceptance_required",
                         "carrier_rate_required") and \
                    origin_region and dest_region and \
                    origin_region.country_id != dest_region.country_id and \
                    not self.allow_cross_border_subcontract():
                state = "manual_review"
                sc["compliance"] = True
            records |= self.env["logistics.execution.scenario"].create({
                "booking_id": booking.id,
                "rank": sc["rank"],
                "state": state,
                "customer_revenue": revenue,
                "estimated_total_cost": round(cost, 2)
                if sc["cost"] is not False else 0.0,
                "estimated_margin": round(margin, 2)
                if margin is not False else 0.0,
                "estimated_margin_pct": round(margin / revenue * 100.0, 2)
                if margin is not False and revenue > 0 else 0.0,
                "execution_plan": sc["plan"],
                "compliance_review": bool(sc.get("compliance")),
            })
        return records

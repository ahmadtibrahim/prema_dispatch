"""Estimator bridge — the Dispatch-side authority surface the Prema AI
estimator calls (master §1 cross-repo contract, §9-13).

Prema Dispatch owns pricing/availability/capacity/scheduling authority;
Prema AI extracts, drafts and advises.  This AbstractModel is the ONLY
entry point the AI side uses for scenario, availability, pricing-intel,
pairing and route-development data.  Everything it returns is read-only
advisory — creating quotes, bookings or plan changes stays behind explicit
access-controlled actions on the Dispatch side.

Rules:
  - Every public method returns a dict and never raises: failures are
    {"error": code, "message": ...} so the AI side can show the reason.
  - Engine identity checks: the engine only calls this when
    'logistics.estimator.bridge' is in its registry (guarded env lookup).
"""

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class LogisticsEstimatorBridge(models.AbstractModel):
    _name = "logistics.estimator.bridge"
    _description = "Prema AI Estimator Bridge (dispatch authority)"

    # ── Scenario cards (§10) + intel (§12) + pairing (§13) ──────────

    def estimate_request(self, payload):
        """One canonical estimator response: the three scenario cards with
        the customer-intel and pairing evidence attached.  Read-only."""
        try:
            payload = payload or {}
            from ..services.estimator_scenario_service import \
                EstimatorScenarioService
            result = EstimatorScenarioService(self.env).build_cards(payload)

            intel = {}
            partner_id = int(payload.get("partner_id") or 0) or False
            if partner_id:
                from ..services.customer_pricing_intel_service import \
                    CustomerPricingIntelService
                intel = CustomerPricingIntelService(self.env).intel_report(
                    partner_id, payload)

            pairing_payload = dict(payload)
            pairing_payload["pickup_date"] = \
                (result.get("d0") or payload.get("pickup_date")
                 or False)
            from ..services.load_pairing_service import LoadPairingService
            pairing = LoadPairingService(self.env).pairing_report(
                pairing_payload)

            return {
                "ok": True,
                "scenarios": result.get("scenarios", []),
                "fatal": result.get("fatal", False),
                "message": result.get("message", ""),
                "warnings": result.get("warnings", []),
                "capacity": result.get("capacity", {}),
                "d0": result.get("d0"),
                "requested_date": result.get("requested_date"),
                "intel": intel,
                "pairing": pairing,
            }
        except Exception as exc:
            _logger.exception("estimator bridge estimate_request failed")
            return {"error": "estimate_request_failed",
                    "message": str(exc)[:300]}

    # ── Route development week view (§13) ───────────────────────────

    def route_development(self, vehicle_id=0, week_start=None,
                          fsa_codes=None):
        """Scheduled corridor week for the truck + the request's region
        pair, development gaps, and region city context for AI lead
        matching.  Read-only advisory."""
        try:
            from ..services.route_development_service import \
                RouteDevelopmentService
            return RouteDevelopmentService(self.env).route_dev_report(
                vehicle_id, week_start, fsa_codes or [])
        except Exception as exc:
            _logger.exception("estimator bridge route_development failed")
            return {"error": "route_development_failed",
                    "message": str(exc)[:300]}

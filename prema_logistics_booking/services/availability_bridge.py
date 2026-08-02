"""Thin, read-only wrapper around prema_dispatch's existing capacity
services — Layer 2 (real operational capacity) of the hybrid availability
design. Deliberately does NOT reimplement fleet/capacity logic; it only
translates this module's inputs into what DispatchFeasibilityService already
expects. prema_dispatch itself is never modified by this file.
"""


class AvailabilityBridge:
    def __init__(self, env):
        # sudo(): prema_dispatch's feasibility service reads fleet.vehicle /
        # dispatch jobs, which a customer has no ACL on. The authorization
        # decision (may this booking be confirmed at all) already happened
        # in logistics.booking.confirm_from_session before this is ever
        # called -- same pattern as PricingService/ScheduleService.
        self.env = env(su=True)

    def check_real_capacity(self, pickup_address, delivery_address, pickup_date,
                             pallets, weight_lbs, requires_reefer, requires_liftgate):
        try:
            from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService
        except ImportError:
            # prema_dispatch not available for some reason (should never
            # happen given the hard module dependency) — fail safe to "can't
            # confirm capacity" rather than silently allow overbooking.
            return {"feasible": False, "reason": "capacity_service_unavailable"}

        service = DispatchFeasibilityService(self.env)
        payload = {
            "pickup_address": pickup_address,
            "dropoff_address": delivery_address,
            "check_date": pickup_date,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "requires_reefer": requires_reefer,
            "requires_liftgate": requires_liftgate,
        }
        try:
            result = service.check(payload)
        except Exception:
            # Fail safe: a geocoding/network hiccup in the underlying
            # feasibility check must never let a booking through unverified,
            # and must never surface a raw 500 to the customer either.
            return {"feasible": False, "reason": "capacity_check_failed"}
        verdict = result.get("verdict")
        return {
            "feasible": verdict in ("feasible", "risky"),
            "verdict": verdict,
            "reason": result.get("reason"),
            "options": result.get("options", []),
        }

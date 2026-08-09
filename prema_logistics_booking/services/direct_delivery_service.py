"""Direct Delivery Service — canonical routing decision authority.

Determines whether freight between two service regions can bypass the Hub.

Default: HUB_TRANSFER_REQUIRED (safest operational default).
"""

import logging
from collections import namedtuple
from datetime import datetime

_logger = logging.getLogger(__name__)

RoutingDecision = namedtuple("RoutingDecision", [
    "decision",               # 'DIRECT_ALLOWED' | 'HUB_TRANSFER_REQUIRED' | 'MANUAL_REVIEW'
    "matched_rule",           # logistics.direct.delivery.rule record or None
    "direct_allowed",         # bool
    "hub_transfer_required",  # bool
    "applicable_corridor_id", # logistics.corridor record or None
    "reason_code",            # str — machine-readable
    "reason_text",            # str — human-readable
    "evaluated_day",          # str — day that was evaluated
    "restrictions",           # list of str — any restrictions that applied
    "decision_timestamp",     # str — ISO timestamp
])


class DirectDeliveryService:
    """Canonical service for determining direct vs hub-transfer routing."""

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Public API ───────────────────────────────────────────────────

    def decide(self, origin_region_id, destination_region_id,
               pickup_day=None, road_distance_km=None, pickup_time=None,
               delivery_deadline=None):
        """Determine whether direct delivery is allowed for a region pair.

        Args:
            origin_region_id: logistics.region ID or record
            destination_region_id: logistics.region ID or record
            pickup_day: lowercase day name (e.g. 'monday')
            road_distance_km: Google road distance in km
            pickup_time: float hours (e.g. 8.5 = 8:30 AM)
            delivery_deadline: float hours for latest delivery

        Returns:
            RoutingDecision namedtuple
        """
        timestamp = datetime.utcnow().isoformat() + "Z"
        Region = self.env["logistics.region"]
        Rule = self.env["logistics.direct.delivery.rule"]

        # Resolve records
        origin = self._resolve_region(origin_region_id)
        dest = self._resolve_region(destination_region_id)
        if not origin or not dest:
            return RoutingDecision(
                decision="MANUAL_REVIEW", matched_rule=None,
                direct_allowed=False, hub_transfer_required=True,
                applicable_corridor_id=None,
                reason_code="INVALID_REGION",
                reason_text="Origin or destination region not found.",
                evaluated_day=pickup_day or "unknown",
                restrictions=["invalid_region"],
                decision_timestamp=timestamp,
            )

        # ── Network check ─────────────────────────────────────────
        if not origin._is_network_available():
            return RoutingDecision(
                decision="MANUAL_REVIEW", matched_rule=None,
                direct_allowed=False, hub_transfer_required=True,
                applicable_corridor_id=None,
                reason_code="NETWORK_DISABLED",
                reason_text=f"Origin region {origin.code} is not network-available.",
                evaluated_day=pickup_day or "unknown",
                restrictions=["network_disabled"],
                decision_timestamp=timestamp,
            )
        if not dest._is_network_available():
            return RoutingDecision(
                decision="MANUAL_REVIEW", matched_rule=None,
                direct_allowed=False, hub_transfer_required=True,
                applicable_corridor_id=None,
                reason_code="NETWORK_DISABLED",
                reason_text=f"Destination region {dest.code} is not network-available.",
                evaluated_day=pickup_day or "unknown",
                restrictions=["network_disabled"],
                decision_timestamp=timestamp,
            )

        # ── Same-region: intra-region direct candidate ────────────
        if origin.id == dest.id:
            return RoutingDecision(
                decision="DIRECT_ALLOWED", matched_rule=None,
                direct_allowed=True, hub_transfer_required=False,
                applicable_corridor_id=None,
                reason_code="INTRA_REGION",
                reason_text=f"Same region ({origin.code}). Intra-region movement "
                           f"is eligible for direct local delivery on a scheduled "
                           f"service day.",
                evaluated_day=pickup_day or "unknown",
                restrictions=[],
                decision_timestamp=timestamp,
            )

        # ── Look up explicit rule ─────────────────────────────────
        day = pickup_day.lower() if pickup_day else None
        rules = Rule.search([
            ("active", "=", True),
            ("origin_region_id", "=", origin.id),
            ("destination_region_id", "=", dest.id),
        ])

        matched = None
        for rule in rules:
            # Check directionality
            if rule.direction == "outbound":
                # Outbound means Hub→Region; if origin is not a Hub, skip
                # For simplicity: "both" and "inbound" match any direction
                pass  # Direction handling expanded later

            # Check day
            if day and rule.allowed_service_days:
                if not rule.is_day_allowed(day):
                    continue

            matched = rule
            break

        # ── No rule → Hub transfer ────────────────────────────────
        if not matched:
            return RoutingDecision(
                decision="HUB_TRANSFER_REQUIRED", matched_rule=None,
                direct_allowed=False, hub_transfer_required=True,
                applicable_corridor_id=None,
                reason_code="NO_DIRECT_RULE",
                reason_text=f"No approved direct-delivery rule exists for "
                           f"{origin.code} → {dest.code}. "
                           f"Shipment must route through the Hub.",
                evaluated_day=pickup_day or "unknown",
                restrictions=["hub_transfer"],
                decision_timestamp=timestamp,
            )

        # ── Rule matched — validate constraints ───────────────────
        restrictions = []

        # Distance check
        if matched.max_direct_distance_km and matched.max_direct_distance_km > 0:
            if road_distance_km and road_distance_km > matched.max_direct_distance_km:
                return RoutingDecision(
                    decision="HUB_TRANSFER_REQUIRED", matched_rule=matched,
                    direct_allowed=False, hub_transfer_required=True,
                    applicable_corridor_id=matched.applicable_corridor_id,
                    reason_code="DISTANCE_EXCEEDED",
                    reason_text=f"Road distance ({road_distance_km:.0f} km) exceeds "
                               f"max direct distance ({matched.max_direct_distance_km:.0f} km) "
                               f"for {origin.code} → {dest.code}.",
                    evaluated_day=pickup_day or "unknown",
                    restrictions=["distance_exceeded"],
                    decision_timestamp=timestamp,
                )
            if road_distance_km:
                restrictions.append(f"distance={road_distance_km:.0f}km")

        # Time window check
        if pickup_time is not None and matched.latest_pickup_time:
            if pickup_time > matched.latest_pickup_time:
                return RoutingDecision(
                    decision="HUB_TRANSFER_REQUIRED", matched_rule=matched,
                    direct_allowed=False, hub_transfer_required=True,
                    applicable_corridor_id=matched.applicable_corridor_id,
                    reason_code="PICKUP_TIME_EXCEEDED",
                    reason_text=f"Pickup time ({pickup_time:.1f}h) exceeds latest "
                               f"direct pickup time ({matched.latest_pickup_time:.1f}h) "
                               f"for {origin.code} → {dest.code}.",
                    evaluated_day=pickup_day or "unknown",
                    restrictions=["pickup_time_exceeded"],
                    decision_timestamp=timestamp,
                )
            restrictions.append(f"pickup_time={pickup_time:.1f}h")

        if pickup_time is not None and matched.earliest_pickup_time:
            if pickup_time < matched.earliest_pickup_time:
                restrictions.append("pickup_before_earliest")

        # ── Direct allowed ────────────────────────────────────────
        if matched.direct_same_day_allowed:
            return RoutingDecision(
                decision="DIRECT_ALLOWED", matched_rule=matched,
                direct_allowed=True, hub_transfer_required=False,
                applicable_corridor_id=matched.applicable_corridor_id,
                reason_code="DIRECT_RULE_MATCH",
                reason_text=f"{origin.code} → {dest.code} is approved for "
                           f"direct same-day service on {pickup_day or 'eligible days'}.",
                evaluated_day=pickup_day or "unknown",
                restrictions=restrictions,
                decision_timestamp=timestamp,
            )

        # ── Hub transfer ──────────────────────────────────────────
        return RoutingDecision(
            decision="HUB_TRANSFER_REQUIRED", matched_rule=matched,
            direct_allowed=False, hub_transfer_required=True,
            applicable_corridor_id=matched.applicable_corridor_id,
            reason_code="RULE_REQUIRES_HUB",
            reason_text=f"Rule for {origin.code} → {dest.code} requires Hub transfer.",
            evaluated_day=pickup_day or "unknown",
            restrictions=restrictions,
            decision_timestamp=timestamp,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _resolve_region(self, region_ref):
        """Resolve a region ID int or record to a region record.
        Returns None if the region does not exist."""
        if region_ref is None:
            return None
        Region = self.env["logistics.region"]
        try:
            if isinstance(region_ref, int):
                rec = Region.browse(region_ref)
                return rec if rec.exists() else None
            if hasattr(region_ref, "_name") and region_ref._name == "logistics.region":
                return region_ref if region_ref.exists() else None
            rec = Region.browse(int(region_ref))
            return rec if rec.exists() else None
        except Exception:
            return None

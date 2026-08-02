"""Centralized Capacity Engine — THE single authority for pallet/weight capacity evaluation.

Used by Pricing, Availability, Booking, and Dispatch/Load Planning.
Evaluates BOTH pallet positions AND weight payload against actual fleet.vehicle specs.

Phase 8: Added leg-segment peak computation — for each corridor stop segment, sums
pallets/weight from all bookings whose pickup is before or at this stop AND
delivery is after this stop. Peak = max across all segments. Rejects bookings
exceeding 12 pallets; allows 13+ only with dispatcher override (pinwheel mode).
"""
import logging
from collections import defaultdict

_logger = logging.getLogger(__name__)


class CapacityResult:
    def __init__(self, eligible=False, vehicle=None, layout="straight",
                 pallet_count=0, shipment_weight_lbs=0.0,
                 straight_capacity=12, pinwheel_capacity=13, turned_capacity=14,
                 auto_booking_capacity=13, payload_capacity_lbs=11000.0,
                 manual_review=False, reason_code=None):
        self.eligible = eligible
        self.vehicle = vehicle
        self.layout = layout
        self.pallet_count = pallet_count
        self.shipment_weight_lbs = shipment_weight_lbs
        self.straight_capacity = straight_capacity
        self.pinwheel_capacity = pinwheel_capacity
        self.turned_capacity = turned_capacity
        self.auto_booking_capacity = auto_booking_capacity
        self.payload_capacity_lbs = payload_capacity_lbs
        self.manual_review = manual_review
        self.reason_code = reason_code

    def as_dict(self):
        return {
            "eligible": self.eligible,
            "vehicle_id": self.vehicle.id if self.vehicle else None,
            "vehicle_name": self.vehicle.name if self.vehicle else "",
            "layout": self.layout,
            "pallet_count": self.pallet_count,
            "shipment_weight_lbs": self.shipment_weight_lbs,
            "straight_capacity": self.straight_capacity,
            "pinwheel_capacity": self.pinwheel_capacity,
            "turned_capacity": self.turned_capacity,
            "auto_booking_capacity": self.auto_booking_capacity,
            "payload_capacity_lbs": self.payload_capacity_lbs,
            "manual_review": self.manual_review,
            "reason_code": self.reason_code,
        }


class CapacityEngine:
    """Evaluate whether a fleet vehicle can handle a given shipment."""

    def __init__(self, env):
        self.env = env(su=True)

    def evaluate(self, pallets, weight_lbs, equipment_profile=None,
                 vehicle=None, requires_reefer=False, requires_liftgate=False):
        """Returns CapacityResult. Use either equipment_profile or vehicle."""
        if vehicle is None and equipment_profile:
            vehicle = equipment_profile.fleet_vehicle_id

        if not vehicle:
            return CapacityResult(eligible=False, reason_code="no_vehicle_provided")

        if not vehicle.x_operational_logistics:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="equipment_not_operational")

        # Equipment checks
        if requires_reefer and not vehicle.x_reefer:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="reefer_required")
        if requires_liftgate and not vehicle.x_liftgate:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="liftgate_required")

        # Read actual pallet capacities from fleet.vehicle
        straight = int(vehicle.straight_pallet_capacity or 12)
        pinwheel = int(vehicle.pin_wheel_pallet_capacity or 13)
        turned = int(vehicle.turned_pallet_capacity or 14)
        payload = vehicle.x_max_payload_lbs or 11000.0

        # Auto-booking capacity: straight + pin-wheel (13 is the max for automated)
        auto_max = pinwheel  # 13 for PB38446

        # Determine layout and eligibility
        if pallets <= 0:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="invalid_pallet_count")

        if pallets <= straight:
            layout = "straight"
            eligible = True
            manual_review = False
            reason = None
        elif pallets == pinwheel:
            # V3 spec: 13 pallets requires dispatcher override (pinwheel mode)
            layout = "pin_wheel"
            eligible = True
            manual_review = True
            reason = "pinwheel_override_required"
        elif pallets >= turned:
            # V3 spec: 14+ pallets always rejected (exceeds truck physical capacity)
            return CapacityResult(
                eligible=False, vehicle=vehicle,
                pallet_count=pallets, shipment_weight_lbs=weight_lbs,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, auto_booking_capacity=pinwheel,
                payload_capacity_lbs=payload,
                reason_code="pallet_capacity_exceeded",
            )
        else:
            # Between pinwheel+1 and turned-1 → shouldn't happen with standard trucks
            return CapacityResult(
                eligible=False, vehicle=vehicle,
                pallet_count=pallets, shipment_weight_lbs=weight_lbs,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, auto_booking_capacity=pinwheel,
                payload_capacity_lbs=payload,
                manual_review=True, reason_code="manual_layout_review",
            )
        # Weight validation
        if weight_lbs > payload:
            return CapacityResult(
                eligible=False, vehicle=vehicle,
                pallet_count=pallets, shipment_weight_lbs=weight_lbs,
                layout=layout, straight_capacity=straight,
                pinwheel_capacity=pinwheel, turned_capacity=turned,
                auto_booking_capacity=auto_max, payload_capacity_lbs=payload,
                reason_code="payload_exceeded",
            )

        return CapacityResult(
            eligible=eligible, vehicle=vehicle, layout=layout,
            pallet_count=pallets, shipment_weight_lbs=weight_lbs,
            straight_capacity=straight, pinwheel_capacity=pinwheel,
            turned_capacity=turned, auto_booking_capacity=auto_max,
            payload_capacity_lbs=payload,
            manual_review=manual_review, reason_code=reason,
        )

    def find_compatible_vehicles(self, pallets, weight_lbs, requires_reefer=False,
                                  requires_liftgate=False):
        """Return list of eligible fleet.vehicle records."""
        Vehicle = self.env["fleet.vehicle"]
        candidates = Vehicle.search([
            ("active", "=", True),
            ("x_operational_logistics", "=", True),
        ])
        results = []
        for v in candidates:
            cap = self.evaluate(pallets, weight_lbs, vehicle=v,
                                requires_reefer=requires_reefer,
                                requires_liftgate=requires_liftgate)
            if cap.eligible and not cap.manual_review:
                results.append(cap)
        return results

    # ── Phase 8: Leg-Segment Peak Capacity ───────────────────────────

    def compute_departure_peak(self, departure):
        """Compute peak pallets and weight across all segments of a corridor departure.

        For each corridor stop (segment between consecutive stops), sums
        pallets/weight from all bookings whose:
        - pickup is at or before this stop's region, AND
        - delivery is after this stop's region

        Returns dict: {peak_pallets, peak_weight, segment_details: [...]}
        """
        if not departure or not departure.corridor_id:
            return {"peak_pallets": 0, "peak_weight": 0.0, "segment_details": []}

        corridor = departure.corridor_id
        stops = corridor.stop_ids.sorted("sequence")
        if len(stops) < 2:
            return {"peak_pallets": 0, "peak_weight": 0.0, "segment_details": []}

        # Get all confirmed bookings for this departure
        bookings = self.env["logistics.booking"].search([
            ("departure_id", "=", departure.id),
            ("state", "=", "confirmed"),
        ])

        # Build booking list with pickup/delivery region indices
        booking_segments = []
        for bk in bookings:
            pickup_region = bk.pickup_fsa_id.region_id
            delivery_region = bk.delivery_fsa_id.region_id
            # Find the stop indices for these regions on the corridor
            pickup_idx = None
            delivery_idx = None
            for i, stop in enumerate(stops):
                if stop.region_id == pickup_region:
                    pickup_idx = i
                if stop.region_id == delivery_region:
                    delivery_idx = i
            if pickup_idx is not None and delivery_idx is not None:
                booking_segments.append({
                    "booking": bk,
                    "pallets": bk.pallets,
                    "weight": bk.weight_lbs,
                    "pickup_idx": pickup_idx,
                    "delivery_idx": delivery_idx,
                })

        # Compute load on each segment (between consecutive stops)
        max_pallets = 0
        max_weight = 0.0
        segment_details = []

        for seg_start in range(len(stops) - 1):
            seg_pallets = 0
            seg_weight = 0.0
            for bs in booking_segments:
                # Booking is on this segment if: pickup at or before seg_start
                # AND delivery after seg_start
                if bs["pickup_idx"] <= seg_start and bs["delivery_idx"] > seg_start:
                    seg_pallets += bs["pallets"]
                    seg_weight += bs["weight"]

            segment_details.append({
                "from_stop": stops[seg_start].region_id.name if stops[seg_start].region_id else stops[seg_start].name,
                "to_stop": stops[seg_start + 1].region_id.name if stops[seg_start + 1].region_id else stops[seg_start + 1].name,
                "pallets": seg_pallets,
                "weight": seg_weight,
            })
            max_pallets = max(max_pallets, seg_pallets)
            max_weight = max(max_weight, seg_weight)

        return {
            "peak_pallets": max_pallets,
            "peak_weight": max_weight,
            "total_handled": sum(bs["pallets"] for bs in booking_segments),
            "segment_details": segment_details,
        }

    def can_accept_booking(self, departure, new_pallets, new_weight_lbs):
        """Check if a new booking can fit on this departure without exceeding capacity.

        Returns dict: {accepted: bool, reason: str, requires_override: bool,
                       current_peak: int, new_peak: int, max_capacity: int}
        """
        if not departure:
            return {"accepted": False, "reason": "No departure specified"}

        max_capacity = departure.max_capacity or 12
        current = self.compute_departure_peak(departure)
        current_peak = current["peak_pallets"]

        # For a quick check: add new pallets to current peak (conservative)
        # The actual peak would be computed with the booking added
        estimated_new_peak = current_peak + new_pallets

        if estimated_new_peak <= max_capacity:
            return {
                "accepted": True,
                "requires_override": False,
                "current_peak": current_peak,
                "new_peak": estimated_new_peak,
                "max_capacity": max_capacity,
            }
        elif estimated_new_peak == max_capacity + 1 and new_pallets <= 1:
            # Allow exactly 13 with dispatcher override (pinwheel mode)
            return {
                "accepted": False,
                "reason": "Capacity warning: 13 pallets requires dispatcher override (pinwheel)",
                "requires_override": True,
                "current_peak": current_peak,
                "new_peak": estimated_new_peak,
                "max_capacity": max_capacity,
            }
        else:
            return {
                "accepted": False,
                "reason": f"Capacity exceeded: {estimated_new_peak} pallets on a {max_capacity}-pallet truck",
                "requires_override": True,
                "current_peak": current_peak,
                "new_peak": estimated_new_peak,
                "max_capacity": max_capacity,
            }

"""Single physical-capacity authority for scheduled logistics operations.

Capacity belongs to an exact corridor departure / vehicle and is calculated
from the booking LEGS riding that departure.  A transferred shipment therefore
reserves every physical movement independently instead of relying on the
booking header's first departure.
"""
import logging

_logger = logging.getLogger(__name__)


class CapacityResult:
    def __init__(self, eligible=False, vehicle=None, layout="straight",
                 pallet_count=0, shipment_weight_lbs=0.0,
                 straight_capacity=0, pinwheel_capacity=0, turned_capacity=0,
                 auto_booking_capacity=0, payload_capacity_lbs=0.0,
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
    def __init__(self, env):
        self.env = env(su=True)

    @staticmethod
    def _vehicle_capacities(vehicle):
        if not vehicle:
            return 0, 0, 0, 0.0
        return (
            int(vehicle.straight_pallet_capacity or 0),
            int(vehicle.pin_wheel_pallet_capacity or 0),
            int(vehicle.turned_pallet_capacity or 0),
            float(vehicle.x_max_payload_lbs or 0.0),
        )

    @classmethod
    def vehicle_booking_capacity(cls, vehicle, allow_pinwheel_override=False):
        """One deterministic capacity value for booking/availability services."""
        straight, pinwheel, _turned, _payload = cls._vehicle_capacities(vehicle)
        if allow_pinwheel_override:
            return pinwheel or straight
        return straight

    def evaluate(self, pallets, weight_lbs, equipment_profile=None,
                 vehicle=None, requires_reefer=False, requires_liftgate=False,
                 allow_pinwheel_override=False):
        if vehicle is None and equipment_profile:
            vehicle = equipment_profile.fleet_vehicle_id
        if not vehicle:
            return CapacityResult(eligible=False, reason_code="no_vehicle_provided")
        if not vehicle.x_operational_logistics:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="equipment_not_operational")
        if requires_reefer and not vehicle.x_reefer:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="reefer_required")
        if requires_liftgate and not vehicle.x_liftgate:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="liftgate_required")

        straight, pinwheel, turned, payload = self._vehicle_capacities(vehicle)
        if not straight or not pinwheel or not payload:
            return CapacityResult(
                eligible=False, vehicle=vehicle,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, payload_capacity_lbs=payload,
                reason_code="vehicle_capacity_not_configured",
            )
        if pallets <= 0:
            return CapacityResult(eligible=False, vehicle=vehicle,
                                  reason_code="invalid_pallet_count")
        if weight_lbs > payload:
            return CapacityResult(
                eligible=False, vehicle=vehicle,
                pallet_count=pallets, shipment_weight_lbs=weight_lbs,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, auto_booking_capacity=straight,
                payload_capacity_lbs=payload,
                reason_code="payload_exceeded",
            )

        if pallets <= straight:
            return CapacityResult(
                eligible=True, vehicle=vehicle, layout="straight",
                pallet_count=pallets, shipment_weight_lbs=weight_lbs,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, auto_booking_capacity=straight,
                payload_capacity_lbs=payload,
            )
        if pallets <= pinwheel:
            return CapacityResult(
                eligible=bool(allow_pinwheel_override), vehicle=vehicle,
                layout="pin_wheel", pallet_count=pallets,
                shipment_weight_lbs=weight_lbs,
                straight_capacity=straight, pinwheel_capacity=pinwheel,
                turned_capacity=turned, auto_booking_capacity=straight,
                payload_capacity_lbs=payload,
                manual_review=not allow_pinwheel_override,
                reason_code=None if allow_pinwheel_override else "pinwheel_override_required",
            )
        return CapacityResult(
            eligible=False, vehicle=vehicle,
            pallet_count=pallets, shipment_weight_lbs=weight_lbs,
            straight_capacity=straight, pinwheel_capacity=pinwheel,
            turned_capacity=turned, auto_booking_capacity=straight,
            payload_capacity_lbs=payload,
            reason_code="pallet_capacity_exceeded",
        )

    def find_compatible_vehicles(self, pallets, weight_lbs, requires_reefer=False,
                                  requires_liftgate=False):
        candidates = self.env["fleet.vehicle"].search([
            ("active", "=", True),
            ("x_operational_logistics", "=", True),
        ])
        results = []
        for vehicle in candidates:
            cap = self.evaluate(
                pallets, weight_lbs, vehicle=vehicle,
                requires_reefer=requires_reefer,
                requires_liftgate=requires_liftgate,
            )
            if cap.eligible and not cap.manual_review:
                results.append(cap)
        return results

    def _leg_segment_indices(self, leg, corridor_stops):
        origin = leg.origin_region_id
        destination = leg.destination_region_id
        if not origin or not destination:
            return None, None
        origin_indices = [
            idx for idx, stop in enumerate(corridor_stops)
            if stop.region_id == origin and stop.pickup_allowed
        ]
        destination_indices = [
            idx for idx, stop in enumerate(corridor_stops)
            if stop.region_id == destination and stop.delivery_allowed
        ]
        candidates = [
            (start, end) for start in origin_indices for end in destination_indices
            if end > start
        ]
        if not candidates:
            return None, None
        return min(candidates, key=lambda pair: (pair[1] - pair[0], pair[0]))

    def compute_departure_peak(self, departure):
        """Peak onboard freight for one exact departure using booking legs.

        Canonical path: ``logistics.booking.leg.departure_id``.  A narrow
        legacy fallback is retained for historical single-leg bookings that
        predate booking-leg creation.
        """
        if not departure or not departure.corridor_id:
            return {"peak_pallets": 0, "peak_weight": 0.0,
                    "total_handled": 0, "segment_details": []}

        corridor = departure.corridor_id
        stops = corridor.stop_ids.filtered("active").sorted("sequence")
        if len(stops) < 2:
            return {"peak_pallets": 0, "peak_weight": 0.0,
                    "total_handled": 0, "segment_details": []}

        Leg = self.env["logistics.booking.leg"]
        legs = Leg.search([
            ("departure_id", "=", departure.id),
            ("booking_id.state", "=", "confirmed"),
            ("reservation_state", "not in", ("released", "cancelled")),
        ])

        movements = []
        booking_ids_seen = set()
        for leg in legs:
            start, end = self._leg_segment_indices(leg, stops)
            if start is None:
                _logger.warning(
                    "Capacity: booking leg %s does not map to ordered regions on departure %s",
                    leg.id, departure.id,
                )
                continue
            booking = leg.booking_id
            physical_pallets = booking.physical_pallets or leg.pallets or booking.pallets
            physical_weight = booking.weight_lbs or leg.weight_lbs
            movements.append({
                "booking": booking,
                "pallets": physical_pallets,
                "weight": physical_weight,
                "pickup_idx": start,
                "delivery_idx": end,
            })
            booking_ids_seen.add(booking.id)

        # Historical fallback only when no canonical leg exists for a booking.
        legacy = self.env["logistics.booking"].search([
            ("departure_id", "=", departure.id),
            ("state", "=", "confirmed"),
            ("id", "not in", list(booking_ids_seen) or [0]),
        ])
        for booking in legacy:
            pickup_region = booking.pickup_fsa_id.region_id
            delivery_region = booking.delivery_fsa_id.region_id
            pseudo = self.env["logistics.booking.leg"].new({
                "origin_region_id": pickup_region.id,
                "destination_region_id": delivery_region.id,
            })
            start, end = self._leg_segment_indices(pseudo, stops)
            if start is None:
                continue
            movements.append({
                "booking": booking,
                "pallets": booking.physical_pallets or booking.pallets,
                "weight": booking.weight_lbs,
                "pickup_idx": start,
                "delivery_idx": end,
            })

        max_pallets = 0
        max_weight = 0.0
        segment_details = []
        for seg_start in range(len(stops) - 1):
            onboard = [
                movement for movement in movements
                if movement["pickup_idx"] <= seg_start < movement["delivery_idx"]
            ]
            pallets = sum(movement["pallets"] for movement in onboard)
            weight = sum(movement["weight"] for movement in onboard)
            segment_details.append({
                "from_stop": stops[seg_start].region_id.name if stops[seg_start].region_id else stops[seg_start].name,
                "to_stop": stops[seg_start + 1].region_id.name if stops[seg_start + 1].region_id else stops[seg_start + 1].name,
                "pallets": pallets,
                "weight": weight,
                "booking_ids": [movement["booking"].id for movement in onboard],
            })
            max_pallets = max(max_pallets, pallets)
            max_weight = max(max_weight, weight)

        return {
            "peak_pallets": max_pallets,
            "peak_weight": max_weight,
            "total_handled": sum(movement["pallets"] for movement in movements),
            "segment_details": segment_details,
        }

    def can_accept_booking(self, departure, new_pallets, new_weight_lbs,
                           allow_pinwheel_override=False):
        if not departure or not departure.vehicle_id:
            return {"accepted": False, "reason": "No vehicle assigned to departure"}
        vehicle = departure.vehicle_id
        current = self.compute_departure_peak(departure)
        capacity = self.vehicle_booking_capacity(vehicle, allow_pinwheel_override)
        _straight, _pinwheel, _turned, payload = self._vehicle_capacities(vehicle)
        projected_pallets = current["peak_pallets"] + new_pallets
        projected_weight = current["peak_weight"] + new_weight_lbs

        if projected_weight > payload:
            return {
                "accepted": False, "reason": "Payload capacity exceeded",
                "requires_override": False, "current_peak": current["peak_pallets"],
                "new_peak": projected_pallets, "max_capacity": capacity,
            }
        if projected_pallets <= capacity:
            return {
                "accepted": True, "requires_override": False,
                "current_peak": current["peak_pallets"],
                "new_peak": projected_pallets, "max_capacity": capacity,
            }
        if not allow_pinwheel_override and projected_pallets <= (vehicle.pin_wheel_pallet_capacity or 0):
            return {
                "accepted": False,
                "reason": "Pin-wheel layout requires dispatcher override",
                "requires_override": True,
                "current_peak": current["peak_pallets"],
                "new_peak": projected_pallets,
                "max_capacity": capacity,
            }
        return {
            "accepted": False,
            "reason": f"Capacity exceeded: {projected_pallets} pallets on a {capacity}-pallet booking layout",
            "requires_override": False,
            "current_peak": current["peak_pallets"],
            "new_peak": projected_pallets,
            "max_capacity": capacity,
        }

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

    @staticmethod
    def _is_exclusive_service(booking):
        """A Full Truckload / Dedicated / Exclusive service booking owns the
        ENTIRE vehicle on its departure: nothing may share the truck with it.

        The discriminator is the booking's SERVICE TYPE (load_type /
        shipment_type = 'ftl') — NEVER the pricing mode. Corridors with
        'enable_ftl / ftl_threshold_pallets / auto_price' price large LTL
        loads with FTL math at the threshold (corridor 9: 10 pallets) but
        those remain LTL service bookings: they reserve their positions
        only and never auto-reserve the truck."""
        return bool(booking) and (
            booking.load_type == "ftl" or booking.shipment_type == "ftl")

    def compute_departure_peak(self, departure):
        """Compute peak pallets and weight across all segments of a corridor departure.

        For each corridor stop (segment between consecutive stops), sums
        physical positions/weight from all CONFIRMED bookings whose:
        - pickup is at or before this stop's region, AND
        - delivery is after this stop's region

        Segment occupancy is movement-aware: movement_v1 (milk-run)
        bookings contribute ONE physical position per pallet movement,
        each spanning pickup-stop region → delivery-stop region (a movement
        picked up at a later corridor stop only occupies segments after
        it — never the whole first-pickup → last-delivery span). Legacy
        bookings keep their FSA-anchored span, counted in physical
        positions.

        Returns dict: {peak_pallets, peak_weight, segment_details,
        reserved_ltl_positions, exclusive_vehicle_reserved,
        exclusive_booking_ids, ...} — peak_pallets is the true onboard
        peak; reserved_ltl_positions is the LTL-only sellable occupancy
        (an exclusive FTL booking zeroes the remaining SELLABLE capacity
        regardless of its own position count).
        """
        empty = {
            "peak_pallets": 0, "peak_weight": 0.0, "total_handled": 0,
            "segment_details": [],
            "reserved_ltl_positions": 0,
            "exclusive_vehicle_reserved": False,
            "exclusive_booking_ids": [],
            "capacity_state": "available",
            "integrity_conflicts": [],
        }
        if not departure or not departure.corridor_id:
            return dict(empty)

        corridor = departure.corridor_id
        stops = corridor.stop_ids.sorted("sequence")
        if len(stops) < 2:
            return dict(empty)

        # All confirmed bookings on this departure.
        bookings = self.env["logistics.booking"].search([
            ("departure_id", "=", departure.id),
            ("state", "=", "confirmed"),
        ])
        from .departure_span_validator import DepartureSpanValidator
        span_validator = DepartureSpanValidator(self.env)
        integrity_conflicts = []
        for booking in bookings:
            booking_legs = booking.leg_ids.filtered(
                lambda leg: leg.departure_id.id == departure.id
            )
            for leg in booking_legs:
                origin_region, destination_region = self._leg_regions(leg)
                validation = span_validator.validate(
                    departure, origin_region, destination_region,
                )
                if not validation["valid"]:
                    conflict = {
                        "departure_id": departure.id,
                        "booking_id": booking.id,
                        "leg_id": leg.id,
                        "origin_region": origin_region.code if origin_region else None,
                        "destination_region": destination_region.code if destination_region else None,
                        "reason": validation["reason"],
                    }
                    integrity_conflicts.append(conflict)
                    _logger.error("Departure capacity integrity conflict: %s", conflict)

        if integrity_conflicts:
            return {
                **empty,
                "capacity_state": "integrity_conflict",
                "integrity_conflicts": integrity_conflicts,
            }
        exclusive = [b for b in bookings if self._is_exclusive_service(b)]
        exclusive_ids = [b.id for b in exclusive]

        # Build per-booking segment occupancy (movement-aware).
        booking_segments = []
        for bk in bookings:
            segments = self._booking_segments(stops, bk)
            booking_segments.extend(segments)

        # Compute load on each segment (between consecutive stops)
        max_pallets = 0
        max_weight = 0.0
        max_ltl = 0
        segment_details = []

        for seg_start in range(len(stops) - 1):
            seg_pallets = 0
            seg_weight = 0.0
            seg_ltl = 0
            for bs in booking_segments:
                # Booking is on this segment if: pickup at or before seg_start
                # AND delivery after seg_start
                if bs["pickup_idx"] <= seg_start and bs["delivery_idx"] > seg_start:
                    seg_pallets += bs["pallets"]
                    seg_weight += bs["weight"]
                    if not bs["exclusive"]:
                        seg_ltl += bs["pallets"]

            segment_details.append({
                "from_stop": stops[seg_start].region_id.name if stops[seg_start].region_id else stops[seg_start].name,
                "to_stop": stops[seg_start + 1].region_id.name if stops[seg_start + 1].region_id else stops[seg_start + 1].name,
                "pallets": seg_pallets,
                "weight": seg_weight,
                "ltl_positions": seg_ltl,
            })
            max_pallets = max(max_pallets, seg_pallets)
            max_weight = max(max_weight, seg_weight)
            max_ltl = max(max_ltl, seg_ltl)

        # Phase 7 — Weekly Capacity Planner reservations (spec §42, §47):
        # planned occurrences reserve physical positions on the departure,
        # exactly like confirmed bookings. An anchored card counts only on
        # its chosen departure; an unanchored card counts on any scheduled
        # departure for its truck on the plan date. FTL cards hold the whole
        # vehicle like an FTL booking. The reserve is flat (whole route) —
        # conservative — and becomes a real segment-aware booking at
        # generation time. This flows into VehicleCapacityService
        # reserved_pallets / evaluate / for_pickup_date / check_and_reserve,
        # so the portal can never overbook a planned position.
        Reservation = self.env["logistics.weekly.plan.reservation"]
        reservations = Reservation.search([
            ("state", "=", "planned"),
            "|",
            ("corridor_departure_id", "=", departure.id),
            ("corridor_departure_id", "=", False),
        ])
        exclusive_reservation_ids = []
        if reservations:
            anchored = reservations.filtered(
                lambda r: r.corridor_departure_id.id == departure.id)
            unanchored = reservations.filtered(
                lambda r: not r.corridor_departure_id)
            day_match = unanchored.filtered(
                lambda r: (r.plan_date == departure.departure_date
                           and r.vehicle_id.id == departure.vehicle_id.id))
            counted = anchored | day_match
            if counted:
                ltl_cards = counted.filtered(lambda r: r.load_type != "ftl")
                ftl_cards = counted.filtered(lambda r: r.load_type == "ftl")
                if ltl_cards:
                    max_pallets += sum(ltl_cards.mapped("pallets"))
                    max_weight += sum(ltl_cards.mapped("weight_lbs"))
                    max_ltl += sum(ltl_cards.mapped("pallets"))
                if ftl_cards:
                    exclusive_vehicle_reserved = True
                    exclusive_reservation_ids = ftl_cards.ids

        return {
            "peak_pallets": max_pallets,
            "peak_weight": max_weight,
            "total_handled": sum(bs["pallets"] for bs in booking_segments),
            "segment_details": segment_details,
            "reserved_ltl_positions": max_ltl,
            "exclusive_vehicle_reserved": bool(
                exclusive_ids or exclusive_reservation_ids),
            "exclusive_booking_ids": exclusive_ids,
            "exclusive_reservation_ids": exclusive_reservation_ids,
            "capacity_state": "available",
            "integrity_conflicts": [],
        }

    def _leg_regions(self, leg):
        origin = leg.origin_region_id
        destination = leg.destination_region_id
        if origin and destination:
            return origin, destination
        for snapshot in (leg.booking_id.route_snapshot or {}).get("legs") or []:
            if snapshot.get("departure_id") != leg.departure_id.id:
                continue
            origin = self._canonical_region(
                snapshot.get("origin_region_id") or snapshot.get("origin_region_code")
                or snapshot.get("origin_region")
            )
            destination = self._canonical_region(
                snapshot.get("dest_region_id") or snapshot.get("dest_region_code")
                or snapshot.get("dest_region")
            )
            break
        # Manual/negotiated legs carry no region fields and no route
        # snapshot; fall back to the booking's own FSA-anchored regions so
        # span validation is meaningful (and the booking counts) instead of
        # raising a spurious ROUTE_DEPARTURE_MISMATCH for None regions.
        if not origin or not destination:
            booking = leg.booking_id
            if booking:
                if not origin and booking.pickup_fsa_id and booking.pickup_fsa_id.region_id:
                    origin = self._canonical_region(booking.pickup_fsa_id.region_id)
                if not destination and booking.delivery_fsa_id and booking.delivery_fsa_id.region_id:
                    destination = self._canonical_region(booking.delivery_fsa_id.region_id)
        return origin, destination

    def _canonical_region(self, value):
        if not value:
            return False
        from .region_resolver import RegionResolver
        # Region records pass through the resolver too: legacy lane regions
        # (1-20) must be bridged to the official set (142+) that corridor
        # stops and span validation are keyed on.
        return RegionResolver(self.env).canonical_region(value)

    def _booking_segments(self, corridor_stops, booking):
        """Segment occupancy of one confirmed booking on the corridor.

        movement_v1: one segment span PER PALlet MOVEMENT (pickup stop
        region → delivery stop region) — the milk-run segment capacity:
        a movement picked up at a later corridor stop occupies only the
        segments after it. A shared pallet (multiple delivery stops) rides
        to its DEEPEST delivery index.
        Legacy: the booking's FSA-anchored span (pickup region → delivery
        region), counted in physical positions.
        Returns [{pallets, weight, pickup_idx, delivery_idx, exclusive}].
        """
        result = []
        if booking.route_model_version == "movement_v1":
            from .region_resolver import RegionResolver
            resolver = RegionResolver(self.env)
            stop_by_key = {s.stop_key: s for s in booking.stop_ids}
            movements = self.env["logistics.booking"]._extract_pallet_movements_from_snapshot(
                booking.price_snapshot)
            resolved = {}
            for mv in movements:
                pu = stop_by_key.get(mv.get("pickup_stop_key") or "")
                keys = mv.get("delivery_stop_keys") or (
                    [mv["delivery_stop_key"]] if mv.get("delivery_stop_key") else [])
                dests = [stop_by_key.get(k) for k in keys]
                dests = [d for d in dests if d]
                if not pu or not dests:
                    continue
                idxs = []
                for stop in [pu] + dests:
                    key = ("stop", stop.id)
                    if key not in resolved:
                        idx = self._region_stop_index(resolver, corridor_stops, stop)
                        resolved[key] = idx
                    idxs.append(resolved[key])
                pu_idx = idxs[0]
                de_idx = max(idxs[1:])
                if pu_idx is not None and de_idx is not None:
                    result.append({
                        "pallets": 1,  # one physical position per movement
                        "weight": float(mv.get("weight_lbs") or booking.weight_lbs or 0.0),
                        "pickup_idx": pu_idx, "delivery_idx": de_idx,
                        "exclusive": self._is_exclusive_service(booking),
                    })
            return result

        # Legacy anchor: the booking's own FSA regions on this corridor,
        # canonicalized (legacy lane regions 1-20 → official 142+) so the
        # anchor matches corridor stops keyed to the operational region set.
        # When the FSA span still matches nothing, fall back to the frozen
        # route snapshot's operational region codes, then to the booking
        # stops' coordinates (polygon resolution) — a confirmed booking must
        # never be silently uncounted on its own departure.
        pickup_region = self._canonical_region(
            booking.pickup_fsa_id.region_id) if booking.pickup_fsa_id else False
        delivery_region = self._canonical_region(
            booking.delivery_fsa_id.region_id) if booking.delivery_fsa_id else False
        pickup_idx = None
        delivery_idx = None
        for i, stop in enumerate(corridor_stops):
            if stop.region_id == pickup_region:
                pickup_idx = i
            if stop.region_id == delivery_region:
                delivery_idx = i
        if pickup_idx is None or delivery_idx is None:
            idx_by_code = {
                stop.region_id.code: i
                for i, stop in enumerate(corridor_stops)
                if stop.region_id
            }
            for leg in (booking.route_snapshot or {}).get("legs") or []:
                if pickup_idx is None and leg.get("origin_region_code") in idx_by_code:
                    pickup_idx = idx_by_code[leg["origin_region_code"]]
                if delivery_idx is None and leg.get("dest_region_code") in idx_by_code:
                    delivery_idx = idx_by_code[leg["dest_region_code"]]
        if pickup_idx is None or delivery_idx is None:
            from .region_resolver import RegionResolver
            resolver = RegionResolver(self.env)
            pu_stop = booking.stop_ids.filtered(
                lambda s: s.stop_type == "pickup")[:1]
            de_stop = booking.stop_ids.filtered(
                lambda s: s.stop_type == "delivery")[:1]
            if pu_stop and de_stop:
                pu_idx = self._region_stop_index(resolver, corridor_stops, pu_stop)
                de_idx = self._region_stop_index(resolver, corridor_stops, de_stop)
                if pu_idx is not None:
                    pickup_idx = pu_idx
                if de_idx is not None:
                    delivery_idx = de_idx
        if pickup_idx is not None and delivery_idx is not None:
            result.append({
                "pallets": int(booking.physical_pallets or booking.pallets or 1),
                "weight": booking.weight_lbs or 0.0,
                "pickup_idx": pickup_idx, "delivery_idx": delivery_idx,
                "exclusive": self._is_exclusive_service(booking),
            })
        return result

    @staticmethod
    def _region_stop_index(resolver, corridor_stops, stop):
        """Index of the corridor stop whose region contains the booking
        stop's pin (resolved once per stop); None when unresolvable."""
        if not (stop.latitude and stop.longitude):
            return None
        region = resolver.resolve(
            stop.latitude, stop.longitude,
            country=stop.country_id.id if stop.country_id else None,
        ).matched_region
        if not region:
            return None
        for i, cstop in enumerate(corridor_stops):
            if cstop.region_id == region:
                return i
        return None

    def can_accept_booking(self, departure, new_pallets, new_weight_lbs):
        """Check if a new booking can fit on this departure without exceeding capacity.

        Returns dict: {accepted: bool, reason: str, requires_override: bool,
                       current_peak: int, new_peak: int, max_capacity: int}
        """
        if not departure:
            return {"accepted": False, "reason": "No departure specified"}

        max_capacity = departure.max_capacity or 12
        current = self.compute_departure_peak(departure)
        if current.get("capacity_state") == "integrity_conflict":
            return {
                "accepted": False,
                "reason": "capacity_integrity_conflict",
                "requires_override": False,
                "capacity_state": "integrity_conflict",
                "current_peak": 0,
                "new_peak": 0,
                "max_capacity": max_capacity,
            }
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

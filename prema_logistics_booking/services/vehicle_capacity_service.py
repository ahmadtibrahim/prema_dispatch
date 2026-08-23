"""VehicleCapacityService — THE canonical pallet-capacity authority.

Every consumer (booking portal, pricing/departure resolution, booking
confirmation, dispatch board, load planning, driver app) must obtain
capacity and layout answers from here. Nothing is hardcoded: capacities
come from the assigned vehicle's active pallet layouts (or the legacy
straight/pin-wheel/turned fields when a vehicle has no layout rows yet),
and reserved positions come from committed bookings on the departure.

Layout selection: prefer the vehicle's default active layout when it fits;
otherwise the smallest-capacity active layout that fits; no layout fits →
capacity invalid.
"""
from odoo import _


class VehicleCapacityService:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Layout sources ──────────────────────────────────────────────

    def get_layouts(self, vehicle):
        """Active layout modes for a vehicle, sorted by (sequence,
        max_pallets ascending). Falls back to the legacy per-vehicle
        capacity fields when no layout rows exist yet."""
        if not vehicle:
            return []
        Layout = self.env["fleet.vehicle.pallet.layout"]
        rows = Layout.search([("vehicle_id", "=", vehicle.id), ("active", "=", True)],
                             order="sequence, max_pallets, id")
        if rows:
            return [{
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "layout_type": row.layout_type,
                "max_pallets": row.max_pallets,
                "is_default": row.is_default,
                "sequence": row.sequence,
            } for row in rows]
        # Legacy fallback: vehicle fields straight/pin-wheel/turned.
        layouts = []
        if (vehicle.straight_pallet_capacity or 0) > 0:
            layouts.append({
                "id": False, "code": "standard", "name": "Standard",
                "layout_type": "standard",
                "max_pallets": vehicle.straight_pallet_capacity or 0,
                "is_default": True, "sequence": 10,
            })
        if (vehicle.pin_wheel_pallet_capacity or 0) > 0:
            layouts.append({
                "id": False, "code": "pinwheel", "name": "Pinwheel",
                "layout_type": "pinwheel",
                "max_pallets": vehicle.pin_wheel_pallet_capacity or 0,
                "is_default": False, "sequence": 20,
            })
        return sorted(layouts, key=lambda layout: (layout["sequence"], layout["max_pallets"]))

    def default_capacity(self, vehicle):
        layouts = self.get_layouts(vehicle)
        default = next((l for l in layouts if l["is_default"]), None)
        if default:
            return default["max_pallets"]
        return max((l["max_pallets"] for l in layouts), default=0)

    def maximum_capacity(self, vehicle):
        return max((l["max_pallets"] for l in self.get_layouts(vehicle)), default=0)

    def select_layout(self, vehicle, required_pallets, preferred_layout_id=False):
        """Return the layout dict that must carry `required_pallets`.

        Selection order (conceptual Part C1):
        1. valid layouts = layouts whose max_pallets >= required
        2. no valid layout → (False, None)
        3. explicit dispatcher override (preferred_layout_id) when valid
        4. the default active layout when it fits
        5. otherwise the smallest-capacity valid layout
        """
        layouts = self.get_layouts(vehicle)
        valid = [l for l in layouts if l["max_pallets"] >= required_pallets]
        if not valid:
            return False, None
        if preferred_layout_id:
            preferred = next((l for l in valid if l["id"] == preferred_layout_id), None)
            if preferred:
                return True, preferred
        default = next((l for l in valid if l["is_default"]), None)
        if default:
            return True, default
        return True, valid[0]

    # ── Reservations ────────────────────────────────────────────────

    def reserved_pallets(self, departure):
        """Physical pallet positions already reserved on the departure
        (cancelled/draft bookings never count). Delegates to the canonical
        CapacityEngine segment-peak computation — the true onboard peak
        (LTL positions + any exclusive FTL booking's positions)."""
        if not departure:
            return 0
        from .capacity_engine import CapacityEngine
        return CapacityEngine(self.env).compute_departure_peak(departure)["peak_pallets"]

    # ── Canonical evaluation ────────────────────────────────────────

    def evaluate(self, vehicle, departure=None, proposed_pallets=0):
        """Full capacity answer for a vehicle (+ optional departure).

        FTL / Dedicated / Exclusive service bookings reserve the ENTIRE
        vehicle: when one is confirmed on the departure, remaining
        SELLABLE capacity is 0 regardless of its own position count.
        LTL bookings (even threshold-priced FTL loads — pricing mode is
        NOT service type) reserve their physical positions only.
        """
        peak = {}
        if departure:
            from .capacity_engine import CapacityEngine
            peak = CapacityEngine(self.env).compute_departure_peak(departure)
        exclusive = bool(peak.get("exclusive_vehicle_reserved"))
        reserved = peak.get("peak_pallets", 0)
        reserved_ltl = peak.get("reserved_ltl_positions", 0)
        maximum = self.maximum_capacity(vehicle)
        # Sellable = what a NEW LTL booking may reserve: when the truck is
        # exclusively held, nothing is sellable at all.
        remaining_sellable = 0 if exclusive else max(maximum - reserved_ltl, 0)

        result = {
            "vehicle": vehicle,
            "layouts": self.get_layouts(vehicle),
            "default_capacity": self.default_capacity(vehicle),
            "maximum_capacity": maximum,
            "reserved_pallets": reserved,
            "reserved_ltl_positions": reserved_ltl,
            "exclusive_vehicle_reserved": exclusive,
            "exclusive_booking_ids": peak.get("exclusive_booking_ids", []),
            "exclusive_reservation_ids": peak.get(
                "exclusive_reservation_ids", []),
            "remaining_sellable_capacity": remaining_sellable,
            "proposed_pallets": proposed_pallets,
            "proposed_total": 0,
            "selected_layout": None,
            "remaining_pallets": remaining_sellable,
            "capacity_valid": True,
            "reason": None,
        }
        result["proposed_total"] = reserved + proposed_pallets
        valid, layout = self.select_layout(vehicle, result["proposed_total"])
        result["selected_layout"] = layout
        if exclusive and proposed_pallets:
            result["capacity_valid"] = False
            result["reason"] = _(
                "This departure's truck is exclusively reserved for a "
                "Full Truckload / dedicated shipment.")
        elif not valid:
            # Non-disclosing: never reveal the remaining pallet count —
            # exact capacity is internal.
            result["capacity_valid"] = False
            result["reason"] = _(
                "This pallet quantity is not available on the selected "
                "departure. Reduce the quantity or choose another "
                "pickup date.",
            )
        return result

    @classmethod
    def for_pickup_date(cls, env, region, date):
        """Best-effort capacity answer for the scheduled departure serving
        `region` on `date` (used by the portal pallet-input limit and the
        Get Price pre-check). Returns a plain dict; `available` is False
        when no suitable departure/truck exists."""
        service = cls(env)
        empty = {
            "available": False,
            "departure_id": False,
            "max_pallets": 0,
            "reserved_pallets": 0,
            "remaining_pallets": 0,
            "layout_code": "",
            "layout_name": "",
        }
        if not region or not date:
            return empty
        stop_ids = env["logistics.corridor.stop"].sudo().search([
            ("region_id", "=", region.id),
            ("pickup_allowed", "=", True),
            ("active", "=", True),
        ]).mapped("corridor_id").ids
        if not stop_ids:
            return empty
        departure = env["logistics.corridor.departure"].sudo().search([
            ("corridor_id", "in", stop_ids),
            ("departure_date", "=", date),
            ("status", "=", "scheduled"),
            ("active", "=", True),
            ("vehicle_id", "!=", False),
        ], order="id", limit=1)
        if not departure or not departure.vehicle_id:
            return empty
        result = service.evaluate(departure.vehicle_id, departure, 0)
        layout = result["selected_layout"] or {}
        corridor = departure.corridor_id
        return {
            "available": True,
            "departure_id": departure.id,
            "max_pallets": result["maximum_capacity"],
            "reserved_pallets": result["reserved_pallets"],
            "remaining_pallets": result["remaining_pallets"],
            "reserved_ltl_positions": result["reserved_ltl_positions"],
            "exclusive_vehicle_reserved": result["exclusive_vehicle_reserved"],
            "remaining_sellable_capacity": result["remaining_sellable_capacity"],
            "layout_code": layout.get("code", ""),
            "layout_name": layout.get("name", ""),
            # Per-pallet default weight from the selected corridor's own
            # configuration — the portal weight auto-calc source.
            "per_pallet_weight": corridor.included_weight_per_pallet or 0.0 if corridor else 0.0,
        }

    def check_and_reserve(self, departure, proposed_pallets, proposed_weight_lbs=0.0,
                          service_type="ltl"):
        """Authoritative, concurrency-safe capacity validation.

        Locks the departure row FOR UPDATE inside the caller's transaction
        and recomputes reserved positions from committed bookings, so two
        simultaneous confirmations can never both pass.

        service_type: 'ltl' reserves physical positions only. 'ftl'
        (Full Truckload / Dedicated / Exclusive) requires the ENTIRE
        vehicle: the departure must be completely free, and once such a
        booking is confirmed nothing else may join. A corridor's FTL
        PRICING threshold (enable_ftl + ftl_threshold_pallets +
        auto_price) never flips service_type — threshold-priced LTL loads
        reserve their positions like any LTL booking.

        Returns the evaluation dict; `capacity_valid` is False with a
        user-facing reason when the booking cannot fit.
        """
        if not departure or not departure.vehicle_id:
            return {
                "capacity_valid": False,
                "reason": _("No truck is assigned to the selected departure."),
            }
        # Serialize concurrent confirmations on this departure.
        self.env.cr.execute(
            "SELECT id FROM logistics_corridor_departure WHERE id = %s FOR UPDATE",
            [departure.id],
        )
        departure.invalidate_recordset()
        result = self.evaluate(departure.vehicle_id, departure, proposed_pallets)
        if service_type == "ftl" and not result["capacity_valid"]:
            return result
        # Exclusivity: FTL needs the truck EMPTY; LTL can never join an
        # exclusively-held truck.
        from .capacity_engine import CapacityEngine
        peak = CapacityEngine(self.env).compute_departure_peak(departure)
        if service_type == "ftl":
            if peak["peak_pallets"] or peak["exclusive_vehicle_reserved"]:
                result["capacity_valid"] = False
                result["reason"] = _(
                    "Full Truckload / dedicated moves require the ENTIRE "
                    "vehicle — this departure already has bookings on it. "
                    "Choose a free departure.")
                return result
        elif peak["exclusive_vehicle_reserved"]:
            result["capacity_valid"] = False
            result["reason"] = _(
                "This departure's truck is exclusively reserved for a "
                "Full Truckload / dedicated shipment.")
            return result
        if not result["capacity_valid"]:
            return result
        # Weight is a separate constraint on the vehicle payload.
        payload = departure.vehicle_id.x_max_payload_lbs or 0.0
        if payload and proposed_weight_lbs:
            current = 0.0
            try:
                current = peak["peak_weight"]
            except Exception:
                current = 0.0
            if current + proposed_weight_lbs > payload:
                result["capacity_valid"] = False
                result["reason"] = _(
                    "This shipment exceeds the truck's remaining payload "
                    "capacity. Reduce the weight or choose another pickup "
                    "date.",
                )
        return result

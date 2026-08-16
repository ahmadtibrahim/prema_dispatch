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
        CapacityEngine segment-peak computation."""
        if not departure:
            return 0
        from .capacity_engine import CapacityEngine
        return CapacityEngine(self.env).compute_departure_peak(departure)["peak_pallets"]

    # ── Canonical evaluation ────────────────────────────────────────

    def evaluate(self, vehicle, departure=None, proposed_pallets=0):
        """Full capacity answer for a vehicle (+ optional departure)."""
        result = {
            "vehicle": vehicle,
            "layouts": self.get_layouts(vehicle),
            "default_capacity": self.default_capacity(vehicle),
            "maximum_capacity": self.maximum_capacity(vehicle),
            "reserved_pallets": self.reserved_pallets(departure) if departure else 0,
            "proposed_pallets": proposed_pallets,
            "proposed_total": 0,
            "selected_layout": None,
            "remaining_pallets": 0,
            "capacity_valid": True,
            "reason": None,
        }
        result["proposed_total"] = result["reserved_pallets"] + proposed_pallets
        result["remaining_pallets"] = max(
            result["maximum_capacity"] - result["reserved_pallets"], 0,
        )
        valid, layout = self.select_layout(vehicle, result["proposed_total"])
        result["selected_layout"] = layout
        if not valid:
            result["capacity_valid"] = False
            result["reason"] = _(
                "Only %(remaining)s pallet position(s) remain on the selected "
                "departure. Please reduce the pallet quantity or choose "
                "another departure.",
                remaining=result["remaining_pallets"],
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
            "layout_code": layout.get("code", ""),
            "layout_name": layout.get("name", ""),
            # Per-pallet default weight from the selected corridor's own
            # configuration — the portal weight auto-calc source.
            "per_pallet_weight": corridor.included_weight_per_pallet or 0.0 if corridor else 0.0,
        }

    def check_and_reserve(self, departure, proposed_pallets, proposed_weight_lbs=0.0):
        """Authoritative, concurrency-safe capacity validation.

        Locks the departure row FOR UPDATE inside the caller's transaction
        and recomputes reserved positions from committed bookings, so two
        simultaneous confirmations can never both pass.

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
        if not result["capacity_valid"]:
            return result
        # Weight is a separate constraint on the vehicle payload.
        payload = departure.vehicle_id.x_max_payload_lbs or 0.0
        if payload and proposed_weight_lbs:
            current = 0.0
            try:
                from .capacity_engine import CapacityEngine
                current = CapacityEngine(self.env).compute_departure_peak(
                    departure)["peak_weight"]
            except Exception:
                current = 0.0
            if current + proposed_weight_lbs > payload:
                result["capacity_valid"] = False
                result["reason"] = _(
                    "This shipment exceeds the truck's remaining payload "
                    "capacity (%(payload)s lb).",
                    payload=payload,
                )
        return result

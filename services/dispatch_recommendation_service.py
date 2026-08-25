"""
Deterministic pallet-position recommendation engine for the Driver/Dispatch
Load Plan.

The recommendation is advisory only.  It follows the same practical loading
rule used by multi-stop freight operations: freight for the earliest delivery
should be easiest to reach from the rear door, while later-stop freight can be
loaded deeper into the body.  The service also tries to avoid unnecessary
moves, balances weight left/right where possible, and leaves final acceptance
to the driver/dispatcher.
"""

from collections import defaultdict


class DispatchRecommendationService:
    def __init__(self, env):
        self.env = env

    @staticmethod
    def _display_position_code(code):
        """Driver-friendly label without changing the stored position code.

        Stored codes are intentionally stable because historic load plans and
        layout templates already reference them.  The 13th pin-wheel position
        (PW1) is presented to the driver as L-07, matching the physical truck
        numbering used by operations.
        """
        code = (code or "").upper()
        if code == "PW1":
            return "L-07"
        if len(code) >= 2 and code[0] in ("L", "R") and code[1:].isdigit():
            return f"{code[0]}-{int(code[1:]):02d}"
        return code

    @staticmethod
    def _active_delivery_sequence(item):
        allocs = item.stop_allocation_ids.filtered(
            lambda a: a.active and a.stop_id and a.stop_id.status not in ("cancelled", "skipped")
        )
        seqs = allocs.mapped("stop_id.sequence")
        if not seqs and item.delivery_stop_id and item.delivery_stop_id.status not in ("cancelled", "skipped"):
            seqs = [item.delivery_stop_id.sequence]
        return min(seqs) if seqs else 999999

    @staticmethod
    def _destination_label(item):
        allocs = item.stop_allocation_ids.filtered(
            lambda a: a.active and a.stop_id and a.stop_id.status not in ("cancelled", "skipped")
        )
        stop = allocs.sorted(key=lambda a: a.stop_id.sequence)[:1].stop_id if allocs else item.delivery_stop_id
        if not stop:
            return "Destination not assigned"
        loc = stop.saved_location_id
        return (
            (loc.name if loc else False)
            or (loc.business_name if loc else False)
            or stop.job_id.partner_id.name
            or stop.address
            or f"Stop {stop.sequence}"
        )

    @staticmethod
    def _pickup_label(item):
        stop = item.pickup_stop_id or item.available_after_stop_id
        if not stop:
            return ""
        loc = stop.saved_location_id
        return (
            (loc.name if loc else False)
            or (loc.business_name if loc else False)
            or stop.job_id.partner_id.name
            or stop.address
            or f"Stop {stop.sequence}"
        )

    def recommend(self, load_plan):
        load_plan.ensure_one()
        tpl = load_plan.layout_template_id
        positions = tpl.position_ids.filtered(lambda p: p.active and not p.blocked)
        positions = sorted(
            positions,
            key=lambda p: (
                p.distance_from_rear_in if p.distance_from_rear_in is not None else 999999,
                0 if p.side == "driver" else 1 if p.side == "passenger" else 2,
                p.sequence,
                p.id,
            ),
        )

        # Physically received freight: can be positioned now.
        items = list(load_plan.pallet_ids.filtered(
            lambda i: i.status not in ("cancelled", "delivered")
            and i.consumes_floor_position
            and not i.pending_future_pickup
        ))
        # Future pickups: NOT positionable yet — the driver has not received
        # them — but they ARE plannable: the recommendation proposes one
        # vacant position to RESERVE per future pallet, and Accept persists
        # that as a pending reserve_position operation. The state stays
        # RESERVED (never Assigned/Loaded) until the pickup happens.
        future_items = list(load_plan.pallet_ids.filtered(
            lambda i: i.status not in ("cancelled", "delivered")
            and i.consumes_floor_position
            and i.pending_future_pickup
        ))

        warnings = []
        if not items and not future_items:
            return {
                "strategy": "delivery_lifo",
                "positions": [],
                "moves": [],
                "new_placements": [],
                "unresolved_item_ids": [],
                "reserved_future_position_codes": [],
                "warnings": [],
                "summary": "No pallets to plan yet.",
            }

        # Positions already committed to a future pickup (pending
        # reserve_position operations) are NOT available to the planner —
        # the recommendation must respect existing reservations and never
        # propose a double-booked slot.
        reserved_position_ids = {
            op.to_position_id.id
            for op in load_plan.env["prema.dispatch.load.plan.operation"].search([
                ("load_plan_id", "=", load_plan.id),
                ("operation_type", "=", "reserve_position"),
                ("state", "=", "pending"), ("active", "=", True),
            ])
        }
        positions = [p for p in positions if p.id not in reserved_position_ids]

        # Unknown destinations are intentionally sorted last/deepest.  The
        # driver can still position them manually, but an optimizer cannot
        # safely decide unload accessibility until a destination is assigned.
        by_sequence = defaultdict(list)
        for item in items:
            by_sequence[self._active_delivery_sequence(item)].append(item)

        assigned = {}
        used_position_ids = set()
        cursor = 0

        # Keep already-good assignments inside each delivery band's allocated
        # depth slots.  This is the key to avoiding pointless shuffling every
        # time a later pickup is added to the route.
        for stop_seq in sorted(by_sequence):
            group = sorted(
                by_sequence[stop_seq],
                key=lambda i: (-(i.weight_lbs or 0.0), i.id),
            )
            band = positions[cursor: cursor + len(group)]
            cursor += len(group)
            band_ids = {p.id for p in band}

            kept = []
            for item in group:
                if item.position_id and item.position_id.id in band_ids and item.position_id.id not in used_position_ids:
                    assigned[item.id] = item.position_id
                    used_position_ids.add(item.position_id.id)
                    kept.append(item)

            remaining_items = [i for i in group if i not in kept]
            remaining_positions = [p for p in band if p.id not in used_position_ids]

            # Greedy side balance within the correct delivery-depth band.
            side_weight = {"driver": 0.0, "passenger": 0.0, "center": 0.0}
            for item_id, pos in assigned.items():
                if pos in band:
                    item = next((candidate for candidate in group if candidate.id == item_id), None)
                    if item:
                        side_weight[pos.side or "center"] = side_weight.get(pos.side or "center", 0.0) + (item.weight_lbs or 0.0)

            for item in remaining_items:
                if not remaining_positions:
                    break
                remaining_positions.sort(
                    key=lambda p: (
                        side_weight.get(p.side or "center", 0.0),
                        p.distance_from_rear_in if p.distance_from_rear_in is not None else 999999,
                        p.id,
                    )
                )
                pos = remaining_positions.pop(0)
                assigned[item.id] = pos
                used_position_ids.add(pos.id)
                side_weight[pos.side or "center"] = side_weight.get(pos.side or "center", 0.0) + (item.weight_lbs or 0.0)

        unresolved = [item for item in items if item.id not in assigned]

        # Future pickups get the remaining vacant positions nearest the rear
        # door (a later pickup needs the fewest rehandles), in
        # delivery-sequence order. These are reservation proposals only —
        # nothing is persisted until Accept.
        future_placements = []
        free_after_physical = [p for p in positions if p.id not in used_position_ids]
        for item in sorted(future_items, key=lambda i: (self._active_delivery_sequence(i), i.id)):
            if not free_after_physical:
                unresolved.append(item)
                warnings.append(
                    f"Future pickup {item.name} has no vacant position left — capacity is exhausted."
                )
                continue
            pos = free_after_physical.pop(0)
            seq = self._active_delivery_sequence(item)
            future_placements.append({
                "item_id": item.id,
                "item_name": item.name,
                "job_id": item.job_id.id,
                "job_name": item.job_id.name,
                "position_id": pos.id,
                "position_code": pos.position_code,
                "position_label": self._display_position_code(pos.position_code),
                "delivery_sequence": None if seq >= 999999 else seq,
                "destination": self._destination_label(item),
                "pickup_label": self._pickup_label(item),
                "future": True,
            })
            used_position_ids.add(pos.id)
        if unresolved:
            warnings.append(f"{len(unresolved)} pallet(s) do not fit in the active layout.")

        placements = []
        moves = []
        new_placements = []
        unknown_destination = []

        for item in items:
            pos = assigned.get(item.id)
            if not pos:
                continue
            stop_seq = self._active_delivery_sequence(item)
            destination = self._destination_label(item)
            if stop_seq >= 999999:
                unknown_destination.append(item.name)
            placement = {
                "item_id": item.id,
                "item_name": item.name,
                "position_id": pos.id,
                "position_code": pos.position_code,
                "position_label": self._display_position_code(pos.position_code),
                "delivery_sequence": None if stop_seq >= 999999 else stop_seq,
                "destination": destination,
            }
            placements.append(placement)

            current = item.position_id
            if not current:
                new_placements.append({
                    **placement,
                    "reason": (
                        "Placed near the rear for an earlier delivery."
                        if stop_seq < 999999
                        else "Destination is not assigned; verify before loading."
                    ),
                })
            elif current.id != pos.id:
                moves.append({
                    **placement,
                    "from_position_id": current.id,
                    "from_position_code": current.position_code,
                    "from_position_label": self._display_position_code(current.position_code),
                    "to_position_id": pos.id,
                    "to_position_code": pos.position_code,
                    "to_position_label": self._display_position_code(pos.position_code),
                    "reason": (
                        f"Move {item.name} so freight for earlier deliveries stays easier to unload."
                        if stop_seq < 999999
                        else f"Move {item.name}; destination is still unassigned, so keep it deeper until verified."
                    ),
                })

            if pos.max_weight_lbs and item.weight_lbs and item.weight_lbs > pos.max_weight_lbs:
                warnings.append(
                    f"{item.name} ({item.weight_lbs:.0f} lb) exceeds {self._display_position_code(pos.position_code)}'s configured weight limit."
                )
            if pos.four_way_required and not item.four_way_entry:
                warnings.append(
                    f"{item.name} may not be suitable for {self._display_position_code(pos.position_code)} because that position requires four-way entry."
                )

        if unknown_destination:
            warnings.append(
                f"{len(unknown_destination)} pallet(s) have no delivery destination. Assign the destination before relying on the optimized unload order."
            )

        # Keep the deepest currently-free positions visible as a planning hint
        # for later pickups.  They are NOT assigned or reserved in the DB; the
        # next pickup recalculates the whole recommendation from live state.
        occupied_target_ids = {p["position_id"] for p in placements}
        free_after_plan = [p for p in positions if p.id not in occupied_target_ids]
        future_count = int(load_plan.future_pickup_pallet_count or 0)
        reserved_future = list(reversed(free_after_plan))[:future_count]

        move_count = len(moves)
        place_count = len(new_placements)
        future_count = len(future_placements)
        if not move_count and not place_count and not future_count:
            summary = "Current load layout already matches the delivery order."
        else:
            parts = []
            if place_count:
                parts.append(f"place {place_count} pallet(s)")
            if move_count:
                parts.append(f"move {move_count} pallet(s)")
            if future_count:
                parts.append(f"reserve {future_count} position(s) for future pickup(s)")
            summary = "Recommended: " + " and ".join(parts) + "."

        return {
            "strategy": "delivery_lifo",
            "positions": placements + future_placements,
            "moves": moves,
            "new_placements": new_placements + [
                {**fp, "reason": "Future pickup — reserved nearest the rear door for the fewest rehandles."}
                for fp in future_placements
            ],
            "unresolved_item_ids": [i.id for i in unresolved],
            "reserved_future_position_codes": [
                self._display_position_code(p.position_code) for p in reserved_future
            ],
            "warnings": warnings,
            "summary": summary,
        }

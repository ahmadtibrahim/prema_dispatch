"""
Rule-based (not ML) pallet-position recommendation engine — see the
approved architecture: no historical Load Plan data exists to train or
validate a model on. Advisory only; the caller (prema.dispatch.load.plan)
never applies this without an explicit accept_recommendation() call.
"""


class DispatchRecommendationService:
    def __init__(self, env):
        self.env = env

    def recommend(self, load_plan):
        tpl = load_plan.layout_template_id
        positions = tpl.position_ids.filtered(lambda p: p.active and not p.blocked)
        occupied_position_ids = set(load_plan.pallet_ids.filtered(lambda i: i.status != "cancelled" and i.position_id).mapped("position_id.id"))
        vacant_positions = positions.filtered(lambda p: p.id not in occupied_position_ids)
        unresolved_items = self.env["prema.dispatch.item"]

        def min_stop_sequence(item):
            allocs = item.stop_allocation_ids.filtered("active")
            seqs = allocs.mapped("stop_id.sequence") if allocs else (
                [item.delivery_stop_id.sequence] if item.delivery_stop_id else []
            )
            return min(seqs) if seqs else 9999

        items = load_plan.pallet_ids.filtered(
            lambda i: i.status != "cancelled" and i.consumes_floor_position and not i.position_id
        )
        # Earliest deliveries go to positions closest to the rear/door
        # (unloaded first without moving later-stop freight out of the way).
        sorted_items = sorted(items, key=min_stop_sequence)
        sorted_positions = sorted(vacant_positions, key=lambda p: p.distance_from_rear_in)

        placements, warnings = [], []
        for item, pos in zip(sorted_items, sorted_positions):
            if item.four_way_entry and not pos.four_way_required:
                warnings.append(f"{item.name} needs four-way entry; position {pos.position_code} may not support it — verify equipment.")
            if pos.max_weight_lbs and item.weight_lbs and item.weight_lbs > pos.max_weight_lbs:
                warnings.append(f"{item.name} ({item.weight_lbs:.0f} lbs) exceeds position {pos.position_code}'s weight limit.")
            placements.append({"item_id": item.id, "position_id": pos.id, "position_code": pos.position_code})

        if len(sorted_items) > len(sorted_positions):
            unresolved_items = sorted_items[len(sorted_positions):]
            warnings.append(f"{len(unresolved_items)} pallet(s) have no available position under this layout.")

        # Driver/passenger-side weight balance (advisory only).
        side_weight = {"driver": 0.0, "passenger": 0.0}
        for placement in placements:
            item = self.env["prema.dispatch.item"].browse(placement["item_id"])
            pos = self.env["prema.dispatch.vehicle.layout.position"].browse(placement["position_id"])
            if pos.side in side_weight:
                side_weight[pos.side] += item.weight_lbs or 0.0
        if side_weight["driver"] and side_weight["passenger"]:
            heavier, lighter = max(side_weight.values()), min(side_weight.values())
            if heavier and (heavier - lighter) / heavier > 0.35:
                warnings.append("Recommended layout is significantly heavier on one side — review side balance.")

        return {
            "positions": placements,
            "unresolved_item_ids": [i.id for i in unresolved_items] if unresolved_items else [],
            "warnings": warnings,
        }

from odoo import models
from odoo.exceptions import UserError


class PremaDispatchLoadPlanOptimizer(models.Model):
    _inherit = "prema.dispatch.load.plan"

    def accept_recommendation(self, recommendation, version=None):
        """Apply one optimizer snapshot safely and atomically enough for UI use.

        The base implementation wrote targets one-by-one.  A full-layout
        recommendation may intentionally move pallet A into pallet B's current
        slot while B moves elsewhere, so all participating positions are first
        validated and cleared before the final unique target map is applied.
        """
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)

        recommendation = recommendation or {}
        placements = recommendation.get("positions") or []
        if not placements:
            return self.get_load_plan()

        items_by_id = {}
        target_by_item = {}
        used_target_ids = set()

        for placement in placements:
            item_id = int(placement.get("item_id") or 0)
            position_id = int(placement.get("position_id") or 0)
            if not item_id or not position_id:
                raise UserError("Optimizer recommendation is missing a pallet or position.")

            item = self.env["prema.dispatch.item"].browse(item_id)
            if not item.exists() or item.load_plan_id.id != self.id or item.status in ("cancelled", "delivered"):
                raise UserError("Optimizer recommendation contains freight that is not active on this load plan.")
            if item.pending_future_pickup:
                raise UserError("Future-pickup freight cannot be positioned before it is physically received.")

            pos = self._get_position(position_id)
            if pos.id in used_target_ids:
                raise UserError("Optimizer recommendation assigns two pallets to the same truck position.")
            used_target_ids.add(pos.id)
            items_by_id[item.id] = item
            target_by_item[item.id] = pos

        moving_ids = set(items_by_id)
        occupied_by_nonmoving = self.pallet_ids.filtered(
            lambda i: i.status not in ("cancelled", "delivered")
            and i.position_id
            and i.id not in moving_ids
            and i.position_id.id in used_target_ids
        )
        if occupied_by_nonmoving:
            raise UserError(
                "The load plan changed after this recommendation. Refresh and optimize again before applying it."
            )

        old_positions = {
            item_id: (item.position_id.id, item.position_id.position_code)
            for item_id, item in items_by_id.items()
        }

        # Clear participating assignments first so swaps/re-sequencing do not
        # create transient double-occupancy while the final map is applied.
        self.env["prema.dispatch.item"].browse(list(moving_ids)).write({"position_id": False})
        for item_id, pos in target_by_item.items():
            items_by_id[item_id].write({"position_id": pos.id})

        applied_moves = []
        for item_id, item in items_by_id.items():
            old_id, old_code = old_positions[item_id]
            new_pos = target_by_item[item_id]
            if old_id != new_pos.id:
                applied_moves.append({
                    "item_id": item.id,
                    "item_name": item.name,
                    "from_position": old_code or False,
                    "to_position": new_pos.position_code,
                })

        self._log_event(
            "recommendation_accepted",
            new_value={
                "strategy": recommendation.get("strategy") or "delivery_lifo",
                "applied_moves": applied_moves,
                "warnings": recommendation.get("warnings") or [],
            },
        )
        self._bump_version()
        return self.get_load_plan()

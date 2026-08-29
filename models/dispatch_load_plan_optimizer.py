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

        Future-pickup placements are NOT physical moves: they reserve a
        position (pending reserve_position operation) for freight that has
        not been picked up yet. They are delegated to the base helper and
        never touch item.position_id — the pallet only binds to the slot
        when confirm_future_pickup_operation runs at the real pickup.
        """
        self.ensure_one()
        self._check_access(require_not_locked=True)
        self._check_version(version)

        recommendation = recommendation or {}
        placements = recommendation.get("positions") or []
        if not placements:
            return self.get_load_plan()

        future_placements = [p for p in placements if p.get("future")]
        physical = [p for p in placements if not p.get("future")]
        for placement in future_placements:
            # §5: never reserve truck space for freight whose job is in an
            # unresolved temperature conflict (confirm_future_pickup_
            # operation blocks the actual bind as well).
            job_id = placement.get("job_id") or (
                self.env["prema.dispatch.item"].browse(
                    placement.get("item_id")).job_id.id
                if placement.get("item_id") else False)
            if job_id:
                job = self.env["prema.dispatch.job"].browse(job_id)
                if job.temperature_conflict:
                    raise UserError(
                        "Cannot reserve %s: temperature conflict on job %s — "
                        "authorize an override or remove the incompatible "
                        "freight before loading."
                        % (placement.get("position_code") or "position",
                           job.name))
            self._reserve_future_position(placement)
        if not physical:
            self._log_event(
                "recommendation_accepted",
                new_value={
                    "strategy": recommendation.get("strategy") or "future_pickup_reserve",
                    "reserved_positions": [
                        {
                            "job_name": p.get("job_name"),
                            "item_name": p.get("item_name"),
                            "position_code": p.get("position_code"),
                        } for p in future_placements
                    ],
                    "warnings": recommendation.get("warnings") or [],
                },
            )
            self._clear_stale_if_no_blocking()
            self._bump_version()
            return self.get_load_plan()

        items_by_id = {}
        target_by_item = {}
        used_target_ids = set()

        for placement in physical:
            item_id = int(placement.get("item_id") or 0)
            position_id = int(placement.get("position_id") or 0)
            if not item_id or not position_id:
                raise UserError("Optimizer recommendation is missing a pallet or position.")

            item = self.env["prema.dispatch.item"].browse(item_id)
            if not item.exists() or item.load_plan_id.id != self.id or item.status in ("cancelled", "delivered"):
                raise UserError("Optimizer recommendation contains freight that is not active on this load plan.")
            if item.pending_future_pickup:
                raise UserError("Future-pickup freight cannot be positioned before it is physically received.")
            # §5 safe-engine guard: incompatible reefer ranges onboard must be
            # authorized (temperature override) or the offending freight
            # removed BEFORE any pallet of that job is placed on a truck.
            if item.job_id.temperature_conflict:
                raise UserError(
                    "Cannot place %s: temperature conflict on job %s — "
                    "authorize an override or remove the incompatible "
                    "freight before loading."
                    % (item.name, item.job_id.name))

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
        self._clear_stale_if_no_blocking()
        self._bump_version()
        return self.get_load_plan()

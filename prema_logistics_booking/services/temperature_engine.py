# -*- coding: utf-8 -*-
"""Safe multi-shipment temperature engine — 18-section work order §5-§6.

Single authority for:

- per-shipment requirement resolution (target / min / max from the booking,
  tolerance applied only when configured, 0°C preserved);
- the job-level safe intersection (safe_min = max of active minimums,
  safe_max = min of active maximums; compatible iff safe_min <= safe_max);
- setpoint choice: a shipment target inside EVERY active range wins; the
  midpoint of the intersection is the documented fallback (never an
  average of requirements — incompatible freight is NEVER averaged);
- CONFLICT state: incompatible ranges → 'TEMPERATURE CONFLICT — DISPATCH
  REVIEW REQUIRED', the conflicting items identified by booking/pallet,
  automatic route release blocked;
- dynamic reefer state machine: none / precool / on / off / conflict,
  recomputed after pickup/delivery completion, pallet-count change,
  restore, skip, reorder, transfer, override and refresh.

Driver-facing strings are built here so the app never computes its own
setpoint. States are stored on the job; the stored values are recomputed
by `recalc()` whenever a trigger fires.
"""

import json
import logging
from datetime import timedelta

from odoo import fields

from ..services.temperature_service import (
    format_dual,
    range_dual,
)

_logger = logging.getLogger(__name__)

# Item statuses that mean "physically on the truck right now" (or in
# transit). Pending-future-pickup items are NOT onboard; delivered and
# cancelled items are NOT onboard.
_ONBOARD_STATUSES = {"in_transit", "loaded", "partially_unloaded"}

REEFER = "reefer"


class TemperatureEngine:
    def __init__(self, env):
        self.env = env

    # ── per-shipment requirement ──────────────────────────────────────

    def _booking_of(self, item):
        """Canonical item → booking link. Preferred: the booking-pallet
        bridge (per-pallet allocations carry it). Fallback: the item's own
        pickup stop's booking stop (legacy shared/dedicated shapes — the
        pickup stop still identifies the shipment even on consolidated
        jobs, where only job_id moves). Empty recordset when neither
        exists (no resolvable requirement)."""
        pallet = item.logistics_booking_pallet_id
        if pallet:
            return pallet.booking_id
        stop = item.pickup_stop_id
        if stop and "logistics_booking_stop_id" in stop._fields:
            bstop = stop.logistics_booking_stop_id
            if bstop:
                return bstop.booking_id
        return self.env["logistics.booking"]

    def item_requirement(self, item):
        """Effective (range_min_c, range_max_c, target_c, supplied) for one
        pallet item's booking — or None when the freight has no reefer
        requirement. Tolerance is applied ONLY when no explicit min/max is
        configured. Unset target (dry / missing) → None."""
        booking = self._booking_of(item)
        if not booking:
            return None
        if booking.temperature_mode != REEFER:
            return None
        # Existence comes from the supplied-flags (written at the
        # create/write boundary), NEVER from identity on the float —
        # Odoo 18 reads an unset Float back as 0.0.
        if not booking.temperature_supplied:
            return None
        target = booking.target_temperature_c
        supplied_min = booking.minimum_temperature_supplied
        supplied_max = booking.maximum_temperature_supplied
        tolerance = booking.temperature_tolerance_c
        has_tolerance = tolerance not in (False, None) and tolerance > 0
        rmin = (
            booking.minimum_temperature_c if supplied_min
            else (target - tolerance if has_tolerance else target)
        )
        rmax = (
            booking.maximum_temperature_c if supplied_max
            else (target + tolerance if has_tolerance else target)
        )
        return {
            "item": item,
            "booking": booking,
            "target_c": target,
            "range_min_c": rmin,
            "range_max_c": rmax,
            "tolerance_c": tolerance,
            "source": booking.temperature_requirement_source,
        }

    def _active_items(self, job):
        """Items contributing to the current onboard requirement set."""
        return job.item_ids.filtered(
            lambda it: it.status in _ONBOARD_STATUSES
            and not it.pending_future_pickup
        )

    def _upcoming_reefer_items(self, job):
        """Reefer items whose pickup stop has NOT departed — pre-cool
        candidates (the next reefer pickup drives the pre-cool timing)."""
        return job.item_ids.filtered(
            lambda it: it.pending_future_pickup
            and self._booking_of(it).temperature_mode == REEFER
        )

    # ── route-level resolution ────────────────────────────────────────

    def resolve(self, job, persist=True):
        """Compute the job's temperature instruction.

        Returns the full state dict. With persist=True the authoritative
        fields are stored on the job and a timeline event is emitted only
        when the state actually CHANGES (noise-free recomputes).

        States:
          none    — no reefer freight anywhere on this job
          precool — no reefer onboard yet; reefer pickup(s) ahead: pre-cool
          on      — reefer freight onboard: keep the setpoint
          off     — all reefer freight delivered/removed AND safe to switch
                    off (guards: nothing onboard, no upcoming reefer pickup,
                    no un-departed reefer transfer/return)
          conflict— incompatible ranges with no authorized override
        """
        onboard = self._active_items(job)
        onboard_reqs = []
        for item in onboard:
            req = self.item_requirement(item)
            if req:
                onboard_reqs.append(req)

        upcoming = self._upcoming_reefer_items(job)
        override = self._active_override(job)

        state = "none"
        setpoint_c = None
        safe_min_c = safe_max_c = None
        conflict_items = []
        compatible = True
        message = ""

        if onboard_reqs:
            safe_min_c = max(r["range_min_c"] for r in onboard_reqs)
            safe_max_c = min(r["range_max_c"] for r in onboard_reqs)
            compatible = safe_min_c <= safe_max_c
            if not compatible:
                # Never average. Identify the bookings/pallets whose ranges
                # are disjoint from the (empty) consensus.
                conflict_items = [
                    r for r in onboard_reqs
                    if r["range_max_c"] < safe_min_c or r["range_min_c"] > safe_max_c
                ]
                if override:
                    state = "on"
                    setpoint_c = override.selected_setpoint_c
                    message = (
                        "TEMPERATURE CONFLICT OVERRIDDEN — dispatch-authorized "
                        f"setpoint {format_dual(override.selected_setpoint_c)}. "
                        f"Reason: {override.reason}"
                    )
                else:
                    state = "conflict"
                    message = "TEMPERATURE CONFLICT — DISPATCH REVIEW REQUIRED"
            else:
                # Prefer a shipment target inside every range.
                setpoint_c = next(
                    (r["target_c"] for r in onboard_reqs
                     if safe_min_c <= r["target_c"] <= safe_max_c),
                    (safe_min_c + safe_max_c) / 2.0,  # documented fallback
                )
                if override:
                    setpoint_c = override.selected_setpoint_c
                    message = (
                        "Dispatch-authorized setpoint override: "
                        f"{format_dual(setpoint_c)}"
                    )
                else:
                    message = (
                        "REEFER — maintain "
                        f"{format_dual(setpoint_c)}"
                    )
                state = "on"
        elif upcoming:
            # Pre-cool phase: the truck is empty of reefer freight but a
            # reefer pickup is ahead. Instruction is to pre-cool NOW.
            first = min(
                upcoming,
                key=lambda it: it.pickup_stop_id.sequence or 9999,
            )
            first_req = self.item_requirement(first)
            if first_req:
                setpoint_c = first_req["target_c"]
                safe_min_c = first_req["range_min_c"]
                safe_max_c = first_req["range_max_c"]
            state = "precool"
            message = (
                "PRE-COOL REEFER TO "
                f"{format_dual(setpoint_c)} — begin by "
                f"{self._precool_begin_label(job, first)}"
            )
        elif any(
            self._booking_of(it).temperature_mode == REEFER
            for it in job.item_ids
        ):
            # Reefer freight existed (delivered/removed) — nothing onboard,
            # nothing upcoming → safe to switch the reefer off. Guarded: if
            # any item were still onboard or pending pickup, the branches
            # above would have caught it.
            state = "off"
            message = "REEFER OFF — all reefer freight delivered"
        else:
            state = "none"

        result = {
            "state": state,
            "setpoint_c": setpoint_c,
            "safe_min_c": safe_min_c,
            "safe_max_c": safe_max_c,
            "compatible": compatible,
            "conflict": state == "conflict",
            "conflict_items": [
                {
                    "item_id": r["item"].id,
                    "item_name": r["item"].name,
                    "booking_id": r["booking"].id,
                    "booking_number": r["booking"].booking_number or "",
                    "range_min_c": r["range_min_c"],
                    "range_max_c": r["range_max_c"],
                    "target_c": r["target_c"],
                    "customer_id": r["booking"].partner_id.id,
                }
                for r in conflict_items
            ],
            "onboard_count": len(onboard_reqs),
            "upcoming_count": len(upcoming),
            "message": message,
            "override_id": override.id if override else False,
        }

        if persist:
            self._persist(job, result)
        return result

    def _precool_begin_label(self, job, first_item):
        """'Begin pre-cooling by 3:20 AM' — first binding constraint is the
        upcoming reefer pickup's service start, minus the configured
        pre-cool duration (booking-level field, default from the company
        parameter). Never hardcoded."""
        from odoo import fields as odoo_fields

        stop = first_item.pickup_stop_id
        anchor = (
            stop.customer_eta_at
            or stop.facility_service_start_at
            or stop.scheduled_time
        )
        if not anchor:
            return "first pickup service"
        minutes = self._booking_of(first_item).reefer_pre_cool_minutes
        if not minutes:
            minutes = int(self.env["ir.config_parameter"].sudo().get_param(
                "prema_dispatch.reefer_precool_minutes", "40"))
        begin = odoo_fields.Datetime.to_datetime(anchor) - timedelta(
            minutes=minutes)
        # context_timestamp needs a recordset (its tz drives the label —
        # the dispatcher sees the begin time in their own local time).
        local = odoo_fields.Datetime.context_timestamp(self.env.user, begin)
        return local.strftime("%-I:%M %p").lower()

    def _active_override(self, job):
        """Latest APPLIED override for this job (applied = driver-facing)."""
        # Partial-registry window: module-load test runs build prema_dispatch's
        # graph before prema_logistics_booking's Python has loaded (booking
        # depends on dispatch), and dispatch_stop.write lazily imports this
        # engine mid-test. No override row can exist before the booking
        # module's own upgrade created the table — treat as none, but say so.
        if "prema.dispatch.temperature.override" not in self.env.registry.models:
            _logger.warning(
                "temperature override model not registered (partial registry) "
                "— no override for job %s", job.id)
            return None
        return self.env["prema.dispatch.temperature.override"].sudo().search([
            ("job_id", "=", job.id),
            ("state", "=", "applied"),
        ], order="id desc", limit=1)

    # ── persistence ───────────────────────────────────────────────────

    def _persist(self, job, result):
        """Write the authoritative fields and emit a timeline event ONLY
        when the state (or setpoint) changed since the last compute."""
        prev = (
            job.temperature_state,
            job.temperature_instruction_c,
            job.temperature_range_min_c,
            job.temperature_range_max_c,
        )
        vals = {
            "temperature_state": result["state"],
            "temperature_instruction_c": result["setpoint_c"],
            "temperature_range_min_c": result["safe_min_c"],
            "temperature_range_max_c": result["safe_max_c"],
            "temperature_conflict": result["conflict"],
            "temperature_message": result["message"],
        }
        job.write(vals)
        # §5: mirror the conflict state onto the source bookings — the
        # booking's temperature_override_required flag is what blocks route
        # release at the load-plan layer and tells the dispatcher review
        # screen that authorization is pending. Cleared as soon as the
        # engine no longer sees a conflict (apply_override re-persists with
        # the authorized setpoint; removing the freight also clears).
        if result["conflict"]:
            affected = self.env["logistics.booking"].browse({
                c["booking_id"] for c in result.get("conflict_items", [])
                if c.get("booking_id")})
            flagged = affected.filtered(
                lambda b: not b.temperature_override_required)
            if flagged:
                flagged.write({"temperature_override_required": True})
        else:
            cleared = self.env["logistics.booking"].browse({
                self._booking_of(it).id
                for it in job.item_ids if it.temperature_supplied
                and self._booking_of(it)}).filtered(
                    lambda b: b.temperature_override_required)
            if cleared:
                cleared.write({"temperature_override_required": False})
        def _norm_sp(state, value):
            # Odoo 18 reads an unset Float back as 0.0 — a stored NULL
            # setpoint (none/off/conflict states) must compare equal to
            # the result's None, or every refresh would re-emit. Only
            # on/precool carry a real setpoint (0.0 is a legit °C there).
            if state in ("on", "precool") and value is not None:
                return value
            return None

        changed = (
            prev[0] != result["state"]
            or _norm_sp(result["state"], prev[1])
            != _norm_sp(result["state"], result["setpoint_c"])
        )
        if changed:
            # §6: a CHANGED instruction supersedes the previous
            # acknowledgment — a stale ack (old setpoint/state) must never
            # be presented to the driver as current, so the driver is asked
            # to re-confirm the new instruction. Recompute-only refreshes
            # (no state/setpoint change) leave the ack intact. (The former
            # `or result["conflict"]` term also re-emitted the conflict
            # timeline event on every refresh while conflicted — state
            # transitions already cover conflict entry/exit.)
            if job.reefer_acknowledged or job.reefer_off_acknowledged:
                job.write({
                    "reefer_acknowledged": False,
                    "reefer_off_acknowledged": False,
                    "reefer_ack_at": False,
                    "reefer_ack_user_id": False,
                    "reefer_off_ack_at": False,
                    "reefer_off_ack_user_id": False,
                })
        if changed and not self.env.context.get("no_temperature_timeline"):
            if result["state"] == "conflict":
                names = ", ".join(
                    f"{c['booking_number'] or c['item_name']}"
                    for c in result["conflict_items"][:3])
                job._post_timeline(
                    job, "temperature_conflict",
                    notes=(
                        "TEMPERATURE CONFLICT — DISPATCH REVIEW "
                        f"REQUIRED ({names})"
                    ),
                )
                # §9 feed: conflicts are severity-URGENT operational events —
                # automatic route release is already blocked by the engine.
                job._emit_feed(
                    "temperature_conflict", severity="urgent",
                    message=(
                        "TEMPERATURE CONFLICT — DISPATCH REVIEW "
                        f"REQUIRED ({names})"
                    ),
                )
            elif result["state"] == "precool":
                job._post_timeline(
                    job, "temperature",
                    notes=(
                        "Reefer pre-cool required: "
                        f"{format_dual(result['setpoint_c'])} — "
                        f"{result['message']}"
                    ),
                )
                job._emit_feed(
                    "temperature_changed", severity="warning",
                    message=(
                        f"Reefer instruction changed: pre-cool "
                        f"{format_dual(result['setpoint_c'])} — "
                        f"{result['message']}"
                    ),
                )
            elif result["state"] == "on":
                job._post_timeline(
                    job, "temperature",
                    notes=(
                        "Reefer setpoint: "
                        f"{format_dual(result['setpoint_c'])} (range "
                        f"{range_dual(result['safe_min_c'], result['safe_max_c'])})"
                    ),
                )
                job._emit_feed(
                    "temperature_changed",
                    message=(
                        f"Reefer instruction changed: setpoint "
                        f"{format_dual(result['setpoint_c'])} (range "
                        f"{range_dual(result['safe_min_c'], result['safe_max_c'])})"
                    ),
                )
            elif result["state"] == "off":
                job._post_timeline(
                    job, "temperature",
                    notes="Reefer switched off — all reefer freight delivered",
                )
                job._emit_feed(
                    "temperature_changed",
                    message=(
                        "Reefer instruction changed: switched off — "
                        "all reefer freight delivered"
                    ),
                )
        return result

    def recalc(self, job):
        """Idempotent public entry: recompute and store. Safe to call on
        every refresh (no timeline noise when nothing changed)."""
        return self.resolve(job, persist=True)

    # ── conflict authorization ────────────────────────────────────────

    def apply_override(self, job, setpoint_c, reason, item_ids=None,
                       user_id=None):
        """Authorized dispatch override: records the original requirements
        and the new setpoint, marks the conflict resolved, notifies the
        driver through the stored instruction. Returns the override record
        and the updated engine state."""
        affected_items = item_ids or job.item_ids
        orig = {
            "target": job.temperature_instruction_c,
            "range_min": job.temperature_range_min_c,
            "range_max": job.temperature_range_max_c,
            "requirements": [
                {
                    "item_id": it.id,
                    "item_name": it.name,
                    "booking_id": self._booking_of(it).id,
                    "booking_number": self._booking_of(it).booking_number or "",
                    "target_c": self._booking_of(it).target_temperature_c,
                    "min_c": self._booking_of(it).minimum_temperature_c,
                    "max_c": self._booking_of(it).maximum_temperature_c,
                }
                for it in affected_items
                if self._booking_of(it)
            ],
        }
        override = self.env["prema.dispatch.temperature.override"].sudo().create({
            "job_id": job.id,
            "selected_setpoint_c": setpoint_c,
            "reason": reason,
            "override_user_id": user_id or self.env.uid,
            "override_at": fields.Datetime.now(),
            "affected_item_ids": [(6, 0, affected_items.ids)],
            "original_requirements_json": json.dumps(orig),
            "state": "applied",
        })
        # §5: record the authorization on the source bookings (the engine's
        # next persist clears their override_required flag — the reason,
        # authorizer and timestamp stay on the booking for the audit trail).
        bookings = self.env["logistics.booking"].browse({
            self._booking_of(it).id
            for it in affected_items if self._booking_of(it)})
        if bookings:
            bookings.write({
                "temperature_override_reason": reason,
                "temperature_override_user_id": user_id or self.env.uid,
                "temperature_override_at": fields.Datetime.now(),
            })
        # The engine's next persist records the new state + timeline.
        state = self.recalc(job)
        job._post_timeline(
            job, "temperature_override",
            notes=(
                "Temperature override: "
                f"{format_dual(setpoint_c)} — {reason} (by "
                f"{override.override_user_id.name or 'dispatcher'})"
            ),
        )
        return override, state

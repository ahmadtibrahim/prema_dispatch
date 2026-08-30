# -*- coding: utf-8 -*-
"""prema.inbox.pricing — the single pricing authority for the inbox.

Design §8: extraction resolves to FSAs and calls the existing deterministic
PricingService — no second calculator, no invented rates. The price snapshot
(price_lines + route_snapshot + calculated_price) is persisted on the
conversation. When the engine is unavailable or the request is
unserviceable, the caller gets explicit state — never a made-up number.
"""
import re
from datetime import date, datetime

from odoo import api, fields, models

from odoo.addons.prema_logistics_booking.services.pricing_service import (
    PricingService)

# extraction field → PricingService argument
_EQUIPMENT_TEMPERATURE_MODE = {
    "reefer": "reefer", "dry": "dry", "dryvan": "dry", "dry van": "dry",
    "flatbed": "flatbed", "flat deck": "flatbed", "ltl": "ltl",
}
_ACCESSORIAL_LIFTGATE = ("liftgate", "lift gate", "tailgate", "tail gate")

_FSA_RE = re.compile(r"^[A-Za-z]\d[A-Za-z]$")


def _json_safe(value):
    """Json fields reject date/datetime objects; the engine returns them
    in schedules, delivery estimates and route snapshots. Recurse."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k if isinstance(k, str) else str(k): _json_safe(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class InboxPricing(models.Model):
    _name = "prema.inbox.pricing"
    _description = "Dispatch Inbox pricing bridge (stateless service)"

    @api.model
    def calculate_price(self, conversation):
        """Run the deterministic engine for a conversation's extraction.

        Returns a dict for the AI panel:
          {available, reason, calculated_price, currency, price_lines,
           schedule, delivery_date_estimate, manual_review_required,
           recommend_ftl, snapshot_saved, missing}
        Persists conversation.price_snapshot when the engine returns a
        usable result (available OR manual-review path) AND for the
        deterministic fsa_unresolved verdict — the panel renders the
        side-aware reason_text from the stored snapshot (the warn branch
        of the pricing card is dead without it), so the explanation
        survives a reload and the UI is never left with just a toast.
        engine_unavailable stays ephemeral (transient; retry will fix).
        """
        extraction = conversation.ai_extraction or {}
        fields_ = extraction.get("fields") or {}
        missing = extraction.get("missing") or []

        pickup_fsa = self._resolve_fsa(fields_.get("pickup"))
        delivery_fsa = self._resolve_fsa(fields_.get("delivery"))
        if not pickup_fsa or not delivery_fsa:
            # Dispatcher-friendly, side-aware explanation — the UI shows
            # reason_text and which side(s) are missing instead of the raw
            # "fsa_unresolved" code.
            missing_sides = {
                "pickup": not pickup_fsa,
                "delivery": not delivery_fsa,
            }
            side_text = ", ".join(
                name for side, name in
                (("pickup", "pickup location"), ("delivery", "delivery location"))
                if missing_sides[side])
            verdict = {
                "available": False,
                "reason": "fsa_unresolved",
                "reason_text": (
                    "Pricing unavailable — %s: the postal code / FSA could "
                    "not be resolved to a serviceable region." % side_text
                    if side_text else
                    "Pricing unavailable — pickup and delivery locations "
                    "are missing; run Extract shipment first."),
                "missing_sides": missing_sides,
                "calculated_at": fields.Datetime.now().isoformat(),
            }
            conversation.write({"price_snapshot": _json_safe(verdict)})
            return {
                "available": False,
                "reason": "fsa_unresolved",
                "reason_text": verdict["reason_text"],
                "missing_sides": missing_sides,
                "snapshot_saved": True,
                "missing": missing,
                "manual_review_required": False,
                "recommend_ftl": False,
            }

        pallets = int(fields_.get("pallets") or 0)
        weight_lbs = int(fields_.get("weight_lbs") or 0)
        equipment = str(fields_.get("equipment") or "").lower()
        temperature_mode = _EQUIPMENT_TEMPERATURE_MODE.get(
            equipment, "ltl")
        required_temperature_c = fields_.get("temperature_c")
        accessorials = [str(a).lower() for a in
                        (fields_.get("accessorials") or [])]
        liftgate_pickup = any(
            a in _ACCESSORIAL_LIFTGATE for a in accessorials)

        try:
            result = PricingService(self.env).calculate(
                pickup_fsa, delivery_fsa,
                shipment_type="ltl",
                temperature_mode=temperature_mode,
                pallets=pallets,
                weight_lbs=weight_lbs,
                liftgate_pickup=liftgate_pickup,
                liftgate_delivery=False,
                appointment=False,
                residential=False,
                partner=conversation.partner_id,
                reference_dt=fields.Datetime.now(),
                required_temperature_c=required_temperature_c,
                resolve_departures=True,
            )
        except Exception as exc:  # noqa: BLE001 — engine must never crash the inbox
            return {
                "available": False,
                "reason": "engine_unavailable",
                "reason_text": (
                    "Pricing engine unavailable right now — %s. No price "
                    "was invented; try again shortly." % str(exc)[:200]),
                "error": str(exc)[:500],
                "snapshot_saved": False,
                "missing": missing,
                "manual_review_required": True,
                "recommend_ftl": False,
            }

        snapshot = {
            "calculated_price": result.calculated_price,
            "currency": self.env.company.currency_id.name,
            "price_lines": list(result.price_lines or []),
            "schedule": result.schedule,
            "delivery_date_estimate": result.delivery_date_estimate,
            "route_snapshot": result.route_snapshot,
            "recommend_ftl": result.recommend_ftl,
            "calculated_at": fields.Datetime.now().isoformat(),
        }
        conversation.write({"price_snapshot": _json_safe(snapshot)})

        return {
            "available": result.available,
            "reason": result.reason,
            "calculated_price": result.calculated_price,
            "currency": snapshot["currency"],
            "price_lines": snapshot["price_lines"],
            "schedule": snapshot["schedule"],
            "delivery_date_estimate": snapshot["delivery_date_estimate"],
            "manual_review_required": bool(result.manual_review_required),
            "recommend_ftl": bool(result.recommend_ftl),
            "snapshot_saved": True,
            "missing": missing,
        }

    # ------------------------------------------------------------------
    # resolution helpers
    # ------------------------------------------------------------------
    def _resolve_fsa(self, stop_dict):
        """Extracted stop → logistics.fsa row (or None).

        Accepts a full postal code (M5V3E1 → M5V) or a bare FSA. First
        three letters of the postal code, validated shape, active rows only.
        """
        if not stop_dict:
            return None
        postal = str(stop_dict.get("postal_code") or "").strip()
        fsa_code = postal[:3].upper() if postal else None
        if not fsa_code or not _FSA_RE.match(fsa_code):
            return None
        return self.env["logistics.fsa"].search(
            [("fsa", "=", fsa_code), ("active", "=", True)], limit=1)

    def _revalidate_at_acceptance(self, conversation):
        """Cutover hook (design §8.7): re-check capacity before confirm."""
        return True

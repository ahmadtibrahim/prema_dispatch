"""18.0.13.38.0 post-migration — Phases 3-10 batch (booking module side).

Phase 3 — c15 Monday SWON corridor finalization, the ONLY production
corridor configuration the batch spec explicitly allows:

1. Enables prior-day pickup on the Monday corridor (Sunday physical pickup
   → Monday linehaul, max 1 day, Mississauga hub) — the same feature that
   shipped default-OFF in 18.0.13.37.0. This does not create a second
   departure; capacity stays on the single Monday departure.
2. Moves the round-trip hub arrival from 15:00 to 16:00 (manual) — the
   dispatcher's explicit manual value is preserved and only corrected to
   the spec'd ~16:00, never recalculated.

Targeting is deliberately narrow: name ILIKE '%Southwestern Ontario%' +
operate_monday + same_day_return — the Thursday GTA–HHB–Niagara corridor
(also manual 15:00) is NEVER touched. Idempotent: every step re-checks
its precondition; safe to re-run on any DB state.

NOTE: `cr.env` does NOT exist on this Odoo build's migration cursor —
always build an explicit environment (api.Environment).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

C15_NAME_MATCH = "%SW Ontario / Windsor%"


def migrate(cr, version):
    _logger.info("18.0.13.38.0 post-migration: c15 prior-day pickup + 16:00 hub arrival")
    env = api.Environment(cr, SUPERUSER_ID, {})
    Corridor = env["logistics.corridor"].sudo()

    c15 = Corridor.search([
        ("name", "ilike", C15_NAME_MATCH),
        ("operate_monday", "=", True),
        ("same_day_return", "=", True),
        ("active", "in", (True, False)),
    ], limit=1)
    if not c15:
        _logger.warning(
            "18.0.13.38.0: c15 Monday SWON corridor not found (name ilike "
            "%r) — nothing to configure.", C15_NAME_MATCH)
        return

    # 1. Prior-day pickup: enabled, max 1 day, Mississauga hub.
    hub = env["logistics.hub"].sudo().search(
        [("name", "ilike", "%Mississauga%")], limit=1)
    hub_id = hub.id if hub else 1  # Mississauga hub id on Prod-db
    prior_vals = {
        "allow_prior_day_pickup": True,
        "prior_day_pickup_max_days": 1,
        "prior_day_pickup_hub_id": hub_id,
    }
    if not c15.allow_prior_day_pickup or c15.prior_day_pickup_max_days != 1 \
            or c15.prior_day_pickup_hub_id.id != hub_id:
        c15.write(prior_vals)
        _logger.info(
            "18.0.13.38.0: c15 prior-day pickup enabled (max 1 day, hub %s)",
            hub_id)
    else:
        _logger.info("18.0.13.38.0: c15 prior-day pickup already configured")

    # 2. Hub arrival 16:00 — only when the current value is MANUAL (the
    # dispatcher's explicit value is the authority; a suggested value is
    # recalculated by Calculate Route Times, never force-written).
    if c15.hub_arrival_time_source == "manual" and c15.destination_hub_arrival_time != 16.0:
        c15.write({
            "destination_hub_arrival_time": 16.0,
            "hub_arrival_time_source": "manual",
        })
        _logger.info("18.0.13.38.0: c15 hub arrival 15:00 -> 16:00 (manual kept)")
    else:
        _logger.info(
            "18.0.13.38.0: c15 hub arrival unchanged "
            "(source=%s value=%s)",
            c15.hub_arrival_time_source, c15.destination_hub_arrival_time)

    _logger.info("18.0.13.38.0 post-migration done (c15 id=%s)", c15.id)

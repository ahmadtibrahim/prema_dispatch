"""18.0.3.29.0 post-migration — Phases 3-10 batch (dispatch module side).

Seeds the TWO single-authority config parameters and the detention
numbering sequence, all idempotently — existing parameter values are
NEVER overwritten:

1. prema_dispatch.service_time_defaults — operational-class → default
   service minutes (Phase 8/9): retail 15, warehouse 30,
   distribution_center 60, grocery_dc 60. Consumed by
   prema.dispatch.location.planning_service_time_minutes() as the
   type-default tier of the ONE service-duration hierarchy.
2. prema_dispatch.detention_defaults — company-wide detention baseline
   (Phase 10): free 30 min, increment 30 min, rate 0.0. The rate is the
   SELL rate (buy ≠ sell — what Prema pays is tracked separately in
   vendor costs) and is configured on the first detention rule; the
   parameter only guarantees a deterministic baseline.
3. ir.sequence prema.dispatch.detention.item (prefix DET/, 4-digit) used
   by detention item numbering.

NOTE: `cr.env` does NOT exist on this Odoo build's migration cursor —
always build an explicit environment (api.Environment).
"""
import json
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

SERVICE_TIME_DEFAULTS = {
    "retail": 15,
    "warehouse": 30,
    "distribution_center": 60,
    "grocery_dc": 60,
}
DETENTION_DEFAULTS = {
    "free_minutes": 30,
    "increment_minutes": 30,
    "rate_per_increment": 0.0,
}


def migrate(cr, version):
    _logger.info("18.0.3.29.0 post-migration: service-time + detention defaults")
    env = api.Environment(cr, SUPERUSER_ID, {})
    Param = env["ir.config_parameter"].sudo()

    for key, defaults in (
        ("prema_dispatch.service_time_defaults", SERVICE_TIME_DEFAULTS),
        ("prema_dispatch.detention_defaults", DETENTION_DEFAULTS),
    ):
        existing = Param.get_param(key)
        if existing:
            try:
                current = json.loads(existing or "{}")
            except ValueError:
                current = {}
            merged = dict(defaults)
            merged.update(current)  # user-tuned values always win
            if merged != current:
                Param.set_param(key, json.dumps(merged))
                _logger.info("18.0.3.29.0: %s merged with existing values", key)
            else:
                _logger.info("18.0.3.29.0: %s already present — untouched", key)
        else:
            Param.set_param(key, json.dumps(defaults))
            _logger.info("18.0.3.29.0: %s seeded", key)

    Sequence = env["ir.sequence"].sudo()
    seq = Sequence.search(
        [("code", "=", "prema.dispatch.detention.item")], limit=1)
    if not seq:
        Sequence.create({
            "name": "Detention Item",
            "code": "prema.dispatch.detention.item",
            "prefix": "DET/",
            "padding": 4,
            "number_increment": 1,
            "company_id": False,
        })
        _logger.info("18.0.3.29.0: detention sequence created")
    else:
        _logger.info("18.0.3.29.0: detention sequence already exists")

    _logger.info("18.0.3.29.0 post-migration done")

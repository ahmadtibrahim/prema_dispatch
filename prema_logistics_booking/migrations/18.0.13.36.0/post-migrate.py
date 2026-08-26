"""18.0.13.36.0 post-migration — corridor weekly sequence seeding (Phase 1).

The corridor model gains a pure DISPLAY/planning key `weekly_sequence`
(default 10) plus a stored computed `schedule_weekday_index` (earliest
operating weekday, 0=Mon .. 6=Sun, 7 = none). Both are recomputed/created
by the ORM on module upgrade; this migration only seeds the new integer
column so existing corridors get the documented default instead of NULL
(a NULL would render as blank in the corridor list until touched).

Idempotent: only rows where the column is still NULL are touched; re-running
after a manual set leaves operator values intact. No scheduling logic is
touched — departure generation, operating days, pricing and capacity
authorities are unchanged (see _operating_weekdays and the corridor
write() schedule-fields guard).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # NOTE: `cr.env` does NOT exist on this Odoo build's migration cursor
    # (silently no-ops — the same bug shipped in 18.0.13.15.0 and
    # 18.0.13.35.0). Canonical pattern: explicit environment.
    env = api.Environment(cr, SUPERUSER_ID, {})
    Corridor = env["logistics.corridor"].sudo()
    missing = Corridor.search([("weekly_sequence", "=", False)])
    if missing:
        missing.write({"weekly_sequence": 10})
        _logger.info(
            "18.0.13.36.0: seeded weekly_sequence=10 on %s corridors",
            len(missing))
    else:
        _logger.info("18.0.13.36.0: no corridors needed weekly_sequence seeding")
    _logger.info("18.0.13.36.0 post-migration done")

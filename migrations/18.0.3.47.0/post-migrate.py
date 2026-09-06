"""18.0.3.47.0 post-migration — MP1 D-B3 (§18 detention completion).

Idempotent backfills for the §18 stop-kind dimension; safe to re-run:

1. detention rule pickup side: every existing rule predates the
   pickup/delivery split, so its three pickup columns are all empty
   (NULL/0). Copy the delivery-side columns into the pickup side — after
   this, the pickup side is "set" and the rule keeps applying its legacy
   numbers to BOTH stop kinds, exactly as it did before the upgrade.
   Rows that already carry any pickup-side value are left untouched.
2. detention item stop_kind: items created before the upgrade have an
   empty stop_kind. Derive it from the linked stop's stop_type using the
   same mapping as the model helper
   (prema.dispatch.detention.rule.detention_stop_kind): pickup /
   cross_dock_pickup are pickup-kind, everything else is delivery-kind.
   Items with no stop row keep NULL (only ever shown, never matched).

NOTE: `cr.env` does NOT exist on this Odoo build's migration cursor —
always build an explicit environment (api.Environment).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Rule pickup side = delivery side, only when the whole pickup
    #    side is still empty.
    cr.execute(
        """
        UPDATE prema_dispatch_detention_rule r
           SET pickup_free_minutes = r.free_minutes,
               pickup_increment_minutes = r.increment_minutes,
               pickup_rate_per_increment = r.rate_per_increment
         WHERE (pickup_free_minutes IS NULL OR pickup_free_minutes = 0)
           AND (pickup_increment_minutes IS NULL OR pickup_increment_minutes = 0)
           AND (pickup_rate_per_increment IS NULL OR pickup_rate_per_increment = 0)
        """
    )
    _logger.info("D-B3 migrate: pickup side backfilled on %s rule(s)",
                 cr.rowcount)

    # 2. Item stop_kind from the stop's stop_type (model mapping:
    #    pickup / cross_dock_pickup → pickup; everything else →
    #    delivery). Idempotent: only NULL rows are written.
    cr.execute(
        """
        UPDATE prema_dispatch_detention_item i
           SET stop_kind = CASE
                 WHEN s.stop_type IN ('pickup', 'cross_dock_pickup')
                    THEN 'pickup'
                 ELSE 'delivery'
               END
          FROM prema_dispatch_stop s
         WHERE s.id = i.stop_id
           AND i.stop_kind IS NULL
        """
    )
    _logger.info("D-B3 migrate: stop_kind backfilled on %s item(s)",
                 cr.rowcount)

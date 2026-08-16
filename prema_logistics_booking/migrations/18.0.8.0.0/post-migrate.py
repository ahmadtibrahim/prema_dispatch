"""18.0.8.0.0 post-migration — FTL Regional Pricing backfill.

Runs AFTER the new pricing_type / flat_rate columns exist (existing rows
receive the 'corridor_default' default when the columns are added).
Converts any rule created under the old "regional minimum" model without
touching corridor-level FTL $ / km, historical bookings, or agreed rates:

    ftl_rate_per_km_override > 0 → pricing_type = 'per_km'
    minimum_ftl_charge       > 0 → pricing_type = 'flat_rate'
                                   flat_rate   = minimum_ftl_charge
    otherwise                     → pricing_type = 'corridor_default' (unchanged)

The legacy minimum_ftl_charge column is intentionally NOT dropped.
"""
import logging

_logger = logging.getLogger(__name__)

# Raw SQL so the logic is directly unit-testable against a test cursor.
# Order matters: an override wins over a legacy minimum (per spec).
_BACKFILL_SQL = (
    "UPDATE logistics_ftl_regional_minimum "
    "SET pricing_type = 'per_km' "
    "WHERE pricing_type = 'corridor_default' AND ftl_rate_per_km_override > 0",
    "UPDATE logistics_ftl_regional_minimum "
    "SET pricing_type = 'flat_rate', flat_rate = minimum_ftl_charge "
    "WHERE pricing_type = 'corridor_default' AND minimum_ftl_charge > 0",
)


def backfill_legacy_pricing_types(cr):
    """Convert legacy regional-minimum rows to the new pricing types."""
    for statement in _BACKFILL_SQL:
        cr.execute(statement)


def migrate(cr, version):
    _logger.info("18.0.8.0.0 post-migration: backfilling FTL regional pricing types")
    backfill_legacy_pricing_types(cr)
    _logger.info("18.0.8.0.0 post-migration complete")

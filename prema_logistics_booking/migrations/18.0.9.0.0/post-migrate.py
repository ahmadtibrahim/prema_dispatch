"""18.0.9.0.0 post-migration — FTL Regional Pricing manual ordering.

Runs AFTER the new sequence column exists (pre-existing rows receive the
default 10). Renumbers each corridor's rules 10, 20, 30, ... preserving
their previous visible order (origin region, destination region, id).
Pricing values are never touched.
"""
import logging

_logger = logging.getLogger(__name__)

# Raw SQL so the logic is directly unit-testable against a test cursor.
_RENUMBER_SQL = """
UPDATE logistics_ftl_regional_minimum AS rule
SET sequence = numbered.position * 10
FROM (
    SELECT id, row_number() OVER (
        PARTITION BY corridor_id
        ORDER BY origin_region_id, destination_region_id, id
    ) AS position
    FROM logistics_ftl_regional_minimum
) AS numbered
WHERE rule.id = numbered.id
"""


def renumber_ftl_regional_pricing(cr):
    """Assign each corridor's rules 10, 20, 30, ... in their previous
    visible order. Cosmetic only — no pricing fields are read or written."""
    cr.execute(_RENUMBER_SQL)


def migrate(cr, version):
    _logger.info("18.0.9.0.0 post-migration: renumbering FTL regional pricing rows")
    renumber_ftl_regional_pricing(cr)
    _logger.info("18.0.9.0.0 post-migration complete")

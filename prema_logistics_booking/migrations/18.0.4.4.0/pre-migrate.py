"""Pre-migration 18.0.4.4.0 — safe offering consolidation and pricing fixes.

Consolidates duplicate offerings that share lane+service_level+shipment_type.
Selects one canonical offering per group, repoints references, archives others.
Idempotent — safe to re-run.
"""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("18.0.4.4.0: pre-migration — consolidating offerings and fixing pricing")

    # 1. Consolidate target_load_quantity into planned_pallets
    cr.execute("""
        UPDATE logistics_rate_plan
        SET target_load_quantity = planned_pallets
        WHERE target_load_quantity IS DISTINCT FROM planned_pallets
    """)
    _logger.info("Rate Plan TLQ consolidated: %s rows", cr.rowcount)

    # 2. Find duplicate offerings (same lane, service_level, shipment_type) and
    #    repoint Rate Plans to the canonical (lowest-ID) offering in each group
    cr.execute("""
        WITH duplicates AS (
            SELECT lane_id, service_level_id, shipment_type,
                   MIN(id) as canonical_id,
                   COUNT(*) as cnt
            FROM logistics_service_offering
            WHERE active = true
            GROUP BY lane_id, service_level_id, shipment_type
            HAVING COUNT(*) > 1
        )
        UPDATE logistics_rate_plan rp
        SET service_offering_id = d.canonical_id
        FROM logistics_service_offering off
        JOIN duplicates d ON off.lane_id = d.lane_id
            AND off.service_level_id = d.service_level_id
            AND off.shipment_type = d.shipment_type
        WHERE rp.service_offering_id = off.id
          AND off.id != d.canonical_id
    """)
    _logger.info("Rate Plans repointed to canonical offerings: %s", cr.rowcount)

    # 3. Archive duplicate offerings
    cr.execute("""
        WITH duplicates AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY lane_id, service_level_id, shipment_type) as canonical_id
            FROM logistics_service_offering
            WHERE active = true
        )
        UPDATE logistics_service_offering
        SET active = false
        WHERE id IN (SELECT id FROM duplicates WHERE id != canonical_id)
    """)
    _logger.info("Duplicate offerings archived: %s", cr.rowcount)

    # 4. Update offering names to remove temperature wording
    cr.execute("""
        UPDATE logistics_service_offering
        SET name = REGEXP_REPLACE(name, ' \\(Dry\\)| \\(Chilled\\)| \\(Frozen\\)| \\(Reefer\\)', '', 'g')
        WHERE name ~ '\((Dry|Chilled|Frozen|Reefer)\)'
    """)
    _logger.info("Offering names cleaned: %s", cr.rowcount)

    _logger.info("18.0.4.4.0: pre-migration complete")

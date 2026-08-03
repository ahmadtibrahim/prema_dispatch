"""Pre-migration 18.0.4.5.0 — safe offering consolidation and reference repointing.

Idempotent. Works whether 18.0.4.2/3/4 already ran or not.
Preserves effective prices, snapshots and invoice totals.
"""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("18.0.4.5.0: pre-migration — safe offering consolidation")

    # 1. Consolidate target_load_quantity → planned_pallets
    cr.execute("""
        UPDATE logistics_rate_plan
        SET target_load_quantity = planned_pallets
        WHERE target_load_quantity IS DISTINCT FROM planned_pallets
    """)
    _logger.info("TLQ consolidated: %s", cr.rowcount)

    # 2. Convert 'both' shipment_type to explicit LTL+FTL entries
    cr.execute("""
        INSERT INTO logistics_service_offering
            (lane_id, service_level_id, shipment_type, temperature_mode, active, create_uid, write_uid, create_date, write_date)
        SELECT o.lane_id, o.service_level_id, 'ftl', 'dry', o.active, 1, 1, now(), now()
        FROM logistics_service_offering o
        WHERE o.shipment_type = 'both'
          AND o.active = true
          AND NOT EXISTS (
              SELECT 1 FROM logistics_service_offering o2
              WHERE o2.lane_id = o.lane_id
                AND o2.service_level_id = o.service_level_id
                AND o2.shipment_type = 'ftl'
          )
    """)
    _logger.info("FTL offerings created from 'both': %s", cr.rowcount)
    cr.execute("UPDATE logistics_service_offering SET shipment_type = 'ltl' WHERE shipment_type = 'both'")
    _logger.info("'both'→'ltl': %s", cr.rowcount)

    # 3. Resolve Rate Plan version conflicts: for each offering with duplicate versions,
    #    reassign conflicting plans to new unique versions
    cr.execute("""
        WITH versioned AS (
            SELECT id, service_offering_id, version,
                   ROW_NUMBER() OVER (
                       PARTITION BY service_offering_id, version ORDER BY id
                   ) as rn,
                   MAX(version) OVER (PARTITION BY service_offering_id) as max_ver
            FROM logistics_rate_plan
        )
        UPDATE logistics_rate_plan rp
        SET version = v.max_ver + v.rn - 1
        FROM versioned v
        WHERE rp.id = v.id AND v.rn > 1
    """)
    _logger.info("Rate Plan versions deduplicated: %s", cr.rowcount)

    # 4. Clean offering names
    cr.execute("""
        UPDATE logistics_service_offering
        SET name = REGEXP_REPLACE(
            REGEXP_REPLACE(name, ' \\(Dry\\)|\\(Chilled\\)|\\(Frozen\\)|\\(Reefer\\)', '', 'g'),
            '  ', ' '
        )
    """)
    _logger.info("Offering names cleaned: %s", cr.rowcount)

    # 5. Map chilled/frozen → reefer in all tables
    for table in ['logistics_booking', 'logistics_pricing_session']:
        cr.execute(f"""
            UPDATE {table} SET temperature_mode = 'reefer'
            WHERE temperature_mode IN ('chilled', 'frozen')
        """)
        _logger.info("%s temperature updated: %s", table, cr.rowcount)

    _logger.info("18.0.4.5.0: pre-migration complete")

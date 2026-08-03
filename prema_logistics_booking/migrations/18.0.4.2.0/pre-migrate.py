"""Pre-migration 18.0.4.2.0 — Dry/Reefer consolidation, offering cleanup.

Temperature must not participate in offering identity (final architecture,
brief §7), so the legacy 4-column unique(lane_id, service_level_id,
temperature_mode, shipment_type) constraint is dropped FIRST, and any
offerings that collide once temperature is ignored are merged (canonical =
lowest id, references repointed, loser archived) before the temperature
values themselves are updated.
"""
import logging
_logger = logging.getLogger(__name__)

REFERENCE_TABLES = (
    ("logistics_rate_plan", "service_offering_id"),
    ("logistics_customer_rate", "service_offering_id"),
    ("logistics_pricing_session", "service_offering_id"),
    ("logistics_lane_schedule", "service_offering_id"),
    ("logistics_booking", "service_offering_id"),
    ("logistics_booking_leg", "offering_id"),
)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    _logger.info("18.0.4.2.0: pre-migration")

    # 1. Drop the legacy 4-column unique constraint before touching any
    # temperature_mode values, so the merge below can never hit it.
    cr.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'logistics_service_offering'::regclass AND contype = 'u'
    """)
    for (conname,) in cr.fetchall():
        cr.execute(f'ALTER TABLE logistics_service_offering DROP CONSTRAINT "{conname}"')
        _logger.info("Dropped legacy offering unique constraint: %s", conname)

    # 2. Merge offerings that will become identical once temperature no
    # longer distinguishes them (dry vs chilled/frozen for the same
    # lane/level/shipment_type).
    cr.execute("""
        SELECT lane_id, service_level_id, shipment_type, array_agg(id ORDER BY id) AS ids
        FROM logistics_service_offering
        WHERE active = true
        GROUP BY lane_id, service_level_id, shipment_type
        HAVING count(*) > 1
    """)
    for lane_id, service_level_id, shipment_type, ids in cr.fetchall():
        canonical_id, dupes = ids[0], ids[1:]
        for ref_table, ref_col in REFERENCE_TABLES:
            if _column_exists(cr, ref_table, ref_col):
                cr.execute(
                    f"UPDATE {ref_table} SET {ref_col} = %s WHERE {ref_col} = ANY(%s)",
                    (canonical_id, dupes),
                )
        cr.execute("UPDATE logistics_service_offering SET active = false WHERE id = ANY(%s)", (dupes,))
        _logger.info(
            "Offering merge (temperature no longer distinguishes): lane=%s level=%s type=%s canonical=%s archived=%s",
            lane_id, service_level_id, shipment_type, canonical_id, dupes,
        )

    # 3. Map offering temperature_mode: chilled/frozen -> reefer (canonical
    # adapter — see services/temperature_compat.py).
    cr.execute("UPDATE logistics_service_offering SET temperature_mode = 'reefer' WHERE temperature_mode IN ('chilled', 'frozen')")
    _logger.info("chilled/frozen→reefer offerings: %s", cr.rowcount)

    # Map booking temperature_mode for historical records
    cr.execute("UPDATE logistics_booking SET temperature_mode = 'reefer' WHERE temperature_mode IN ('chilled','frozen')")
    _logger.info("booking temperature updated: %s", cr.rowcount)

    _logger.info("18.0.4.2.0: pre-migration complete")

"""Pre-migration 18.0.4.6.0 — repair pass for databases where 18.0.4.1.0-
18.0.4.5.0 already partially ran, and a from-scratch pass for databases
(like Prod-db-staging) upgrading straight from 18.0.4.1.0. Fully idempotent.

Key fix: the live DB constraint `logistics_service_offering_offering_uniq`
is a 4-column unique(lane_id, service_level_id, temperature_mode,
shipment_type) — an older constraint than what the current model declares.
Temperature must not participate in offering identity (brief §7), so this
migration DROPS that constraint and MERGES any offerings that differ only
by temperature_mode into one canonical row before anything else runs.
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


def _repoint_and_archive(cr, canonical_id, dupe_ids):
    if not dupe_ids:
        return
    for ref_table, ref_col in REFERENCE_TABLES:
        if _column_exists(cr, ref_table, ref_col):
            cr.execute(
                f"UPDATE {ref_table} SET {ref_col} = %s WHERE {ref_col} = ANY(%s)",
                (canonical_id, dupe_ids),
            )
    cr.execute(
        "UPDATE logistics_service_offering SET active = false WHERE id = ANY(%s)",
        (dupe_ids,),
    )


def migrate(cr, version):
    _logger.info("18.0.4.6.0: pre-migration start")

    # 1. Drop the legacy 4-column constraint FIRST — temperature must not
    # participate in offering identity, so nothing below may be blocked by it.
    cr.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'logistics_service_offering'::regclass
          AND contype = 'u'
    """)
    for (conname,) in cr.fetchall():
        cr.execute(f'ALTER TABLE logistics_service_offering DROP CONSTRAINT "{conname}"')
        _logger.info("Dropped legacy offering unique constraint: %s", conname)

    # 2. planned_pallets is the sole pricing denominator — repair any drift.
    if _column_exists(cr, "logistics_rate_plan", "target_load_quantity"):
        cr.execute("""
            UPDATE logistics_rate_plan SET target_load_quantity = planned_pallets
            WHERE target_load_quantity IS DISTINCT FROM planned_pallets
        """)
        _logger.info("planned_pallets/TLQ re-synced: %s", cr.rowcount)

    if not _column_exists(cr, "logistics_service_offering", "shipment_type"):
        _logger.info("18.0.4.6.0: offering table not present yet, skipping offering repair")
        return

    # 3. Split any remaining 'both' shipment_type offerings into explicit
    # ltl/ftl pairs (idempotent — matches 18.0.4.5.0's logic).
    cr.execute("""
        INSERT INTO logistics_service_offering
            (lane_id, service_level_id, shipment_type, temperature_mode, active, create_uid, write_uid, create_date, write_date)
        SELECT o.lane_id, o.service_level_id, 'ftl', 'dry', o.active, 1, 1, now(), now()
        FROM logistics_service_offering o
        WHERE o.shipment_type = 'both'
          AND NOT EXISTS (
              SELECT 1 FROM logistics_service_offering o2
              WHERE o2.lane_id = o.lane_id AND o2.service_level_id = o.service_level_id
                AND o2.shipment_type = 'ftl'
          )
    """)
    _logger.info("FTL offerings created from 'both': %s", cr.rowcount)
    cr.execute("UPDATE logistics_service_offering SET shipment_type = 'ltl' WHERE shipment_type = 'both'")
    _logger.info("'both'->'ltl': %s", cr.rowcount)

    # 4. Merge offerings that now differ ONLY by temperature_mode (dry vs
    # reefer/chilled/frozen) for the same (lane, level, shipment_type).
    # Canonical = lowest id. Every known reference is repointed; the rest
    # are archived, never deleted.
    cr.execute("""
        SELECT lane_id, service_level_id, shipment_type, array_agg(id ORDER BY id) AS ids
        FROM logistics_service_offering
        WHERE active = true
        GROUP BY lane_id, service_level_id, shipment_type
        HAVING count(*) > 1
    """)
    for lane_id, service_level_id, shipment_type, ids in cr.fetchall():
        canonical_id, dupes = ids[0], ids[1:]
        _repoint_and_archive(cr, canonical_id, dupes)
        _logger.info(
            "Offering merge: lane=%s level=%s type=%s canonical=%s archived=%s",
            lane_id, service_level_id, shipment_type, canonical_id, dupes,
        )
    # Canonical survivor's temperature_mode is now purely cosmetic — Dry and
    # Reefer share this one offering and one price.
    cr.execute("UPDATE logistics_service_offering SET temperature_mode = 'dry' WHERE active = true")

    # 5. Historical chilled/frozen -> reefer on bookings/pricing sessions
    # (repairs 18.0.4.2.0's earlier chilled->dry bug wherever it already ran).
    for table in ("logistics_booking", "logistics_pricing_session"):
        if _column_exists(cr, table, "temperature_mode"):
            cr.execute(f"UPDATE {table} SET temperature_mode = 'reefer' WHERE temperature_mode IN ('chilled','frozen')")
            _logger.info("%s chilled/frozen->reefer: %s", table, cr.rowcount)

    # 6. Resolve Rate Plan (offering, version) conflicts left over from the
    # offering merge above — never changes a price, only a version number.
    cr.execute("""
        SELECT service_offering_id, version, array_agg(id ORDER BY id DESC) AS ids
        FROM logistics_rate_plan GROUP BY service_offering_id, version HAVING count(*) > 1
    """)
    for offering_id, version, ids in cr.fetchall():
        for extra_id in ids[1:]:
            cr.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM logistics_rate_plan WHERE service_offering_id = %s",
                (offering_id,),
            )
            next_version = cr.fetchone()[0]
            cr.execute("UPDATE logistics_rate_plan SET version = %s WHERE id = %s", (next_version, extra_id))
        _logger.info("Rate plan version conflict resolved: offering=%s version=%s ids=%s", offering_id, version, ids)

    # 7. Active-only Offering uniqueness at the DB level (belt-and-suspenders
    # alongside the Python @api.constrains in the model).
    cr.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'logistics_service_offering_active_uniq'")
    if not cr.fetchone():
        cr.execute("""
            CREATE UNIQUE INDEX logistics_service_offering_active_uniq
            ON logistics_service_offering (lane_id, service_level_id, shipment_type)
            WHERE active = true
        """)
        _logger.info("Created partial unique index logistics_service_offering_active_uniq")

    # 8. Equipment Profile is deprecated/archived, never deleted.
    if _column_exists(cr, "logistics_equipment_profile", "active"):
        cr.execute("UPDATE logistics_equipment_profile SET active = false WHERE active = true")
        _logger.info("Equipment profiles archived: %s", cr.rowcount)

    _logger.info("18.0.4.6.0: pre-migration complete")

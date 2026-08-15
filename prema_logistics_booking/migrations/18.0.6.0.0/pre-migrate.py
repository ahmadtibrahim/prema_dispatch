"""18.0.6.0.0 pre-migration — deprecated-field removal, data-safe.

Runs BEFORE the new code loads (raw SQL only — the package's Python models
are not imported yet). All data that would be destroyed by the removal is
moved to canonical fields or archived here:

1. paired_return_service_id ← return_corridor_id   (feeds is_two_way)
2. origin/destination_hub_id ← start/end_hub_id via the old→canonical
   region map (non-blocking when no matching hub record exists)
3. Archive logistics_route_run / logistics_route_template tables — Odoo 18
   drops removed-model tables at _process_end, so these are mandatory
4. Copy legacy-agreement FSA pointers into a holding table so the
   post-migration (full ORM available) can migrate them to recurring jobs
"""
import logging

_logger = logging.getLogger(__name__)

# Mirrors RegionResolver.OLD_TO_NEW_PRIMARY (services/region_resolver.py).
OLD_TO_NEW = {
    1: 144, 2: 148, 3: 143, 4: 147, 5: 149,
    6: 150, 7: 157, 8: 151, 9: 153, 10: 154,
    16: 150, 17: 157, 18: 151, 19: 153, 20: 154,
}


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        [table, column],
    )
    return cr.fetchone()[0] > 0


def migrate(cr, version):
    _logger.info("18.0.6.0.0 pre-migration: deprecated-field data backfills")

    # 1. Return pairing — corridor 2 (QUEBEC→GTA) relies on the deprecated
    #    fallback for is_two_way; make paired_return_service_id canonical.
    if _column_exists(cr, "logistics_corridor", "return_corridor_id"):
        cr.execute("""
            UPDATE logistics_corridor
            SET paired_return_service_id = return_corridor_id
            WHERE return_corridor_id IS NOT NULL
              AND paired_return_service_id IS NULL
        """)
        _logger.info("  paired_return_service_id backfilled: %s row(s)", cr.rowcount)

    # 2. Hub backfills from the legacy region FKs (non-blocking).
    if _column_exists(cr, "logistics_corridor", "start_hub_id"):
        pairs = ", ".join("(%s, %s)" % (old, new) for old, new in sorted(OLD_TO_NEW.items()))
        cr.execute(
            "UPDATE logistics_corridor c SET origin_hub_id = h.id "
            "FROM (VALUES %s) AS v(old_id, new_id) "
            "JOIN logistics_hub h ON h.canonical_region_id = v.new_id "
            "WHERE c.start_hub_id = v.old_id AND c.origin_hub_id IS NULL" % pairs
        )
        _logger.info("  origin_hub_id backfilled: %s row(s)", cr.rowcount)
        cr.execute(
            "UPDATE logistics_corridor c SET destination_hub_id = h.id "
            "FROM (VALUES %s) AS v(old_id, new_id) "
            "JOIN logistics_hub h ON h.canonical_region_id = v.new_id "
            "WHERE c.end_hub_id = v.old_id AND c.destination_hub_id IS NULL" % pairs
        )
        _logger.info("  destination_hub_id backfilled: %s row(s)", cr.rowcount)
        cr.execute(
            "SELECT c.id, c.name FROM logistics_corridor c "
            "WHERE (c.start_hub_id IS NOT NULL AND c.origin_hub_id IS NULL) "
            "   OR (c.end_hub_id IS NOT NULL AND c.destination_hub_id IS NULL) "
            "ORDER BY c.id"
        )
        for row in cr.fetchall():
            _logger.warning("  Corridor %s (%s) left without a hub mapping — "
                            "no logistics.hub exists for its legacy region", row[0], row[1])

    # 3. Archive the deprecated route models' data. Odoo drops removed-model
    #    tables automatically at _process_end; the archive names are unknown
    #    to ir.model, so they survive the upgrade.
    for table in ("logistics_route_run", "logistics_route_template"):
        cr.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = %s", [table],
        )
        if cr.fetchone()[0]:
            cr.execute(
                "CREATE TABLE IF NOT EXISTS %s_archive AS SELECT * FROM %s" % (table, table)
            )
            cr.execute("SELECT count(*) FROM %s_archive" % table)
            _logger.info("  %s archived: %s row(s)", table, cr.fetchone()[0])

    # 4. Holding table for legacy-agreement FSA endpoints (consumed by the
    #    post-migration once the ORM is available).
    if _column_exists(cr, "logistics_recurring_agreement", "pickup_fsa_id"):
        cr.execute("DROP TABLE IF EXISTS _prema_legacy_agreement_fsa_backup")
        cr.execute("""
            CREATE TABLE _prema_legacy_agreement_fsa_backup AS
            SELECT a.id AS agreement_id,
                   fp.region_id AS pickup_region_id,
                   fd.region_id AS delivery_region_id
            FROM logistics_recurring_agreement a
            LEFT JOIN logistics_fsa fp ON a.pickup_fsa_id = fp.id
            LEFT JOIN logistics_fsa fd ON a.delivery_fsa_id = fd.id
            WHERE (a.pickup_fsa_id IS NOT NULL OR a.delivery_fsa_id IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM logistics_recurring_job j WHERE j.agreement_id = a.id
              )
        """)
        cr.execute("SELECT count(*) FROM _prema_legacy_agreement_fsa_backup")
        _logger.info("  legacy agreement endpoints staged: %s row(s)", cr.fetchone()[0])

    # 5. Informational counts of soon-to-be-dropped historical links.
    for table, column in (
        ("logistics_booking", "route_run_id"),
        ("logistics_recurring_agreement", "route_run_id"),
        ("prema_dispatch_job", "template_id"),
    ):
        if _column_exists(cr, table, column):
            cr.execute("SELECT count(*) FROM %s WHERE %s IS NOT NULL" % (table, column))
            _logger.info("  %s.%s populated rows: %s", table, column, cr.fetchone()[0])

    _logger.info("18.0.6.0.0 pre-migration complete")

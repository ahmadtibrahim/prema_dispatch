"""Post-migration: backfill canonical hub references and add hub region field.

- Adds canonical_region_id to logistics.hub
- Backfills YYZ-HUB → R1 (GTA Central) — the only confirmed mapping
- Backfills corridor origin_hub_id/destination_hub_id where R1 maps to YYZ-HUB
- Leaves ambiguous mappings NULL with logged record identifiers
"""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("prema_logistics_booking 18.0.3.1.0: post-migration — hub fields")

    # Add canonical_region_id to logistics.hub if not present
    cr.execute("""
        DO $$ BEGIN
            ALTER TABLE logistics_hub ADD COLUMN canonical_region_id INTEGER
            REFERENCES logistics_region(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
    """)
    _logger.info("  logistics_hub.canonical_region_id: ensured")

    # Backfill YYZ-HUB → R1 (confirmed mapping)
    cr.execute("SELECT id FROM logistics_region WHERE code = 'R1' LIMIT 1")
    r1_row = cr.fetchone()
    cr.execute("SELECT id, code FROM logistics_hub WHERE code = 'YYZ-HUB' LIMIT 1")
    hub_row = cr.fetchone()

    if r1_row and hub_row:
        r1_id = r1_row[0]
        hub_id, hub_code = hub_row

        # Set canonical_region_id on the hub
        cr.execute("""
            UPDATE logistics_hub SET canonical_region_id = %s
            WHERE id = %s AND canonical_region_id IS NULL
        """, [r1_id, hub_id])
        _logger.info("  YYZ-HUB.canonical_region_id → R1 (%s)", r1_id)

        # Backfill origin_hub_id where start_hub_id = R1
        cr.execute("""
            UPDATE logistics_corridor SET origin_hub_id = %s
            WHERE start_hub_id = %s AND origin_hub_id IS NULL
        """, [hub_id, r1_id])
        _logger.info("  Backfilled %d origin_hub_id from R1→YYZ-HUB", cr.rowcount)

        # Backfill destination_hub_id where end_hub_id = R1
        cr.execute("""
            UPDATE logistics_corridor SET destination_hub_id = %s
            WHERE end_hub_id = %s AND destination_hub_id IS NULL
        """, [hub_id, r1_id])
        _logger.info("  Backfilled %d destination_hub_id from R1→YYZ-HUB", cr.rowcount)
    else:
        _logger.warning("R1 region or YYZ-HUB not found — skipping hub backfill")

    # Log ambiguous corridors
    cr.execute("""
        SELECT c.id, c.name, sr.code, er.code
        FROM logistics_corridor c
        LEFT JOIN logistics_region sr ON c.start_hub_id = sr.id
        LEFT JOIN logistics_region er ON c.end_hub_id = er.id
        WHERE c.origin_hub_id IS NULL OR c.destination_hub_id IS NULL
    """)
    for row in cr.fetchall():
        _logger.info("  Ambiguous corridor %s (%s): start=%s end=%s — needs hub record",
                     row[0], row[1], row[2] or 'NULL', row[3] or 'NULL')

    _logger.info("prema_logistics_booking 18.0.3.1.0: post-migration complete")

"""Post-migration: backfill canonical hub references.

Maps R1 (GTA Central/Mississauga) → Mississauga Hub only.
No other region-to-hub mappings are known — leave them NULL.

Also sets legacy start_hub_id/end_hub_id as read-only in the model
by setting them as deprecated-computed fields (handled in model code).
"""
import logging
_logger = logging.getLogger(__name__)

# Confirmed mappings — only R1→Mississauga Hub is verified
HUB_MAPPING = {
    # region_code: hub_code (must exist in logistics.hub)
    # R1 maps to Mississauga Hub (YYZ-HUB) — confirmed
    # R12 (Ottawa), R13 (Montreal), R15 (Quebec City) — no hub records exist yet
    # DO NOT INVENT HUBS.
}


def migrate(cr, version):
    _logger.info("prema_logistics_booking 18.0.3.0: post-migration — backfilling hub references")

    # Find the Mississauga Hub
    cr.execute("SELECT id, code FROM logistics_hub WHERE code = 'YYZ-HUB' LIMIT 1")
    hub_row = cr.fetchone()
    if not hub_row:
        _logger.warning("No Mississauga Hub (YYZ-HUB) found — skipping hub backfill")
        return

    hub_id, hub_code = hub_row

    # Map R1 region → Mississauga Hub for corridor origin/destination
    cr.execute("SELECT id, code FROM logistics_region WHERE code = 'R1' LIMIT 1")
    r1_row = cr.fetchone()
    if r1_row:
        r1_id = r1_row[0]
        # Set origin_hub_id where start_hub_id points to R1
        cr.execute("""
            UPDATE logistics_corridor
            SET origin_hub_id = %s
            WHERE start_hub_id = %s AND origin_hub_id IS NULL
        """, [hub_id, r1_id])
        updated_origin = cr.rowcount
        # Set destination_hub_id where end_hub_id points to R1
        cr.execute("""
            UPDATE logistics_corridor
            SET destination_hub_id = %s
            WHERE end_hub_id = %s AND destination_hub_id IS NULL
        """, [hub_id, r1_id])
        updated_dest = cr.rowcount
        _logger.info(
            "Backfilled from R1→Mississauga Hub: %d origin_hub_id, %d destination_hub_id",
            updated_origin, updated_dest
        )
    else:
        _logger.warning("Region R1 not found — cannot backfill hub references")

    # Log ambiguous corridors for manual review
    cr.execute("""
        SELECT c.id, c.name, sr.code as start_code, er.code as end_code
        FROM logistics_corridor c
        LEFT JOIN logistics_region sr ON c.start_hub_id = sr.id
        LEFT JOIN logistics_region er ON c.end_hub_id = er.id
        WHERE c.origin_hub_id IS NULL OR c.destination_hub_id IS NULL
    """)
    ambiguous = cr.fetchall()
    for row in ambiguous:
        _logger.info(
            "Ambiguous corridor ID=%s name=%s: start=%s end=%s — needs hub record",
            row[0], row[1], row[2] or 'NULL', row[3] or 'NULL'
        )

    _logger.info("prema_logistics_booking 18.0.3.0: post-migration complete")
    _logger.info("Manually review %d corridors with missing hub assignments", len(ambiguous))

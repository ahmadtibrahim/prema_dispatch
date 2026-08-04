"""Pre-migration 18.0.4.7.0 — corridor stop-topology data repair.

Two concrete, verified data defects found by directly inspecting Prod-db
while scoping this pass (not previously documented):

1. Corridor "GTA -> OTTAWA & RETURN" has only 6 stop rows (R1,R10,R11,R8,R12,
   R12 — the last row duplicates R12 instead of starting the return leg), so
   there is currently no valid corridor for an Ottawa Valley -> GTA/hub leg.
   Any hub-transfer booking originating in Ottawa Valley fails today with
   "no_corridor_to_hub". Fixed by correcting the duplicate row and appending
   the return-direction stops back through R8 -> R11 -> R10 -> R1.

2. Corridor "LOCAL & REGIONAL OPERATIONS" has 8 stop rows but every one has
   region_id = NULL, so it cannot resolve any pickup/delivery at all. Fixed
   by assigning it the ring of regions immediately around GTA (R1 + R2/R3/R4
   Southwest/Golden-Horseshoe + R5/R6 north + R8/R9 east-central/Kawartha),
   excluding the far Northeast (R7) and the Ottawa/Quebec arm (R10-R15)
   already served by the other corridors.

Both corridors are identified by their stable business-key name, not a raw
numeric id, matching the pattern used by migrations/18.0.3.1.0.

Also unlinks the old action_where_we_go record: it was an ir.actions.act_url
and is becoming an ir.actions.client (Where We Go is now a real OWL client
action, not an iframe page). Odoo cannot retype an existing record's model
via a plain XML <record> update — the old row must be removed first so the
normal data-loading pass can create the new ir.actions.client record fresh
under the same XML id.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # ── 0. Retire the old action_where_we_go (ir.actions.act_url) ───────
    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'prema_logistics_booking' AND name = 'action_where_we_go'
          AND model = 'ir.actions.act_url'
    """)
    row = cr.fetchone()
    if row:
        old_action_id = row[0]
        try:
            from odoo import api, SUPERUSER_ID
            env = api.Environment(cr, SUPERUSER_ID, {})
            action = env["ir.actions.act_url"].browse(old_action_id)
            if action.exists():
                action.unlink()
            _logger.info("18.0.4.7.0 pre-migrate: removed old ir.actions.act_url action_where_we_go (id=%s)", old_action_id)
        except Exception:
            _logger.exception("18.0.4.7.0 pre-migrate: failed to unlink old action_where_we_go")
    else:
        _logger.info("18.0.4.7.0 pre-migrate: action_where_we_go already not an ir.actions.act_url, nothing to do")

    # ── 1. Fix "GTA -> OTTAWA & RETURN" missing return leg ──────────────
    cr.execute("""
        SELECT id FROM logistics_corridor WHERE name = %s
    """, ("GTA → OTTAWA & RETURN",))
    row = cr.fetchone()
    if row:
        ottawa_corridor_id = row[0]
        region_codes = {}
        cr.execute("SELECT code, id FROM logistics_region WHERE code IN ('R1','R8','R10','R11','R12')")
        for code, rid in cr.fetchall():
            region_codes[code] = rid

        if all(c in region_codes for c in ("R1", "R8", "R10", "R11", "R12")):
            # Correct the seq=60 duplicate R12 row into the return leg's first
            # hop (R8), then append the rest of the return leg.
            cr.execute("""
                UPDATE logistics_corridor_stop
                SET region_id = %s
                WHERE corridor_id = %s AND sequence = 60
            """, (region_codes["R8"], ottawa_corridor_id))
            return_stops = [
                (70, region_codes["R11"]),
                (80, region_codes["R10"]),
                (90, region_codes["R1"]),
            ]
            for seq, region_id in return_stops:
                cr.execute("""
                    SELECT 1 FROM logistics_corridor_stop
                    WHERE corridor_id = %s AND sequence = %s
                """, (ottawa_corridor_id, seq))
                if cr.fetchone():
                    continue
                cr.execute("""
                    INSERT INTO logistics_corridor_stop
                        (corridor_id, sequence, region_id, pickup_allowed, delivery_allowed)
                    VALUES (%s, %s, %s, true, true)
                """, (ottawa_corridor_id, seq, region_id))
            _logger.info(
                "18.0.4.7.0 pre-migrate: repaired GTA<->Ottawa return leg (corridor id=%s)",
                ottawa_corridor_id,
            )
        else:
            _logger.warning(
                "18.0.4.7.0 pre-migrate: skipped Ottawa corridor repair — one of R1/R8/R10/R11/R12 not found"
            )
    else:
        _logger.info("18.0.4.7.0 pre-migrate: 'GTA -> OTTAWA & RETURN' corridor not found, nothing to repair")

    # ── 2. Assign real regions to "LOCAL & REGIONAL OPERATIONS" ─────────
    cr.execute("""
        SELECT id FROM logistics_corridor WHERE name = %s
    """, ("LOCAL & REGIONAL OPERATIONS",))
    row = cr.fetchone()
    if row:
        local_corridor_id = row[0]
        local_codes = {}
        cr.execute("SELECT code, id FROM logistics_region WHERE code IN ('R1','R2','R3','R4','R5','R6','R8','R9')")
        for code, rid in cr.fetchall():
            local_codes[code] = rid

        ordered_codes = ["R1", "R4", "R2", "R3", "R6", "R5", "R9", "R8"]
        if all(c in local_codes for c in ordered_codes):
            cr.execute("""
                SELECT sequence FROM logistics_corridor_stop
                WHERE corridor_id = %s AND region_id IS NULL
                ORDER BY sequence
            """, (local_corridor_id,))
            null_seqs = [r[0] for r in cr.fetchall()]
            for seq, code in zip(null_seqs, ordered_codes):
                cr.execute("""
                    UPDATE logistics_corridor_stop SET region_id = %s
                    WHERE corridor_id = %s AND sequence = %s
                """, (local_codes[code], local_corridor_id, seq))
            _logger.info(
                "18.0.4.7.0 pre-migrate: assigned %s regions to LOCAL & REGIONAL OPERATIONS (corridor id=%s)",
                len(null_seqs), local_corridor_id,
            )
        else:
            _logger.warning(
                "18.0.4.7.0 pre-migrate: skipped local-ops corridor repair — one of R1-R9 not found"
            )
    else:
        _logger.info("18.0.4.7.0 pre-migrate: 'LOCAL & REGIONAL OPERATIONS' corridor not found, nothing to repair")

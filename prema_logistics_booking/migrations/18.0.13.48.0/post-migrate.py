# -*- coding: utf-8 -*-
"""18.0.13.48.0 — §3 canonical temperature backfill.

Every model in the booking→job→pallet→item chain now stores the canonical
Celsius requirement (target/min/max/tolerance + supplied-flags + unit +
source). This migration backfills the existing rows from their legacy
`required_temperature_c` (+ `temperature_mode` where present) with the
same rules as the booking's own canonical migration (18.0.13.44.0):

- Celsius only; the supplied-flags are the sanctioned existence checks.
- 0.0 is a REAL 0°C requirement for reefer and is kept.
- Dry/non-reefer rows carry no temperature at all (any junk numeric from
  the 0.0 read-back trap is cleared).
- target ↔ required mirror is backfilled in both directions.
- Idempotent: every statement is WHERE-guarded and rerunnable.
"""

import logging

_logger = logging.getLogger(__name__)

# Models that carry BOTH the legacy pair (temperature_mode +
# required_temperature_c) and the new canonical block.
_LEGACY_PAIR = [
    "logistics_pricing_session",
    "logistics_recurring_agreement",
    "logistics_recurring_job",
    "logistics_weekly_plan_reservation",
    "logistics_custom_quote",
]


def _has_columns(cr, table, columns):
    """True when every column exists — guards against partial upgrades."""
    cr.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = %s AND column_name = ANY(%s)",
        (table, list(columns)))
    found = {row[0] for row in cr.fetchall()}
    return set(columns) <= found


def _backfill_legacy_pair(cr, table):
    """Mirror + dry-cleanup + supplied flags + legacy source marker."""
    if not _has_columns(cr, table, (
            "required_temperature_c", "target_temperature_c",
            "temperature_mode", "temperature_supplied",
            "minimum_temperature_supplied",
            "maximum_temperature_supplied",
            "temperature_requirement_source")):
        _logger.warning("skip %s: canonical columns not all present", table)
        return
    # 1. Mirror both directions (raw values — 0.0 survives).
    cr.execute(
        "UPDATE %s SET target_temperature_c = required_temperature_c"
        " WHERE target_temperature_c IS NULL"
        "   AND required_temperature_c IS NOT NULL" % table)
    cr.execute(
        "UPDATE %s SET required_temperature_c = target_temperature_c"
        " WHERE required_temperature_c IS NULL"
        "   AND target_temperature_c IS NOT NULL" % table)
    # 2. Dry cleanup: non-reefer rows never carry a temperature (the
    #    legacy float read-back traps dry 0.0 as a value).
    cr.execute(
        "UPDATE %s SET required_temperature_c = NULL,"
        "              target_temperature_c = NULL,"
        "              minimum_temperature_c = NULL,"
        "              maximum_temperature_c = NULL,"
        "              temperature_tolerance_c = NULL"
        " WHERE temperature_mode IS DISTINCT FROM 'reefer'"
        "   AND (required_temperature_c IS NOT NULL"
        "     OR target_temperature_c IS NOT NULL)" % table)
    # 3. Supplied flags = existence (reefer + numeric; 0.0 counts).
    cr.execute(
        "UPDATE %s SET temperature_supplied ="
        "   (temperature_mode = 'reefer' AND required_temperature_c IS NOT NULL),"
        "   minimum_temperature_supplied ="
        "   (temperature_mode = 'reefer' AND minimum_temperature_c IS NOT NULL),"
        "   maximum_temperature_supplied ="
        "   (temperature_mode = 'reefer' AND maximum_temperature_c IS NOT NULL)"
        " WHERE temperature_mode IS NOT NULL" % table)
    # 4. Legacy source marker for rows that carry a requirement.
    cr.execute(
        "UPDATE %s SET temperature_requirement_source = 'legacy'"
        " WHERE temperature_requirement_source IS NULL"
        "   AND required_temperature_c IS NOT NULL" % table)


def migrate(cr, version):
    if not version:
        return  # fresh install — nothing to backfill
    for table in _LEGACY_PAIR:
        _backfill_legacy_pair(cr, table)

    # ── Pallet rows: snapshot from the booking ────────────────────────
    if _has_columns(cr, "logistics_booking_pallet", (
            "booking_id", "target_temperature_c", "temperature_supplied")):
        cr.execute("""
            UPDATE logistics_booking_pallet p
               SET target_temperature_c = b.target_temperature_c,
                   minimum_temperature_c = b.minimum_temperature_c,
                   maximum_temperature_c = b.maximum_temperature_c,
                   temperature_tolerance_c = b.temperature_tolerance_c,
                   temperature_supplied = b.temperature_supplied,
                   minimum_temperature_supplied =
                       b.minimum_temperature_supplied,
                   maximum_temperature_supplied =
                       b.maximum_temperature_supplied,
                   submitted_temperature_unit =
                       COALESCE(b.submitted_temperature_unit, 'c'),
                   temperature_requirement_source =
                       COALESCE(b.temperature_requirement_source, 'legacy')
              FROM logistics_booking b
             WHERE b.id = p.booking_id
               AND p.target_temperature_c IS NULL
               AND b.temperature_supplied IS TRUE
        """)

    # ── Item rows: from the pallet link, then the job snapshot ────────
    if _has_columns(cr, "prema_dispatch_item", (
            "logistics_booking_pallet_id", "target_temperature_c")):
        cr.execute("""
            UPDATE prema_dispatch_item i
               SET target_temperature_c = p.target_temperature_c,
                   minimum_temperature_c = p.minimum_temperature_c,
                   maximum_temperature_c = p.maximum_temperature_c,
                   temperature_tolerance_c = p.temperature_tolerance_c,
                   temperature_supplied = p.temperature_supplied,
                   minimum_temperature_supplied =
                       p.minimum_temperature_supplied,
                   maximum_temperature_supplied =
                       p.maximum_temperature_supplied,
                   submitted_temperature_unit =
                       COALESCE(p.submitted_temperature_unit, 'c'),
                   temperature_requirement_source =
                       COALESCE(p.temperature_requirement_source, 'legacy')
              FROM logistics_booking_pallet p
             WHERE p.id = i.logistics_booking_pallet_id
               AND i.target_temperature_c IS NULL
               AND p.temperature_supplied IS TRUE
        """)
        if _has_columns(cr, "prema_dispatch_item", ("job_id",)):
            cr.execute("""
                UPDATE prema_dispatch_item i
                   SET target_temperature_c = j.required_temperature_c,
                       temperature_supplied = TRUE,
                       submitted_temperature_unit = 'c',
                       temperature_requirement_source = 'legacy'
                  FROM prema_dispatch_job j
                 WHERE j.id = i.job_id
                   AND i.logistics_booking_pallet_id IS NULL
                   AND i.target_temperature_c IS NULL
                   AND j.required_temperature_c IS NOT NULL
            """)

    # ── Job rows: supplied flags from the frozen legacy value ─────────
    # NOTE: the job has NO target_temperature_c — its canonical target IS
    # the legacy required_temperature_c field itself (mirror 1:1, no copy).
    if _has_columns(cr, "prema_dispatch_job", (
            "required_temperature_c", "temperature_supplied",
            "requires_reefer", "temperature_requirement_source")):
        cr.execute(
            "UPDATE prema_dispatch_job"
            "   SET temperature_supplied ="
            "       (requires_reefer AND required_temperature_c IS NOT NULL),"
            "       minimum_temperature_supplied = FALSE,"
            "       maximum_temperature_supplied = FALSE,"
            "       temperature_requirement_source = 'legacy'"
            " WHERE temperature_supplied IS NOT TRUE"
            "   AND (requires_reefer IS TRUE"
            "     OR required_temperature_c IS NOT NULL)")

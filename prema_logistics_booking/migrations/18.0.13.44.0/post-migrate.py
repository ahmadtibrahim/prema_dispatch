# -*- coding: utf-8 -*-
"""18.0.13.44.0 — canonical temperature model backfill (18-section §3-§4).

- Legacy bookings that carry a numeric required_temperature_c are marked
  temperature_requirement_source='legacy' (they predate the customer-intake
  unit tracking); target_temperature_c is backfilled from
  required_temperature_c here (the mirror is a plain stored field synced
  at the create/write boundary — a stored `related` cannot write through
  in Odoo 18).
- Dry bookings that were previously written with the 0.0 "empty sentinel"
  (the OLD meaning of 0.0 = not set) are corrected to NULL, so 0.0 can now
  mean a real 0°C requirement without ambiguity.
- The pre-cool default parameter is ensured (noupdate data may skip when
  the module was already installed).
"""

import logging

_logger = logging.getLogger(__name__)

TEMPERATURE_COLUMNS = {
    "target_temperature_c",
    "minimum_temperature_c",
    "maximum_temperature_c",
    "temperature_tolerance_c",
    "temperature_supplied",
    "submitted_temperature_unit",
    "temperature_requirement_source",
    "temperature_override_required",
    "temperature_override_reason",
    "temperature_override_user_id",
    "temperature_override_at",
    "reefer_pre_cool_required",
    "reefer_pre_cool_temperature_c",
    "reefer_pre_cool_minutes",
}


def migrate(cr, version):
    # The module upgrade creates the new columns BEFORE post-migrate runs,
    # but a defensive check keeps this migration safe on odd upgrade paths.
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='logistics_booking' AND column_name='required_temperature_c'"
    )
    if not cr.fetchone():
        return

    # 1. Legacy source marker.
    cr.execute(
        "UPDATE logistics_booking "
        "SET temperature_requirement_source = 'legacy' "
        "WHERE temperature_requirement_source IS NULL "
        "AND required_temperature_c IS NOT NULL"
    )
    _logger.info(
        "temperature migration: %s legacy bookings marked source=legacy",
        cr.rowcount,
    )

    # 2. Correct the old 0.0-as-empty sentinel on DRY bookings only.
    #    (Reefer bookings with 0.0 keep it — 0°C is a real requirement.)
    cr.execute(
        "UPDATE logistics_booking "
        "SET required_temperature_c = NULL "
        "WHERE temperature_mode != 'reefer' "
        "AND required_temperature_c = 0.0"
    )
    _logger.info(
        "temperature migration: %s dry bookings cleared 0.0 sentinel",
        cr.rowcount,
    )

    # 2b. Supplied-flags for existing rows (the sanctioned existence
    #     checks — the floats themselves can never be identity-tested,
    #     Odoo 18 reads an unset Float back as 0.0). A reefer booking with
    #     a numeric temperature requirement is SUPPLIED; everything else
    #     is unset (dry, or the corrected 0.0 sentinel above).
    cr.execute(
        "UPDATE logistics_booking "
        "SET temperature_supplied = ("
        "    temperature_mode = 'reefer' "
        "    AND required_temperature_c IS NOT NULL"
        ")"
    )
    _logger.info(
        "temperature migration: supplied flags normalized for %s rows",
        cr.rowcount,
    )

    # 2c. Mirror backfill: target_temperature_c is a plain stored field
    #     synced at the create/write boundary (a stored `related` cannot
    #     write through in Odoo 18), so existing rows are fanned out here
    #     in both directions.
    cr.execute(
        "UPDATE logistics_booking "
        "SET target_temperature_c = required_temperature_c "
        "WHERE target_temperature_c IS NULL "
        "AND required_temperature_c IS NOT NULL"
    )
    cr.execute(
        "UPDATE logistics_booking "
        "SET required_temperature_c = target_temperature_c "
        "WHERE required_temperature_c IS NULL "
        "AND target_temperature_c IS NOT NULL"
    )
    _logger.info("temperature migration: mirror backfill complete")

    # 3. Ensure the pre-cool duration default parameter.
    cr.execute(
        "SELECT value FROM ir_config_parameter "
        "WHERE key = 'prema_dispatch.reefer_precool_minutes'"
    )
    if not cr.fetchone():
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value) "
            "VALUES ('prema_dispatch.reefer_precool_minutes', '40')"
        )
        _logger.info("temperature migration: pre-cool default parameter created")

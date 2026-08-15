"""18.0.6.0.0 post-migration — finish legacy-agreement migration + verify.

Runs AFTER the new code and schema are in place: the removed columns are
already dropped, so this consumes the holding table staged by pre-migrate.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import SUPERUSER_ID, api

    env = api.Environment(cr, SUPERUSER_ID, {})
    _logger.info("18.0.6.0.0 post-migration: legacy agreements + archive verification")

    # Migrate legacy agreements whose FSA endpoints were staged by pre-migrate.
    cr.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = '_prema_legacy_agreement_fsa_backup'"
    )
    if cr.fetchone()[0]:
        cr.execute(
            "SELECT agreement_id, pickup_region_id, delivery_region_id "
            "FROM _prema_legacy_agreement_fsa_backup ORDER BY agreement_id"
        )
        rows = cr.fetchall()
        Job = env["logistics.recurring.job"]
        Agreement = env["logistics.recurring.agreement"].with_context(active_test=False)
        migrated = 0
        for agreement_id, pickup_region_id, delivery_region_id in rows:
            agreement = Agreement.browse(agreement_id)
            if not agreement.exists():
                continue
            if not pickup_region_id or not delivery_region_id:
                _logger.warning(
                    "Agreement %s needs manual endpoint migration (FSA region missing)",
                    agreement_id,
                )
                continue
            Job.create({
                "agreement_id": agreement.id,
                "name": "Migrated recurring route",
                "pickup_kind": "region", "pickup_region_id": pickup_region_id,
                "delivery_kind": "region", "delivery_region_id": delivery_region_id,
                "frequency": agreement.frequency or "weekly",
                "preferred_weekday": str(agreement.preferred_weekday or 0),
                "monthly_week": "1",
                "pallets": agreement.pallets or 1,
                "weight_lbs": agreement.weight_lbs or 0.0,
                "load_type": agreement.load_type or "ltl",
                "temperature_mode": agreement.temperature_mode or "dry",
                "required_temperature_c": agreement.required_temperature_c,
                "temperature_confirmed": agreement.temperature_mode != "reefer",
                "commodity": agreement.commodity or "",
                "auto_generate": False,
            })
            migrated += 1
        cr.execute("DROP TABLE _prema_legacy_agreement_fsa_backup")
        _logger.info("  legacy agreements migrated to recurring jobs: %s", migrated)

    # Verify the archives staged by pre-migrate survived the table drops.
    for table in ("logistics_route_run", "logistics_route_template"):
        cr.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = %s", [table + "_archive"],
        )
        if cr.fetchone()[0]:
            cr.execute("SELECT count(*) FROM %s_archive" % table)
            _logger.info("  %s_archive rows: %s", table, cr.fetchone()[0])
        else:
            _logger.warning("  %s_archive missing", table)

    _logger.info("18.0.6.0.0 post-migration complete")

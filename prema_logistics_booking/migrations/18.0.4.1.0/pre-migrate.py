"""Pre-migration 18.0.4.1.0 — consolidate configuration."""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("18.0.4.1.0: pre-migration — consolidating Rate Plans and menus")

    # Consolidate target_load_quantity into planned_pallets
    cr.execute("""
        UPDATE logistics_rate_plan
        SET target_load_quantity = planned_pallets
        WHERE target_load_quantity != planned_pallets
           OR target_load_quantity IS NULL
    """)
    _logger.info("Rate Plan TLQ consolidated: %s rows", cr.rowcount)

    # Remove obsolete menus (idempotent — skips if already removed)
    for xmlid in [
        'menu_v4_weekly_ops', 'menu_v4_new_booking',
        'menu_v4_postal', 'menu_v4_daily_local',
        'menu_v4_sched_sim', 'menu_v4_rate_plans',
        'menu_v4_service_offerings', 'menu_v4_price_cfg',
        'menu_v4_equipment_profiles', 'menu_v4_geotab',
    ]:
        cr.execute("""
            DELETE FROM ir_ui_menu WHERE id = (
                SELECT res_id FROM ir_model_data
                WHERE module = 'prema_logistics_booking' AND name = %s
            )
        """, [xmlid])
        if cr.rowcount:
            _logger.info("Removed menu: %s", xmlid)
        cr.execute("""
            DELETE FROM ir_model_data
            WHERE module = 'prema_logistics_booking' AND name = %s
        """, [xmlid])

    _logger.info("18.0.4.1.0: pre-migration complete")

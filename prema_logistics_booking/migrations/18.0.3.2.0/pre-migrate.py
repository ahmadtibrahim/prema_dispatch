"""Pre-migration 18.0.3.2.0: add frozen pricing fields to booking legs."""
import logging
_logger = logging.getLogger(__name__)

NEW_COLUMNS = [
    ("lane_id", "INTEGER REFERENCES logistics_lane(id) ON DELETE SET NULL"),
    ("offering_id", "INTEGER REFERENCES logistics_service_offering(id) ON DELETE SET NULL"),
    ("rate_plan_id", "INTEGER REFERENCES logistics_rate_plan(id) ON DELETE SET NULL"),
    ("rate_plan_name", "VARCHAR"),
    ("rate_plan_version", "INTEGER"),
    ("currency_id", "INTEGER REFERENCES res_currency(id) ON DELETE SET NULL"),
    ("frozen_leg_price", "DOUBLE PRECISION"),
    ("frozen_price_breakdown", "JSONB"),
    ("transfer_hub_id", "INTEGER REFERENCES logistics_hub(id) ON DELETE SET NULL"),
]


def migrate(cr, version):
    _logger.info("prema_logistics_booking 18.0.3.2.0: pre-migration")
    for col, col_type in NEW_COLUMNS:
        cr.execute(f"""
            DO $$ BEGIN
                ALTER TABLE logistics_booking_leg ADD COLUMN {col} {col_type};
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$;
        """)
        _logger.info("  logistics_booking_leg.%s: ensured", col)
    _logger.info("prema_logistics_booking 18.0.3.2.0: pre-migration complete")

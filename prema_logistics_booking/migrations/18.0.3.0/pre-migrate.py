"""Pre-migration: add canonical hub fields and booking selections.

- Adds origin_hub_id, destination_hub_id, transfer_hub_id to logistics_corridor
- Adds service_mode, load_type, equipment_requirement to logistics_booking
- Safe to run on existing databases — only adds columns, no data changes.
"""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("prema_logistics_booking 18.0.3.0: pre-migration — adding canonical fields")

    # Add corridor hub fields if they don't exist
    for field, col_type in [
        ("origin_hub_id", "INTEGER"),
        ("destination_hub_id", "INTEGER"),
        ("transfer_hub_id", "INTEGER"),
        ("same_day_return", "BOOLEAN DEFAULT FALSE"),
        ("paired_return_service_id", "INTEGER"),
    ]:
        cr.execute(f"""
            DO $$ BEGIN
                ALTER TABLE logistics_corridor ADD COLUMN {field} {col_type};
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$;
        """)
        _logger.info("  logistics_corridor.%s: ensured", field)

    # Add booking selection fields
    for field, col_type, default in [
        ("service_mode", "VARCHAR", "'dedicated'"),
        ("load_type", "VARCHAR", "'ltl'"),
        ("equipment_requirement", "VARCHAR", "'dry'"),
    ]:
        cr.execute(f"""
            DO $$ BEGIN
                ALTER TABLE logistics_booking ADD COLUMN {field} VARCHAR;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$;
        """)
        cr.execute(f"""
            UPDATE logistics_booking SET {field} = {default}
            WHERE {field} IS NULL
        """)
        _logger.info("  logistics_booking.%s: ensured (default=%s)", field, default)

    _logger.info("prema_logistics_booking 18.0.3.0: pre-migration complete")

"""Pre-migration 18.0.4.2.0 — Dry/Reefer consolidation, offering cleanup."""
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("18.0.4.2.0: pre-migration")

    # Map offering temperature_mode: chilled→dry, frozen→reefer
    cr.execute("UPDATE logistics_service_offering SET temperature_mode = 'dry' WHERE temperature_mode = 'chilled'")
    _logger.info("chilled→dry offerings: %s", cr.rowcount)
    cr.execute("UPDATE logistics_service_offering SET temperature_mode = 'reefer' WHERE temperature_mode = 'frozen'")
    _logger.info("frozen→reefer offerings: %s", cr.rowcount)

    # Map booking temperature_mode for historical records
    cr.execute("UPDATE logistics_booking SET temperature_mode = 'reefer' WHERE temperature_mode IN ('chilled','frozen')")
    _logger.info("booking temperature updated: %s", cr.rowcount)

    _logger.info("18.0.4.2.0: pre-migration complete")

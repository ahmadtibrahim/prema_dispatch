"""Post-migration 18.0.4.6.0 — ORM-level repair requiring loaded models."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = None
    try:
        from odoo import api, SUPERUSER_ID
        env = api.Environment(cr, SUPERUSER_ID, {})
    except Exception:
        _logger.exception("18.0.4.6.0 post-migrate: could not build environment")
        return

    # Recompute offering display names now that 'both' no longer exists.
    offerings = env["logistics.service.offering"].search([])
    offerings._compute_name()
    _logger.info("18.0.4.6.0 post-migrate: recomputed %s offering names", len(offerings))

    # Log (never silently alter) the PB38446 configuration for the smoke-proof record.
    vehicle = env["fleet.vehicle"].search([("license_plate", "=", "PB38446")], limit=1)
    if vehicle:
        _logger.info(
            "18.0.4.6.0 post-migrate: PB38446 id=%s straight=%s pinwheel=%s payload=%s reefer=%s operational=%s",
            vehicle.id, vehicle.straight_pallet_capacity, vehicle.pin_wheel_pallet_capacity,
            vehicle.x_max_payload_lbs, vehicle.x_reefer, vehicle.x_operational_logistics,
        )
    else:
        _logger.warning("18.0.4.6.0 post-migrate: PB38446 not found by license_plate")

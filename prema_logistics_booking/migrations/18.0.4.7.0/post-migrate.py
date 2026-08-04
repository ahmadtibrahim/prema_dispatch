"""Post-migration 18.0.4.7.0 — menu correction + region.destination archival.

1. logistics.lane stops being a customer-pricing menu surface (Rate Plans is
   the sole writable customer-pricing authority). The XML deletion of
   menu_v4_lanes_pricing (Pricing -> "Service Routes" -> lane) is normally
   enough for Odoo's own ir.model.data diff to clean up the ir.ui.menu record
   on -u, but this migration makes the removal explicit and idempotent for
   databases where that XML-diff cleanup doesn't fire (e.g. a partial/manual
   upgrade history).
2. logistics.region.destination is archived (soft, reversible) — confirmed
   dead (no menu, no callers, no data-file population) and superseded by
   RouteResolver/DepartureResolver + logistics.corridor.stop.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    try:
        from odoo import api, SUPERUSER_ID
        env = api.Environment(cr, SUPERUSER_ID, {})
    except Exception:
        _logger.exception("18.0.4.7.0 post-migrate: could not build environment")
        return

    menu = env.ref("prema_logistics_booking.menu_v4_lanes_pricing", raise_if_not_found=False)
    if menu:
        menu.unlink()
        _logger.info("18.0.4.7.0 post-migrate: removed stale menu_v4_lanes_pricing")
    else:
        _logger.info("18.0.4.7.0 post-migrate: menu_v4_lanes_pricing already absent, nothing to do")

    destinations = env["logistics.region.destination"].with_context(active_test=False).search([("active", "=", True)])
    if destinations:
        destinations.write({"active": False})
    _logger.info("18.0.4.7.0 post-migrate: archived %s logistics.region.destination row(s)", len(destinations))

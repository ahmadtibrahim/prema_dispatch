"""Remove the obsolete Booking Templates setup entry."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import SUPERUSER_ID, api

    env = api.Environment(cr, SUPERUSER_ID, {})
    obsolete_xmlids = (
        "prema_dispatch.menu_booking_templates",
        "prema_dispatch.action_booking_templates",
        "prema_dispatch.view_booking_template_list",
        "prema_dispatch.view_booking_template_form",
        "prema_dispatch.ir_cron_dispatch_generate_bookings",
        "prema_dispatch.view_dispatch_duplicate_job_wizard_form",
        "prema_dispatch.access_duplicate_job_wizard_user",
    )
    for xmlid in obsolete_xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()

    # The booking.template model was removed in 18.0.3.0.0 — on an upgrade
    # chain through that version the model is gone from the registry by the
    # time this post-migration runs. Skip gracefully (its table is dropped
    # at _process_end of the same upgrade).
    if "prema.dispatch.booking.template" in env:
        templates = env["prema.dispatch.booking.template"].with_context(active_test=False).search([])
        if templates:
            templates.write({"active": False})
        _logger.info(
            "Removed obsolete Booking Templates UI/cron and archived %d historical template(s)",
            len(templates),
        )
    else:
        _logger.info("prema.dispatch.booking.template model removed — nothing to archive")

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

    templates = env["prema.dispatch.booking.template"].with_context(active_test=False).search([])
    if templates:
        templates.write({"active": False})
    _logger.info(
        "Removed obsolete Booking Templates UI/cron and archived %d historical template(s)",
        len(templates),
    )

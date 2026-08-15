"""18.0.3.0.0 post-migration — booking-template model removal.

The prema.dispatch.booking.template model, its cron and its ACLs are removed
from the code; Odoo drops the model's table at _process_end. Belt-and-braces:
unlink the cron record if a stale copy still exists in the database.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import SUPERUSER_ID, api

    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref("prema_dispatch.ir_cron_dispatch_generate_bookings", raise_if_not_found=False)
    if cron:
        cron.unlink()
        _logger.info("18.0.3.0.0: removed stale deprecated booking-template cron")
    else:
        _logger.info("18.0.3.0.0: deprecated booking-template cron already absent")
    _logger.info("18.0.3.0.0 post-migration complete")

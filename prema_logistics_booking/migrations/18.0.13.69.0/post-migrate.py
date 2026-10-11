# -*- coding: utf-8 -*-
"""Hard guarantee that two reservations can never claim the same position.

The ORM check runs under row locks, which is enough for the application. This
exclusion constraint is the backstop: even a concurrent insert that somehow
skipped the lock cannot create an overlapping reservation for one position.
Intervals are half-open, so a reservation may begin exactly when the previous
buffered window ends.
"""
import logging

_logger = logging.getLogger(__name__)

CONSTRAINT = "logistics_position_reservation_no_overlap"


def migrate(cr, version):
    cr.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
    if not cr.fetchone():
        cr.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    cr.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (CONSTRAINT,))
    if cr.fetchone():
        return
    cr.execute("""
        ALTER TABLE logistics_position_reservation
        ADD CONSTRAINT %s
        EXCLUDE USING gist (
            location_id WITH =,
            tsrange(date_start, date_end, '[)') WITH &&
        ) WHERE (state = 'reserved')
    """ % CONSTRAINT)
    _logger.info("PF Logistics: position overlap exclusion constraint created.")

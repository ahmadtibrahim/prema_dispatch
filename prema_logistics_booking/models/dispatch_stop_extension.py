"""Milk-run bridge fields on the dispatch stop.

These Many2one bridges point UP at logistics.booking.stop, which lives in
prema_logistics_booking (this module) while prema.dispatch.stop lives in
prema_dispatch (a dependency). A field with an upward comodel defined
directly in prema_dispatch breaks the registry when prema_dispatch is
upgraded alone — the comodel is not in the pool yet at field-setup time
and Odoo degrades the field to `_unknown`. Defining it here, via
_inherit, keeps the same proven pattern as dispatch_job_extension.py.
"""

from odoo import fields, models


class PremaDispatchStop(models.Model):
    _inherit = "prema.dispatch.stop"

    logistics_booking_stop_id = fields.Many2one(
        "logistics.booking.stop", string="Booking Stop",
        ondelete="set null", index=True,
        help="Stable bridge to the commercial booking stop (idempotency, "
             "sync, audit).")

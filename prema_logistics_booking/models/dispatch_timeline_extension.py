from odoo import api, fields, models


class PremaDispatchTimelineEventExtension(models.Model):
    """Booking-side extension of prema.dispatch.timeline.event.

    booking_id MUST live here (booking module), NOT in the base model:
    its source job_id.logistics_booking_id is defined by
    prema_logistics_booking, which loads AFTER prema_dispatch. A
    related= or stored-computed definition in the base module fails
    during prema_dispatch setup (existing rows recomputed before the
    extension field is in the registry). A lazy non-stored compute in
    this module resolves at read time, when both modules are loaded.
    """

    _inherit = "prema.dispatch.timeline.event"

    booking_id = fields.Many2one(
        "logistics.booking", string="Booking",
        compute="_compute_booking", readonly=True,
    )

    @api.depends("job_id")
    def _compute_booking(self):
        for rec in self:
            rec.booking_id = rec.job_id.logistics_booking_id.id

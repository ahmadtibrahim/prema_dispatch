"""Milk-run bridge fields on the dispatch item.

Same upward-comodel rule as dispatch_stop_extension.py: the Many2one to
logistics.booking.pallet must be defined here (in prema_logistics_booking)
so the comodel is always in the pool when the field is set up.

§15 (D-B4): an item edit changes the day's LOAD (pallets/weight/route
allocation) — any open day-route proposal covering the item's job goes
stale, exactly like the stop/job hooks.
"""

from odoo import api, fields, models

# Item fields whose change reshapes a day's capacity / movements.
ITEM_STALE_TRIGGERS = (
    "pallet_count", "weight_lbs", "status", "pickup_stop_id",
    "delivery_stop_id", "stop_allocation_ids",
)


class PremaDispatchItem(models.Model):
    _inherit = "prema.dispatch.item"

    logistics_booking_pallet_id = fields.Many2one(
        "logistics.booking.pallet", string="Booking Pallet",
        ondelete="set null", index=True,
        help="Stable bridge to the canonical booking pallet movement.")

    def write(self, vals):
        trigger = set(vals) & set(ITEM_STALE_TRIGGERS)
        pre_jobs = set()
        if trigger and not self.env.context.get("_day_route_silent"):
            # Job set BEFORE the write — delivery/stop reassignments move
            # items between jobs and must stale BOTH sides.
            pre_jobs = set(self.mapped("job_id.id"))
        result = super().write(vals)
        if trigger and not self.env.context.get("_day_route_silent"):
            job_ids = pre_jobs | set(self.mapped("job_id.id"))
            job_ids.discard(False)
            if job_ids:
                # @api.model helpers must be invoked through a recordset —
                # class-level calls hand the raw env in as ``self``.
                self.env["prema.dispatch.day.route.proposal"] \
                    ._mark_stale_for_jobs(
                        list(job_ids),
                        "A load item of this day changed (%s)."
                        % ", ".join(sorted(trigger)))
        return result

    @api.model
    def create(self, vals_list):
        records = super().create(vals_list)
        job_ids = list({r.job_id.id for r in records if r.job_id})
        if job_ids and not self.env.context.get("_day_route_silent"):
            self.env["prema.dispatch.day.route.proposal"] \
                ._mark_stale_for_jobs(
                    job_ids,
                    "A load item was added to one of this day's jobs.")
        return records

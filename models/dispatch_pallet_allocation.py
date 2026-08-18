from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PremaDispatchPalletStopAllocation(models.Model):
    _name = "prema.dispatch.pallet.stop.allocation"
    _description = "Pallet-to-Stop Allocation (shared skid support)"
    _order = "unload_sequence, id"

    dispatch_item_id = fields.Many2one("prema.dispatch.item", required=True, ondelete="cascade", index=True)
    stop_id = fields.Many2one("prema.dispatch.stop", required=True, ondelete="cascade", index=True)
    job_id = fields.Many2one("prema.dispatch.job", related="stop_id.job_id", store=True, index=True)
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", related="dispatch_item_id.load_plan_id", store=True, index=True)
    invoice_id = fields.Many2one("account.move")
    unload_sequence = fields.Integer(default=10)
    # Portion of a shared pallet delivered at THIS stop — mirrors the
    # canonical booking allocation (logistics.booking.pallet.stop.allocation)
    # 1:1 by unload_sequence so the reconciliation trace
    # booking pallet → dispatch item → dispatch allocation → weight holds.
    weight_lbs = fields.Float(string="Portion Weight (lbs)", digits=(10, 1))
    piece_count = fields.Integer(string="Pieces")
    notes = fields.Text()
    delivered = fields.Boolean(default=False)
    delivered_at = fields.Datetime()
    delivered_by = fields.Many2one("res.users")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("item_stop_unique", "unique(dispatch_item_id, stop_id)",
         "This pallet already has an allocation for this stop."),
    ]

    def create(self, vals_list):
        allocs = super().create(vals_list)
        # Spec §34: a pallet assigned to a stop propagates to the timeline
        # (Booking Board + customer tracking both read it).
        for alloc in allocs:
            if alloc.job_id and alloc.active:
                alloc.job_id._post_timeline(
                    alloc.job_id, "pallet_assigned",
                    notes=f"{alloc.dispatch_item_id.name} assigned to "
                          f"{alloc.stop_id.address or alloc.stop_id.stop_type}",
                    stop=alloc.stop_id,
                )
        return allocs

    @api.constrains("dispatch_item_id", "stop_id", "active")
    def _check_allocation_rules(self):
        for alloc in self.filtered("active"):
            item = alloc.dispatch_item_id
            stop = alloc.stop_id
            if stop.status == "cancelled":
                raise ValidationError("Cancelled stops cannot be allocated to a pallet.")
            if item.job_id.id != stop.job_id.id:
                raise ValidationError("A pallet can only be allocated to stops on its own job.")
            active_allocs = item.stop_allocation_ids.filtered("active")
            if len(active_allocs) > 5:
                raise ValidationError("A pallet can be allocated to at most five active stops.")

    def action_mark_delivered(self):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        for alloc in self:
            check_stop_access(self.env, alloc.stop_id)
            alloc.write({
                "delivered": True, "delivered_at": fields.Datetime.now(),
                "delivered_by": self.env.user.id,
            })

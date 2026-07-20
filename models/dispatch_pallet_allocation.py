from odoo import api, fields, models


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
    notes = fields.Text()
    delivered = fields.Boolean(default=False)
    delivered_at = fields.Datetime()
    delivered_by = fields.Many2one("res.users")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("item_stop_unique", "unique(dispatch_item_id, stop_id)",
         "This pallet already has an allocation for this stop."),
    ]

    def action_mark_delivered(self):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        for alloc in self:
            check_stop_access(self.env, alloc.stop_id)
            alloc.write({
                "delivered": True, "delivered_at": fields.Datetime.now(),
                "delivered_by": self.env.user.id,
            })

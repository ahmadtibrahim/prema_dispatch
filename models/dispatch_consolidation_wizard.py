from odoo import fields, models


class DispatchConsolidationSuggestionLine(models.TransientModel):
    """One read-only row in the consolidation preview — mirrors the Stops
    list columns dispatchers already know from the job form."""
    _name = "prema.dispatch.consolidation.line"
    _description = "Suggested Consolidated Stop"
    _order = "sequence"

    wizard_id = fields.Many2one("prema.dispatch.consolidation.wizard", ondelete="cascade")
    sequence = fields.Integer()
    job_id = fields.Many2one("prema.dispatch.job", readonly=True)
    stop_id = fields.Many2one(
        "prema.dispatch.stop", readonly=True,
        help="Blank means this line is a proposed cross-dock leg — accepting "
             "the route creates a new stop for it (see cross_dock_type).",
    )
    stop_type = fields.Selection([
        ("pickup", "Pickup"), ("dropoff", "Drop-Off"),
        ("return", "Return"), ("transfer", "Driver Transfer"),
        ("cross_dock_drop", "Cross-Dock Drop / Transfer-In"),
        ("cross_dock_pickup", "Cross-Dock Pickup / Transfer-Out"),
        ("other", "Other"),
    ], readonly=True)
    address = fields.Char(readonly=True)
    pallets_in = fields.Integer(readonly=True)
    pallets_out = fields.Integer(readonly=True)
    eta = fields.Datetime(readonly=True)
    cross_dock_type = fields.Selection(
        [("drop", "Drop"), ("pickup", "Reload")], readonly=True,
    )
    location_id = fields.Many2one("prema.dispatch.location", readonly=True)
    pallets = fields.Integer(readonly=True)
    origin_stop_name = fields.Char(readonly=True)
    origin_stop_id = fields.Many2one("prema.dispatch.stop", readonly=True)


class DispatchConsolidationWizard(models.TransientModel):
    """Shown when a dispatcher asks to consolidate two or more jobs sharing
    one truck/day into a single sensible route — a dispatcher-confirmed
    alternative to optimize_route(), which only ever reorders stops within
    one job at a time (see suggest_consolidated_route() for why)."""
    _name = "prema.dispatch.consolidation.wizard"
    _description = "Suggested Consolidated Route"

    vehicle_id = fields.Many2one("fleet.vehicle", required=True, readonly=True)
    date = fields.Date(required=True, readonly=True)
    line_ids = fields.One2many(
        "prema.dispatch.consolidation.line", "wizard_id",
        string="Suggested Order", readonly=True,
    )

    def action_accept(self):
        """Apply the suggested order: renumber each job's stop sequence to
        match its position in the merged order, and set the computed ETA on
        each stop so the Planner/Live Map (which sort by scheduled_time)
        display the consolidated route correctly. Lines with no stop_id are
        proposed cross-dock legs — accepting creates the real stop."""
        self.ensure_one()
        Stop = self.env["prema.dispatch.stop"]
        cross_dock_legs = 0
        for i, line in enumerate(self.line_ids.sorted("sequence")):
            seq = (i + 1) * 10
            if line.stop_id:
                line.stop_id.write({"sequence": seq, "scheduled_time": line.eta})
                continue
            base_domain = [
                ("job_id", "=", line.job_id.id),
                ("stop_type", "=", line.stop_type),
                ("saved_location_id", "=", line.location_id.id),
                ("status", "not in", ("cancelled",)),
            ]
            origin_stop_id = line.origin_stop_id.id or False
            existing = Stop.search(
                base_domain + [("cross_dock_origin_stop_id", "=", origin_stop_id)],
                limit=1,
            )
            if not existing and origin_stop_id:
                existing = Stop.search(
                    base_domain + [("cross_dock_origin_stop_id", "=", False)],
                    limit=1,
                )
            if existing:
                existing.write({
                    "sequence": seq,
                    "scheduled_time": line.eta,
                    "service_time_minutes": 10,
                    "pod_required": True,
                    "cross_dock_origin_stop_id": origin_stop_id or existing.cross_dock_origin_stop_id.id or False,
                })
                continue
            cross_dock_legs += 1
            is_drop = line.cross_dock_type == "drop"
            Stop.create({
                "job_id": line.job_id.id,
                "sequence": seq,
                "stop_type": line.stop_type,
                "address": line.address,
                "saved_location_id": line.location_id.id,
                "scheduled_time": line.eta,
                "pallets_in": line.pallets_in,
                "pallets_out": line.pallets_out,
                "pod_required": True,
                "service_time_minutes": 10,
                "cross_dock_origin_stop_id": origin_stop_id,
                "dispatcher_notes": (
                    f"Temporarily hold freight from {line.origin_stop_name} here"
                    if is_drop else
                    f"Reload freight held for {line.origin_stop_name}"
                ),
            })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Route Consolidated",
                "message": f"{len(self.line_ids)} stops reordered across "
                           f"{len(self.line_ids.mapped('job_id'))} job(s)."
                           + (f" {cross_dock_legs} cross-dock leg(s) created." if cross_dock_legs else ""),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

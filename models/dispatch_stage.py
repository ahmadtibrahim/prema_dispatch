from odoo import fields, models


class PremaDispatchStage(models.Model):
    _name = "prema.dispatch.stage"
    _description = "Dispatch Stage"
    _order = "sequence asc, id asc"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        help="Legacy internal code — kept for reference only. Use stage_type for logic."
    )
    stage_type = fields.Selection([
        ("draft",      "Draft"),
        ("booking",    "Booking"),
        ("dispatched", "Dispatched"),
        ("completed",  "Completed"),
        ("cancelled",  "Cancelled"),
        ("exception",  "Exception"),
    ], string="Stage Type",
        help="System category used by automation. Do not change on seeded stages.",
    )
    is_booking_phase = fields.Boolean(
        string="Booking Phase",
        help="Jobs in this stage are in the planning/booking phase, not yet dispatched.",
    )
    is_dispatched = fields.Boolean(
        string="Dispatched",
        help="Jobs in this stage have been dispatched to a driver and are in motion.",
    )
    sequence = fields.Integer(default=10)
    fold = fields.Boolean(
        string="Folded in Kanban",
        help="Fold this column by default on the dispatch board.",
    )
    is_completed = fields.Boolean(
        string="Completed Stage",
        help="Jobs in this stage are considered fully completed.",
    )
    is_cancelled = fields.Boolean(
        string="Cancelled Stage",
        help="Jobs in this stage are considered cancelled.",
    )
    active = fields.Boolean(default=True)

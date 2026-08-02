from odoo import fields, models

ADJUSTMENT_TYPE_SELECTION = [("pickup", "Pickup"), ("delivery", "Delivery")]
CALC_TYPE_SELECTION = [("flat", "Flat Amount"), ("percentage", "Percentage of Subtotal")]


class LogisticsFsaRateAdjustment(models.Model):
    """FSA pricing adjustment, versioned by riding on rate_plan_id's own
    version/effective window instead of carrying independent dates — this
    is deliberate: it prevents FSA pricing from ever drifting onto a
    different version timeline than the base lane rate it modifies.
    """

    _name = "logistics.fsa.rate.adjustment"
    _description = "Per-FSA pickup/delivery pricing adjustment for one rate plan version"
    _order = "rate_plan_id, fsa_id"

    fsa_id = fields.Many2one("logistics.fsa", required=True, index=True)
    rate_plan_id = fields.Many2one("logistics.rate.plan", required=True, index=True, ondelete="cascade")
    adjustment_type = fields.Selection(ADJUSTMENT_TYPE_SELECTION, required=True)
    calc_type = fields.Selection(CALC_TYPE_SELECTION, required=True, default="flat")
    amount = fields.Float(
        required=True, default=0.0,
        help="Dollar amount if Calc Type = Flat; percentage points (e.g. 5 = 5%) if Percentage. "
             "Combined pickup+delivery PERCENTAGE adjustments are capped at 20% total by the "
             "pricing engine regardless of what's entered here.",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("fsa_plan_type_uniq", "unique(fsa_id, rate_plan_id, adjustment_type)",
         "An adjustment for this FSA/rate-plan/type already exists."),
    ]

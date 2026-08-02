from odoo import fields, models

CALC_TYPE_SELECTION = [
    ("flat", "Flat Amount"),
    ("percent", "Percent of Subtotal"),
    ("per_pallet", "Per Pallet"),
    ("per_lb", "Per Pound"),
]


class LogisticsSurchargeType(models.Model):
    _name = "logistics.surcharge.type"
    _description = "Reusable surcharge catalog (reefer, liftgate, appointment, remote, etc.)"
    _order = "name"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    calc_type = fields.Selection(CALC_TYPE_SELECTION, required=True, default="flat")
    default_amount = fields.Float(default=0.0)
    active = fields.Boolean(default=True)
    is_global = fields.Boolean(
        help="Applies automatically to EVERY rate plan (subject to the same "
             "conditional-code gating in the pricing engine) without needing "
             "a per-plan logistics.rate.plan.surcharge row. Use this for "
             "network-wide accessorial policies (liftgate, appointment, "
             "temperature premiums, etc.) rather than creating one "
             "assignment row per rate plan. A rate plan can still override "
             "the amount for itself via an explicit rate.plan.surcharge row "
             "with the same surcharge_type_id.",
    )

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Surcharge code must be unique."),
    ]

    def compute_amount(self, subtotal, pallets, weight_lbs):
        self.ensure_one()
        if self.calc_type == "percent":
            return subtotal * (self.default_amount / 100.0)
        if self.calc_type == "per_pallet":
            return self.default_amount * pallets
        if self.calc_type == "per_lb":
            return self.default_amount * weight_lbs
        return self.default_amount


class LogisticsRatePlanSurcharge(models.Model):
    _name = "logistics.rate.plan.surcharge"
    _description = "Which surcharges apply to a given rate plan, with optional amount override"
    _order = "rate_plan_id, surcharge_type_id"

    rate_plan_id = fields.Many2one("logistics.rate.plan", required=True, index=True, ondelete="cascade")
    surcharge_type_id = fields.Many2one("logistics.surcharge.type", required=True)
    amount_override = fields.Float(help="Leave 0 to use the surcharge type's default amount.")

    _sql_constraints = [
        ("plan_surcharge_uniq", "unique(rate_plan_id, surcharge_type_id)",
         "This surcharge is already assigned to this rate plan."),
    ]

    def effective_amount(self, subtotal, pallets, weight_lbs):
        self.ensure_one()
        if self.amount_override:
            st = self.surcharge_type_id
            if st.calc_type == "percent":
                return subtotal * (self.amount_override / 100.0)
            if st.calc_type == "per_pallet":
                return self.amount_override * pallets
            if st.calc_type == "per_lb":
                return self.amount_override * weight_lbs
            return self.amount_override
        return self.surcharge_type_id.compute_amount(subtotal, pallets, weight_lbs)

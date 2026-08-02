from odoo import api, fields, models

TIER_TYPE_SELECTION = [("pallet", "Pallet Count"), ("weight", "Weight (lbs)")]
CALC_METHOD_SELECTION = [("flat", "Flat Amount"), ("per_unit", "Per Unit x Quantity")]


class LogisticsRateTier(models.Model):
    """Quantity pricing row on a rate plan — one row per pallet quantity.

    Scheduled Shared LTL model:
      - Each pallet quantity (1–13) has its own editable row.
      - Owner edits the ``rate`` field (effective per-unit price).
      - Base, Discount, Dry Total, and Average are computed for display.

    The pricing engine uses ``amount_for_qty()`` which reads ``rate``.
    """

    _name = "logistics.rate.tier"
    _description = "Quantity pricing row on a rate plan"
    _order = "rate_plan_id, tier_type, min_qty"

    rate_plan_id = fields.Many2one(
        "logistics.rate.plan", required=True, index=True, ondelete="cascade",
    )
    tier_type = fields.Selection(TIER_TYPE_SELECTION, required=True)
    min_qty = fields.Float(required=True, string="Quantity")
    max_qty = fields.Float(
        help="Same as Quantity for individual rows. Use ranges for weight tiers.",
    )
    calc_method = fields.Selection(
        CALC_METHOD_SELECTION, required=True, default="per_unit",
    )
    rate = fields.Float(
        required=True, default=0.0, string="Rate",
        help="Effective per-unit dry rate. Edit this to control the price "
             "for this quantity. Dry Total = Rate x Quantity.",
    )
    cap_amount = fields.Float(
        help="Optional ceiling on this tier's total (per_unit only).",
    )

    # ── Display fields (computed, no DB column) ───────────────────────
    base_amount = fields.Float(
        compute="_compute_display", string="Base",
        help="Target Revenue/Pallet x Quantity.",
    )
    discount_amount = fields.Float(
        compute="_compute_display", string="Discount",
        help="Base - Dry Total. The effective dollar discount.",
    )
    dry_total = fields.Float(
        compute="_compute_display", string="Dry Total",
        help="Rate x Quantity. The final dry price.",
    )
    avg_per_pallet = fields.Float(
        compute="_compute_display", string="Average",
        help="Dry Total / Quantity (same as Rate for per_unit).",
    )

    @api.depends("min_qty", "rate", "calc_method", "cap_amount",
                 "rate_plan_id.customer_price_per_pallet")
    def _compute_display(self):
        for rec in self:
            qty = max(rec.min_qty, 1)
            target_per = rec.rate_plan_id.customer_price_per_pallet or 0.0
            rec.base_amount = round(target_per * qty, 2)
            rec.dry_total = rec.amount_for_qty(qty)
            rec.discount_amount = round(max(rec.base_amount - rec.dry_total, 0), 2)
            rec.avg_per_pallet = round(rec.dry_total / qty, 2)

    def amount_for_qty(self, qty):
        """Return the dry price for a given quantity."""
        self.ensure_one()
        if self.calc_method == "per_unit":
            amount = self.rate * qty
            if self.cap_amount and self.cap_amount > 0:
                amount = min(amount, self.cap_amount)
            return amount
        return self.rate

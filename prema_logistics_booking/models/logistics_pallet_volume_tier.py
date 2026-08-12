"""Pallet volume discount tiers for corridor Customer Pricing."""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class LogisticsPalletVolumeTier(models.Model):
    _name = "logistics.pallet.volume.tier"
    _description = "Pallet Volume Discount Tier"

    corridor_id = fields.Many2one(
        "logistics.corridor", string="Corridor", required=True, ondelete="cascade",
    )
    min_pallets = fields.Integer(string="Min Pallets", required=True, default=2)
    max_pallets = fields.Integer(string="Max Pallets", required=True, default=3)
    discount_pct = fields.Float(string="Discount %", default=0.0)
    pricing_type = fields.Selection([
        ("ltl", "LTL"),
        ("ftl", "FTL"),
    ], string="Pricing Type", default="ltl", required=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("check_min_max",
         "CHECK(min_pallets >= 1 AND max_pallets >= min_pallets)",
         "Min Pallets must be >= 1 and Max Pallets must be >= Min Pallets."),
    ]

    @api.constrains("min_pallets", "max_pallets", "active", "corridor_id")
    def _check_no_overlapping_ranges(self):
        for tier in self:
            if not tier.active:
                continue
            overlapping = self.search([
                ("id", "!=", tier.id),
                ("corridor_id", "=", tier.corridor_id.id),
                ("active", "=", True),
                ("pricing_type", "=", tier.pricing_type),
                ("min_pallets", "<=", tier.max_pallets),
                ("max_pallets", ">=", tier.min_pallets),
            ])
            if overlapping:
                raise ValidationError(_(
                    "Pallet range %(min)s–%(max)s overlaps with existing "
                    "active tier(s): %(tiers)s"
                ) % {
                    "min": tier.min_pallets, "max": tier.max_pallets,
                    "tiers": ", ".join(
                        f"{t.min_pallets}–{t.max_pallets}" for t in overlapping
                    ),
                })

    @api.model
    def get_discount_for_pallets(self, corridor_id, physical_pallets):
        if physical_pallets < 2:
            return 0.0
        tier = self.search([
            ("corridor_id", "=", corridor_id),
            ("active", "=", True),
            ("pricing_type", "=", "ltl"),
            ("min_pallets", "<=", physical_pallets),
            ("max_pallets", ">=", physical_pallets),
        ], limit=1)
        return tier.discount_pct if tier else 0.0

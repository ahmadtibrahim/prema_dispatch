"""FTL Regional Pricing — per-pair pricing rules in corridor FTL pricing.

Each active Origin Region → Destination Region rule chooses its pricing
mode:

    corridor_default → segment distance × corridor-level FTL $ / km
    flat_rate        → the rule's Flat Rate (distance-independent)
    per_km           → segment distance × the rule's FTL $ / km

The retired "Minimum FTL Charge" field (regional and corridor-wide) no
longer participates in any pricing calculation. Kept only as legacy
database columns for migration compatibility.

The model technical name (logistics.ftl.regional.minimum) is intentionally
unchanged to avoid migration risk; only user-facing labels say
"FTL Regional Pricing".
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

REGION_DOMAIN = [
    ("is_official_ltl_region", "=", True),
    ("active", "=", True),
]

# Hard guarantee that a corridor can never hold two ACTIVE rules for the
# same origin→destination pair (regardless of pricing type). Odoo
# _sql_constraints cannot express a UNIQUE constraint filtered to active
# rows, so this is a raw partial index. Kept in _auto_init so it exists on
# both fresh installs and upgrades.
_ACTIVE_PAIR_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS
    logistics_ftl_regional_minimum_active_pair_uniq
ON logistics_ftl_regional_minimum
    (corridor_id, origin_region_id, destination_region_id)
WHERE active
"""


class LogisticsFtlRegionalMinimum(models.Model):
    _name = "logistics.ftl.regional.minimum"
    _description = "FTL Regional Pricing Rule"
    _order = "sequence, id"

    sequence = fields.Integer(
        string="Sequence", default=10, index=True,
        help="Manual display order inside the corridor's FTL Regional "
             "Pricing list. Cosmetic only — never used in pricing or rule "
             "matching.",
    )
    corridor_id = fields.Many2one(
        "logistics.corridor", string="Corridor",
        required=True, ondelete="cascade", index=True,
    )
    origin_region_id = fields.Many2one(
        "logistics.region", string="Origin Region",
        required=True, index=True, domain=REGION_DOMAIN,
        help="Approved origin region for this FTL regional pricing rule.",
    )
    destination_region_id = fields.Many2one(
        "logistics.region", string="Destination Region",
        required=True, index=True, domain=REGION_DOMAIN,
        help="Approved destination region for this FTL regional pricing rule.",
    )
    pricing_type = fields.Selection([
        ("corridor_default", "Corridor Default"),
        ("flat_rate", "Flat Rate"),
        ("per_km", "Per KM"),
    ], string="Pricing Type", required=True, default="corridor_default")
    flat_rate = fields.Monetary(
        string="Flat Rate",
        help="Final FTL price for this pair when Pricing Type = Flat Rate. "
             "Must be greater than zero; ignored for other pricing types.",
    )
    ftl_rate_per_km_override = fields.Float(
        string="FTL $ / km",
        help="Regional FTL rate per km for this pair. Used only when "
             "Pricing Type = Per KM and must then be greater than zero.",
    )
    # FTL multi-stop surcharges (per-rule config). Applied ONLY to FTL
    # movements priced through this rule's origin → destination pair;
    # never read by any LTL calculation.
    same_region_additional_stop_charge = fields.Monetary(
        string="Same-Region Stop $",
        default=50.0,
        help="Charge for each additional delivery facility within a region "
             "already being served by this FTL movement.",
    )
    regional_additional_stop_charge = fields.Monetary(
        string="Regional Stop $",
        default=75.0,
        help="Charge for the first delivery stop in an additional en-route "
             "region before the final destination.",
    )
    # LEGACY — retired from pricing and UI. Kept as a database column for
    # migration compatibility with any rule created before FTL Regional
    # Pricing; never read by any pricing calculation.
    minimum_ftl_charge = fields.Monetary(string="Minimum FTL Charge")
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    active = fields.Boolean(default=True)

    def _auto_init(self):
        super()._auto_init()
        self.env.cr.execute(_ACTIVE_PAIR_UNIQUE_INDEX_SQL)

    @api.constrains("flat_rate", "ftl_rate_per_km_override",
                    "same_region_additional_stop_charge",
                    "regional_additional_stop_charge")
    def _check_non_negative_values(self):
        for rule in self:
            if rule.flat_rate < 0:
                raise ValidationError(_("Flat Rate cannot be negative."))
            if rule.ftl_rate_per_km_override < 0:
                raise ValidationError(_("FTL $ / km cannot be negative."))
            if rule.same_region_additional_stop_charge < 0:
                raise ValidationError(_("Same-Region Stop $ cannot be negative."))
            if rule.regional_additional_stop_charge < 0:
                raise ValidationError(_("Regional Stop $ cannot be negative."))

    @api.constrains("pricing_type", "flat_rate", "ftl_rate_per_km_override")
    def _check_pricing_type_values(self):
        for rule in self:
            if rule.pricing_type == "flat_rate" and rule.flat_rate <= 0:
                raise ValidationError(_(
                    "Flat Rate must be greater than zero when Pricing Type "
                    "is Flat Rate."
                ))
            if rule.pricing_type == "per_km" and rule.ftl_rate_per_km_override <= 0:
                raise ValidationError(_(
                    "FTL $ / km must be greater than zero when Pricing Type "
                    "is Per KM."
                ))

    def _raise_if_duplicate_active_pair(self, corridor_id, origin_region_id,
                                        destination_region_id, exclude_id=None):
        """Pre-write duplicate guard. The partial unique index fires inside
        the INSERT and only surfaces a raw database error, so the friendly
        message must be raised BEFORE the write; the index stays in place
        as the race-proof backstop for concurrent writes."""
        if not (corridor_id and origin_region_id and destination_region_id):
            return
        domain = [
            ("corridor_id", "=", corridor_id),
            ("origin_region_id", "=", origin_region_id),
            ("destination_region_id", "=", destination_region_id),
            ("active", "=", True),
        ]
        if exclude_id:
            domain.append(("id", "!=", exclude_id))
        duplicate = self.search(domain, limit=1)
        if duplicate:
            raise ValidationError(_(
                "An active FTL Regional Pricing rule already exists for "
                "%(origin)s → %(destination)s on corridor %(corridor)s.",
                origin=duplicate.origin_region_id.display_name,
                destination=duplicate.destination_region_id.display_name,
                corridor=duplicate.corridor_id.display_name,
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("active", True):
                self._raise_if_duplicate_active_pair(
                    vals.get("corridor_id"),
                    vals.get("origin_region_id"),
                    vals.get("destination_region_id"),
                )
        return super().create(vals_list)

    def write(self, vals):
        for rule in self:
            if vals.get("active", rule.active):
                self._raise_if_duplicate_active_pair(
                    vals.get("corridor_id") or rule.corridor_id.id,
                    vals.get("origin_region_id") or rule.origin_region_id.id,
                    vals.get("destination_region_id") or rule.destination_region_id.id,
                    exclude_id=rule.id,
                )
        return super().write(vals)

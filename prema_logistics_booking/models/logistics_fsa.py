import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

FSA_RE = re.compile(r"^[A-Z][0-9][A-Z]$")

PROVINCE_SELECTION = [
    ("ON", "Ontario"),
    ("QC", "Quebec"),
    ("NS", "Nova Scotia"),
    ("NB", "New Brunswick"),
    ("MB", "Manitoba"),
    ("BC", "British Columbia"),
    ("PE", "Prince Edward Island"),
    ("SK", "Saskatchewan"),
    ("AB", "Alberta"),
    ("NL", "Newfoundland and Labrador"),
    ("NT", "Northwest Territories"),
    ("YT", "Yukon"),
    ("NU", "Nunavut"),
]


class LogisticsFsa(models.Model):
    _name = "logistics.fsa"
    _description = "Canadian Forward Sortation Area (postal pricing geography)"
    _order = "fsa"
    _rec_name = "fsa"

    fsa = fields.Char(string="FSA", required=True, size=3, index=True)
    province = fields.Selection(PROVINCE_SELECTION)
    display_city = fields.Char(help="Customer-facing city/area name shown instead of the internal region code.")
    region_id = fields.Many2one("logistics.region", string="Logistics Region", index=True)
    zone_id = fields.Many2one(
        "logistics.fsa.zone", string="Delivery Zone",
        help="Not assigned until the authoritative FSA dataset is loaded -- see CLAUDE.md.",
    )
    active = fields.Boolean(default=True)
    remote = fields.Boolean(help="Flag for surcharge/serviceability purposes later.")
    pickup_supported = fields.Boolean(default=True)
    delivery_supported = fields.Boolean(default=True)

    _sql_constraints = [
        ("fsa_uniq", "unique(fsa)", "This FSA already exists."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("fsa"):
                vals["fsa"] = vals["fsa"].strip().upper()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("fsa"):
            vals["fsa"] = vals["fsa"].strip().upper()
        return super().write(vals)

    @api.model
    def resolve_from_postal(self, postal_code):
        """Normalize a full Canadian postal code (or bare FSA) and look up
        the matching logistics.fsa record. Returns an empty recordset if the
        format is invalid or the FSA isn't in our (currently unpopulated)
        table -- callers must handle both cases explicitly, never assume a
        match."""
        if not postal_code:
            return self.browse()
        cleaned = postal_code.strip().upper().replace(" ", "").replace("-", "")
        candidate = cleaned[:3]
        if not FSA_RE.match(candidate):
            return self.browse()
        return self.search([("fsa", "=", candidate)], limit=1)

    @api.constrains("fsa")
    def _check_fsa_format(self):
        for rec in self:
            if not rec.fsa or not FSA_RE.match(rec.fsa):
                raise ValidationError(
                    _("'%s' is not a valid Canadian FSA. Expected format: one letter, "
                      "one digit, one letter (e.g. L5M, K1G, H3B).") % (rec.fsa or "",)
                )

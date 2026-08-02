"""City Directory — friendly display names mapped to FSAs and Regions.

Customer sees "Lindsay" → system resolves to Region via FSA.
Pricing stays at Region level. This model is display-only.
"""
from odoo import api, fields, models


class LogisticsCity(models.Model):
    _name = "logistics.city"
    _description = "City (display-only, pricing stays at Region level)"
    _order = "name"
    _sql_constraints = [
        ("name_province_uniq", "unique(name, province_state)", "A city with this name already exists in this province."),
    ]

    name = fields.Char(string="City", required=True, index=True)
    province_state = fields.Selection([
        ("ON", "Ontario"), ("QC", "Quebec"), ("BC", "British Columbia"),
        ("AB", "Alberta"), ("SK", "Saskatchewan"), ("MB", "Manitoba"),
        ("NB", "New Brunswick"), ("NS", "Nova Scotia"), ("PE", "Prince Edward Island"),
        ("NL", "Newfoundland and Labrador"), ("YT", "Yukon"), ("NT", "Northwest Territories"),
        ("NU", "Nunavut"),
    ], string="Province", required=True, default="ON")
    region_id = fields.Many2one(
        "logistics.region", string="Region", required=True, index=True,
        help="The pricing/dispatch region this city belongs to."
    )
    primary_fsa_id = fields.Many2one(
        "logistics.fsa", string="Primary FSA",
        help="The primary FSA for this city (optional — used for display/geography)."
    )
    latitude = fields.Float(string="Latitude", digits=(10, 6))
    longitude = fields.Float(string="Longitude", digits=(10, 6))
    google_place_id = fields.Char(string="Google Place ID")
    active = fields.Boolean(default=True)

    def name_get(self):
        return [(r.id, f"{r.name}, {r.province_state}") for r in self]

from odoo import fields, models


class LogisticsRegion(models.Model):
    _name = "logistics.region"
    _description = "Internal Logistics Pricing/Dispatch Region (R1-R10)"
    _order = "code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    hub_name = fields.Char(string="Hub")

    # ── Phase 10: Suggested Pricing ───────────────────────────────────
    rate_per_km = fields.Float(
        string="Rate per km",
        default=3.00,
        help="Regional rate per km for auto-suggesting revenue targets. "
             "Default: West 3.00, East 2.80, Northern 3.00 or higher."
    )

    phase = fields.Integer(default=1)
    
    # V4 Map fields
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    display_number = fields.Integer(string="Display Number", help="Number shown on map marker")
    main_city = fields.Char(string="Main City")
    marker_latitude = fields.Float(string="Marker Latitude", digits=(10,6))
    marker_longitude = fields.Float(string="Marker Longitude", digits=(10,6))
    polygon_geojson = fields.Text(string="Polygon GeoJSON")
    public_description = fields.Text(string="Public Description")
    default_hub_id = fields.Many2one('logistics.hub', string='Default Hub')

    active = fields.Boolean(default=True)
    map_color = fields.Char(string="Map Color", help="Hex color for admin map visualization only.")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Region code must be unique."),
    ]

    def name_get(self):
        return [(r.id, f"{r.code} - {r.name}") for r in self]

from odoo import fields, models


class LogisticsRegion(models.Model):
    _name = "logistics.region"
    _description = "Service Region — pricing, routing, and map authority"
    _order = "display_sequence, code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    display_number = fields.Integer(string="Display Number", help="Number on map marker")
    main_city = fields.Char(string="Main City")
    hub_name = fields.Char(string="Hub")

    rate_per_km = fields.Float(string="Rate per km", default=3.00)

    # ── Map fields ────────────────────────────────────────────────────
    marker_latitude = fields.Float(string="Marker Latitude", digits=(10, 6))
    marker_longitude = fields.Float(string="Marker Longitude", digits=(10, 6))
    polygon_geojson = fields.Text(string="Polygon GeoJSON")
    public_description = fields.Text(string="Public Description")
    default_hub_id = fields.Many2one("logistics.hub", string="Default Hub")

    # ── Status ────────────────────────────────────────────────────────
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    phase = fields.Integer(default=1)
    display_sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    map_color = fields.Char(string="Map Color")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Region code must be unique."),
    ]

    def name_get(self):
        return [(r.id, f"{r.code} - {r.name}") for r in self]

from odoo import api, fields, models


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

    
    @api.model
    def get_map_data(self):
        """RPC: Return all Where We Go map data."""
        Region = self.sudo()
        Lane = self.env["logistics.lane"].sudo()
        Hub = self.env["logistics.hub"].sudo()
        Corridor = self.env["logistics.corridor"].sudo()
        Departure = self.env["logistics.corridor.departure"].sudo()
        from datetime import date

        regions = []
        for r in Region.search([("active", "=", True), ("customer_visible", "=", True)]):
            regions.append({
                "id": r.id, "code": r.code, "name": r.name,
                "display_number": r.display_number or r.id,
                "main_city": r.main_city or "",
                "lat": r.marker_latitude or 44.0,
                "lng": r.marker_longitude or -78.0,
                "polygon": r.polygon_geojson or "",
            })

        hubs = []
        for h in Hub.search([("active", "=", True)]):
            hubs.append({
                "id": h.id, "name": h.name, "public_name": h.public_name,
                "lat": h.latitude or 43.649, "lng": h.longitude or -79.659,
                "is_default": h.is_default,
            })

        lanes = []
        for l in Lane.search([("active", "=", True)]):
            lanes.append({
                "id": l.id, "origin_id": l.origin_region_id.id,
                "dest_id": l.destination_region_id.id,
                "direct_allowed": l.direct_allowed,
                "via_hub_allowed": l.via_hub_allowed,
                "road_km": l.road_km or 0,
            })

        services = []
        for c in Corridor.search([("active", "=", True)]):
            stops = []
            for s in c.stop_ids.sorted("sequence"):
                stops.append({
                    "region_id": s.region_id.id if s.region_id else None,
                    "sequence": s.sequence,
                })
            services.append({
                "id": c.id, "name": c.name, "weekday": c.weekday or "",
                "direction": c.direction, "stops": stops,
            })

        today = date.today()
        deps = []
        for d in Departure.search([
            ("departure_date", ">=", today),
            ("departure_date", "<=", today + date.resolution * 14),
            ("active", "=", True), ("status", "=", "scheduled"),
        ], order="departure_date", limit=60):
            deps.append({
                "id": d.id, "date": str(d.departure_date),
                "corridor_id": d.corridor_id.id,
                "vehicle": d.vehicle_id.name or "",
            })

        return {"regions": regions, "hubs": hubs, "lanes": lanes,
                "services": services, "departures": deps}

    def name_get(self):
        return [(r.id, f"{r.code} - {r.name}") for r in self]

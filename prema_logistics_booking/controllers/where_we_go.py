"""Where We Go — Interactive service map. Data served as public JSON."""
import json
from odoo import http
from odoo.http import request



    @http.route("/logistics/where-we-go", type="http", auth="user", website=False)
    def where_we_go_page(self, **kwargs):
        return request.render("prema_logistics_booking.where_we_go_page", {})

class WhereWeGoMap(http.Controller):

    @http.route("/logistics/where-we-go/data", type="http", auth="public", methods=["GET"], csrf=False)
    def where_we_go_data(self, **kwargs):
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Corridor = request.env["logistics.corridor"].sudo()
        Departure = request.env["logistics.corridor.departure"].sudo()
        from datetime import date

        regions = []
        for r in Region.search([("active", "=", True), ("customer_visible", "=", True)]):
            regions.append({
                "id": r.id, "code": r.code, "name": r.name,
                "display_number": r.display_number or r.id,
                "main_city": r.main_city or "",
                "lat": r.marker_latitude or 44.0, "lng": r.marker_longitude or -78.0,
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
            })

        data = {"regions": regions, "hubs": hubs, "lanes": lanes,
                "services": services, "departures": deps}
        return request.make_response(json.dumps(data), [("Content-Type", "application/json")])

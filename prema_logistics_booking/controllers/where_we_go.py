"""Where We Go — Interactive service coverage map."""
from odoo import http
from odoo.http import request


class WhereWeGoMap(http.Controller):

    @http.route("/logistics/where-we-go", type="http", auth="user", website=False)
    def where_we_go(self, **kwargs):
        """Render the interactive Where We Go map page."""
        return request.render("prema_logistics_booking.where_we_go_map", {})

    @http.route("/logistics/where-we-go/data", type="json", auth="user")
    def where_we_go_data(self, **kwargs):
        """JSON endpoint returning Regions, Hubs, Lanes, and Weekly Services."""
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Corridor = request.env["logistics.corridor"].sudo()

        regions = []
        for r in Region.search([("customer_visible", "=", True), ("active", "=", True)]):
            regions.append({
                "id": r.id, "code": r.code, "name": r.name,
                "display_number": r.display_number or r.id,
                "main_city": r.main_city or "",
                "lat": r.marker_latitude or r.main_city_latitude or 0,
                "lng": r.marker_longitude or r.main_city_longitude or 0,
                "polygon": r.polygon_geojson or "",
                "public_description": r.public_description or "",
            })

        hubs = []
        for h in Hub.search([("customer_visible", "=", True), ("active", "=", True)]):
            hubs.append({
                "id": h.id, "name": h.public_name,
                "lat": h.latitude or 0, "lng": h.longitude or 0,
                "is_default": h.is_default,
            })

        lanes = []
        for l in Lane.search([("customer_visible", "=", True), ("active", "=", True)]):
            lanes.append({
                "id": l.id, "name": l.name,
                "origin_id": l.origin_region_id.id,
                "dest_id": l.destination_region_id.id,
                "direct_allowed": l.direct_allowed or False,
                "via_hub_allowed": l.via_hub_allowed or False,
                "road_km": l.road_km or 0,
            })

        services = []
        for c in Corridor.search([("customer_visible", "=", True), ("active", "=", True)]):
            stops = []
            for s in c.stop_ids.sorted("sequence"):
                stops.append({
                    "region_id": s.region_id.id if s.region_id else None,
                    "name": s.region_id.name if s.region_id else s.name,
                    "sequence": s.sequence,
                })
            services.append({
                "id": c.id, "name": c.name, "weekday": c.weekday,
                "direction": c.direction, "stops": stops,
            })

        return {"regions": regions, "hubs": hubs, "lanes": lanes, "services": services}

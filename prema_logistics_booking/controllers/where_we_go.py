"""Where We Go — Interactive service coverage and route-planning map."""
import json
from odoo import http
from odoo.http import request


class WhereWeGoMap(http.Controller):

    @http.route("/logistics/where-we-go", type="http", auth="user", website=False)
    def where_we_go_page(self, **kwargs):
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return request.render("prema_logistics_booking.where_we_go_map", {
            "google_api_key": api_key,
        })

    @http.route("/logistics/where-we-go/data", type="json", auth="user")
    def where_we_go_data(self, **kwargs):
        """Return all map data: regions, hubs, lanes, services, departures."""
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Corridor = request.env["logistics.corridor"].sudo()
        Departure = request.env["logistics.corridor.departure"].sudo()
        Fsa = request.env["logistics.fsa"].sudo()
        from datetime import date

        regions = []
        for r in Region.search([("active", "=", True)]):
            regions.append({
                "id": r.id, "code": r.code, "name": r.name,
                "display_number": r.display_number or r.id,
                "main_city": r.main_city or r.name,
                "lat": r.marker_latitude or 44.0,
                "lng": r.marker_longitude or -78.0,
                "polygon": r.polygon_geojson or "",
                "public_description": r.public_description or "",
                "customer_visible": r.customer_visible,
            })

        hubs = []
        for h in Hub.search([("active", "=", True)]):
            hubs.append({
                "id": h.id, "name": h.name, "public_name": h.public_name,
                "lat": h.latitude or 43.649, "lng": h.longitude or -79.659,
                "is_default": h.is_default, "cross_dock_enabled": h.cross_dock_enabled,
            })

        lanes = []
        for l in Lane.search([("active", "=", True)]):
            lanes.append({
                "id": l.id, "name": l.name,
                "origin_id": l.origin_region_id.id,
                "dest_id": l.destination_region_id.id,
                "direct_allowed": l.direct_allowed if hasattr(l, 'direct_allowed') else True,
                "via_hub_allowed": l.via_hub_allowed if hasattr(l, 'via_hub_allowed') else True,
                "road_km": l.road_km or 0,
                "customer_visible": l.customer_visible if hasattr(l, 'customer_visible') else True,
            })

        services = []
        for c in Corridor.search([("active", "=", True)]):
            stops = []
            for s in c.stop_ids.sorted("sequence"):
                stops.append({
                    "region_id": s.region_id.id if s.region_id else None,
                    "name": s.region_id.name if s.region_id else (s.name or ""),
                    "sequence": s.sequence,
                    "pickup_allowed": s.pickup_allowed if hasattr(s, 'pickup_allowed') else True,
                    "delivery_allowed": s.delivery_allowed if hasattr(s, 'delivery_allowed') else True,
                })
            services.append({
                "id": c.id, "name": c.name, "weekday": c.weekday or "",
                "direction": c.direction, "stops": stops,
                "customer_visible": c.customer_visible if hasattr(c, 'customer_visible') else True,
                "default_vehicle_id": c.default_vehicle_id.id if c.default_vehicle_id else None,
            })

        # Upcoming departures (next 14 days)
        today = date.today()
        departures = []
        for d in Departure.search([
            ("departure_date", ">=", today),
            ("departure_date", "<=", today + date.resolution * 14),
            ("active", "=", True), ("status", "=", "scheduled"),
        ], order="departure_date"):
            departures.append({
                "id": d.id, "date": str(d.departure_date),
                "corridor_id": d.corridor_id.id,
                "corridor_name": d.corridor_id.name or "",
                "vehicle": d.vehicle_id.name or "",
                "status": d.status,
            })

        return {
            "regions": regions, "hubs": hubs, "lanes": lanes,
            "services": services, "departures": departures,
        }

"""Backward-compatible redirects to the single maintained network screens."""

from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden


def _require_dispatch_staff():
    user = request.env.user
    if not user.has_group("prema_dispatch.group_dispatcher") and not user.has_group(
        "prema_dispatch.group_dispatch_manager"
    ):
        raise Forbidden()


class LogisticsNetworkMap(http.Controller):

    @http.route("/logistics/network-map/topology", type="json", auth="user")
    def corridor_topology(self, hub_id=None, **kwargs):
        """Return complete corridor topology for network visualization.
        Hub → ordered regions → Hub. Includes region polygons for overlay."""
        _require_dispatch_staff()
        Corridor = request.env["logistics.corridor"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Region = request.env["logistics.region"].sudo()

        # Resolve hub
        if hub_id:
            hub = Hub.browse(int(hub_id)).exists()
        if not (hub_id and hub):
            hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return {"error": "No hub found"}

        # Build hub payload
        hub_payload = {
            "id": hub.id,
            "name": hub.public_name or hub.name,
            "lat": hub.latitude or (hub.saved_location_id.pin_lat if hub.saved_location_id else 0),
            "lng": hub.longitude or (hub.saved_location_id.pin_lng if hub.saved_location_id else 0),
        }

        # Collect all active corridors that have ordered stops
        all_stops = request.env["logistics.corridor.stop"].sudo().search([
            ("active", "=", True),
        ], order="corridor_id, sequence")
        corridor_ids = all_stops.mapped("corridor_id").ids
        corridors = Corridor.search([("id", "in", corridor_ids), ("active", "=", True)])

        DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        corridor_list = []
        all_region_ids = set()

        for c in corridors:
            c_stops = all_stops.filtered(lambda s, cid=c.id: s.corridor_id.id == cid)
            regions = []
            for s in c_stops:
                r = s.region_id
                all_region_ids.add(r.id)
                regions.append({
                    "region_id": r.id,
                    "code": r.code,
                    "name": r.name,
                    "lat": r.marker_latitude,
                    "lng": r.marker_longitude,
                    "pickup_allowed": s.pickup_allowed,
                    "delivery_allowed": s.delivery_allowed,
                })

            operating_days = [d.capitalize()[:3] for d in DAYS
                            if getattr(c, f"operate_{d}")]

            corridor_list.append({
                "id": c.id,
                "name": c.name,
                "operating_days": operating_days,
                "operating_days_full": [d.capitalize() for d in DAYS
                                       if getattr(c, f"operate_{d}")],
                "regions": regions,
            })

        # Collect region polygons
        all_regions = Region.search([("id", "in", list(all_region_ids))])
        region_polygons = []
        for r in all_regions:
            entry = {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "lat": r.marker_latitude,
                "lng": r.marker_longitude,
                "main_city": r.main_city or "",
            }
            if r.polygon_geojson:
                try:
                    import json
                    entry["geojson"] = json.loads(r.polygon_geojson)
                except Exception:
                    entry["geojson"] = None
            region_polygons.append(entry)

        # Also collect manual-quote regions (have polygons, no active corridor)
        manual_regions = Region.search([
            ("active", "=", True), ("customer_visible", "=", True),
            ("id", "not in", list(all_region_ids)),
        ])
        for r in manual_regions:
            entry = {
                "id": r.id, "code": r.code, "name": r.name,
                "lat": r.marker_latitude, "lng": r.marker_longitude,
                "main_city": r.main_city or "", "manual_quote": True,
            }
            if r.polygon_geojson:
                try:
                    import json
                    entry["geojson"] = json.loads(r.polygon_geojson)
                except Exception:
                    entry["geojson"] = None
            region_polygons.append(entry)

        # Hubs list for selector
        hubs = Hub.search([("active", "=", True)])
        hub_list = [{"id": h.id, "name": h.public_name or h.name} for h in hubs]

        return {
            "hub": hub_payload,
            "corridors": corridor_list,
            "regions": region_polygons,
            "hubs": hub_list,
        }

    @http.route("/logistics/network-map", type="http", auth="user", website=False)
    def network_map(self, **kwargs):
        """Old bookmarks now open the maintained Where We Go client action."""
        del kwargs
        _require_dispatch_staff()
        return request.redirect(
            "/web#action=prema_logistics_booking.action_where_we_go"
        )

    @http.route("/logistics/price-matrix", type="http", auth="user", website=False)
    def price_matrix(self, **kwargs):
        """The duplicate price matrix was retired; Corridors own pricing."""
        del kwargs
        _require_dispatch_staff()
        return request.redirect(
            "/web#action=prema_logistics_booking.action_logistics_corridor"
        )

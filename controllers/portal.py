from datetime import datetime, timezone

from odoo import http
from odoo.http import request


class DispatchTrackingController(http.Controller):

    @http.route("/web/dispatch/live-map/data", type="json", auth="user")
    def live_map_data(self, **kwargs):
        """Return active dispatch jobs with truck GPS and stop coordinates."""
        Job = request.env["prema.dispatch.job"]
        active_jobs = Job.search([
            ("stage_id.stage_type", "not in", ["cancelled", "completed"]),
            ("vehicle_id", "!=", False),
        ], limit=200)

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        trucks = {}
        for job in active_jobs:
            vehicle = job.vehicle_id
            vid = vehicle.id
            if vid not in trucks:
                lat = vehicle.x_last_location_lat or 0.0
                lng = vehicle.x_last_location_lng or 0.0
                gps_at = vehicle.x_last_location_at
                gps_age_min = None
                if gps_at:
                    delta = now_utc - gps_at.replace(tzinfo=None)
                    gps_age_min = int(delta.total_seconds() // 60)

                driver = (
                    (job.driver_id.name if job.driver_id else "")
                    or vehicle.driver_id.name
                    or vehicle.x_current_driver_contact_id.name
                )
                trucks[vid] = {
                    "id": vid,
                    "name": vehicle.name or "",
                    "license_plate": vehicle.license_plate or "",
                    "driver": driver,
                    "customer": job.partner_id.name if job.partner_id else "",
                    "lat": lat,
                    "lng": lng,
                    "gps_age_min": gps_age_min,
                    "address": vehicle.x_last_location_address or "",
                    "job_id": job.id,
                    "job_name": job.name,
                    "stops": [],
                }

            # Attach stops (skip cancelled)
            for stop in job.stop_ids.filtered(
                lambda s: s.status != "cancelled"
            ).sorted("sequence"):
                trucks[vid]["stops"].append({
                    "id": stop.id,
                    "seq": stop.sequence // 10,
                    "type": stop.stop_type,
                    "address": stop.address or "",
                    "lat": stop.latitude or 0.0,
                    "lng": stop.longitude or 0.0,
                    "status": stop.status,
                })

        return {"trucks": list(trucks.values())}


    @http.route(
        "/dispatch/track/<string:tracking_number>",
        type="http", auth="public", website=True, sitemap=False,
    )
    def track_shipment(self, tracking_number, **kwargs):
        job = request.env["prema.dispatch.job"].sudo().search(
            [("tracking_number", "=", tracking_number)], limit=1
        )
        if not job:
            return request.render(
                "prema_dispatch.portal_tracking_not_found",
                {"tracking_number": tracking_number},
            )
        events = request.env["prema.dispatch.timeline.event"].sudo().search(
            [("job_id", "=", job.id)], order="occurred_at asc"
        )
        # All active stops on the truck (for map), but privacy-filtered:
        # customer sees their own stop details, other stops only as city
        vehicle = job.vehicle_id
        truck_lat = vehicle.x_last_location_lat if vehicle else 0
        truck_lng = vehicle.x_last_location_lng if vehicle else 0

        stops = job.stop_ids.filtered(
            lambda s: s.status not in ("cancelled",)
        ).sorted("sequence")

        # Find customer's next pending stop for ETA
        next_stop = next(
            (s for s in stops if s.status in ("pending", "en_route")), None
        )
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")

        return request.render(
            "prema_dispatch.portal_tracking_page",
            {
                "job":        job,
                "events":     events,
                "stops":      stops,
                "next_stop":  next_stop,
                "truck_lat":  truck_lat,
                "truck_lng":  truck_lng,
                "google_api_key": api_key,
            },
        )

    @http.route(
        "/dispatch/track/<string:tracking_number>/live",
        type="json", auth="public",
    )
    def track_live(self, tracking_number, **kwargs):
        """JSON endpoint polled every 30s by the tracking portal for live updates."""
        job = request.env["prema.dispatch.job"].sudo().search(
            [("tracking_number", "=", tracking_number)], limit=1
        )
        if not job:
            return {"error": "not found"}
        vehicle = job.vehicle_id
        stops = job.stop_ids.filtered(lambda s: s.status != "cancelled").sorted("sequence")
        next_stop = next((s for s in stops if s.status in ("pending","en_route")), None)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        gps_age = None
        if vehicle and vehicle.x_last_location_at:
            gps_age = int((now - vehicle.x_last_location_at.replace(tzinfo=None)).total_seconds() / 60)
        return {
            "truck_lat":  vehicle.x_last_location_lat if vehicle else 0,
            "truck_lng":  vehicle.x_last_location_lng if vehicle else 0,
            "gps_age_min": gps_age,
            "next_stop_id": next_stop.id if next_stop else None,
            "next_stop_addr": (next_stop.address or "").split(",")[0] if next_stop else "",
            "next_stop_status": next_stop.status if next_stop else "",
            "stops": [{
                "id":     s.id,
                "status": s.status,
                "city":   (s.address or "").split(",")[0],
                "lat":    s.latitude or 0,
                "lng":    s.longitude or 0,
            } for s in stops],
        }

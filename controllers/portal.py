from datetime import datetime, timezone

from odoo import http
from odoo.http import request


class DispatchTrackingController(http.Controller):

    @http.route("/web/dispatch/live-map/data", type="json", auth="user")
    def live_map_data(self, **kwargs):
        """Return all operational vehicles with GPS positions and active dispatch jobs.
        Excludes non-operational vehicles (e.g. DEMO-01)."""
        Vehicle = request.env["fleet.vehicle"].sudo()
        Job = request.env["prema.dispatch.job"]

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # ── 1. Seed all operational vehicles ────────────────────────────
        vehicles = Vehicle.search([
            ("active", "=", True),
            ("x_operational_logistics", "=", True),
        ])

        trucks = {}
        for v in vehicles:
            lat = v.x_last_location_lat or 0.0
            lng = v.x_last_location_lng or 0.0
            gps_at = v.x_last_location_at
            gps_age_min = None
            if gps_at:
                delta = now_utc - gps_at.replace(tzinfo=None)
                gps_age_min = int(delta.total_seconds() // 60)

            driver = v.driver_id.name or v.x_current_driver_contact_id.name or ""
            trucks[v.id] = {
                "id": v.id,
                "name": v.name or "",
                "license_plate": v.license_plate or "",
                "driver": driver,
                "customer": "",
                "lat": lat,
                "lng": lng,
                "gps_age_min": gps_age_min,
                "address": v.x_last_location_address or "",
                "job_id": None,
                "job_name": "",
                "stops": [],
            }

        # ── 2. Overlay active dispatch jobs ────────────────────────────
        active_jobs = Job.search([
            ("stage_id.stage_type", "not in", ["cancelled", "completed"]),
            ("vehicle_id", "!=", False),
            ("vehicle_id.x_operational_logistics", "=", True),
        ], limit=200)

        for job in active_jobs:
            vehicle = job.vehicle_id
            vid = vehicle.id
            if vid not in trucks:
                continue  # Non-operational vehicle — skip

            # Update with job-specific data
            trucks[vid]["customer"] = job.partner_id.name if job.partner_id else ""
            trucks[vid]["job_id"] = job.id
            trucks[vid]["job_name"] = job.name

            # Update driver from job if not already set from vehicle
            if not trucks[vid]["driver"]:
                trucks[vid]["driver"] = (
                    (job.driver_id.name if job.driver_id else "") or ""
                )

            # Attach stops
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
        tracking_token = (kwargs.get("token") or "").strip()

        # Security: require token to prevent sequential-number enumeration.
        domain = [("tracking_number", "=", tracking_number)]
        if tracking_token:
            domain.append(("tracking_token", "=", tracking_token))

        job = request.env["prema.dispatch.job"].sudo().search(domain, limit=1)

        # Fallback: resolve from logistics.booking.booking_number
        # (booking number like PF-260811-000011 may not match job.tracking_number)
        if not job:
            booking = request.env["logistics.booking"].sudo().search(
                [("booking_number", "=", tracking_number)], limit=1,
            )
            if booking and booking.dispatch_job_id:
                job = booking.dispatch_job_id
                # If token provided, verify it against booking
                if tracking_token and booking.tracking_token != tracking_token:
                    return request.render(
                        "prema_dispatch.portal_tracking_not_found",
                        {"tracking_number": tracking_number},
                    )

        if not job:
            return request.render(
                "prema_dispatch.portal_tracking_not_found",
                {"tracking_number": tracking_number},
            )

        # Require token for direct dispatch-job tracking
        if not tracking_token and not job.logistics_booking_id:
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
        tracking_token = (kwargs.get("token") or "").strip()
        domain = [("tracking_number", "=", tracking_number)]
        if tracking_token:
            domain.append(("tracking_token", "=", tracking_token))
        job = request.env["prema.dispatch.job"].sudo().search(domain, limit=1)
        if not job or not tracking_token:
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

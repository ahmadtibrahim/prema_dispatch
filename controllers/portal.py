from datetime import datetime, timezone

from odoo import http
from odoo.http import request


class DispatchTrackingController(http.Controller):

    @http.route("/web/dispatch/live-map/data", type="json", auth="user")
    def live_map_data(self, **kwargs):
        """Return operational vehicles and active dispatch jobs to staff users."""
        user = request.env.user
        allowed = (
            user.has_group("prema_dispatch.group_dispatcher")
            or user.has_group("prema_dispatch.group_dispatch_manager")
            or user.has_group("base.group_system")
        )
        if not allowed:
            return {"trucks": []}

        Vehicle = request.env["fleet.vehicle"].sudo()
        Job = request.env["prema.dispatch.job"].sudo()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        vehicles = Vehicle.search([
            ("active", "=", True),
            ("x_operational_logistics", "=", True),
        ])
        trucks = {}
        for vehicle in vehicles:
            gps_at = vehicle.x_last_location_at
            gps_age_min = None
            if gps_at:
                gps_age_min = int((now_utc - gps_at.replace(tzinfo=None)).total_seconds() // 60)
            driver = vehicle.driver_id.name or vehicle.x_current_driver_contact_id.name or ""
            trucks[vehicle.id] = {
                "id": vehicle.id,
                "name": vehicle.name or "",
                "license_plate": vehicle.license_plate or "",
                "driver": driver,
                "customer": "",
                "lat": vehicle.x_last_location_lat or 0.0,
                "lng": vehicle.x_last_location_lng or 0.0,
                "gps_age_min": gps_age_min,
                "address": vehicle.x_last_location_address or "",
                "job_id": None,
                "job_name": "",
                "stops": [],
            }

        active_jobs = Job.search([
            ("stage_id.stage_type", "not in", ["cancelled", "completed"]),
            ("vehicle_id", "!=", False),
            ("vehicle_id.x_operational_logistics", "=", True),
        ], limit=200)
        for job in active_jobs:
            vehicle = job.vehicle_id
            if vehicle.id not in trucks:
                continue
            trucks[vehicle.id]["customer"] = job.partner_id.name if job.partner_id else ""
            trucks[vehicle.id]["job_id"] = job.id
            trucks[vehicle.id]["job_name"] = job.name
            if not trucks[vehicle.id]["driver"] and job.driver_id:
                trucks[vehicle.id]["driver"] = job.driver_id.name
            for stop in job.stop_ids.filtered(lambda s: s.status != "cancelled").sorted("sequence"):
                trucks[vehicle.id]["stops"].append({
                    "id": stop.id,
                    "seq": stop.sequence // 10,
                    "type": stop.stop_type,
                    "address": stop.address or "",
                    "lat": stop.latitude or 0.0,
                    "lng": stop.longitude or 0.0,
                    "status": stop.status,
                })
        return {"trucks": list(trucks.values())}

    @staticmethod
    def _tracking_job_for_user(tracking_number):
        """Resolve a shipment only when the signed-in user owns it.

        Dispatch staff may inspect any shipment. Portal customers are scoped to
        their commercial partner. Sequential tracking/booking numbers therefore
        reveal nothing to another customer and anonymous access is impossible.
        """
        env = request.env
        user = env.user
        staff = (
            user.has_group("prema_dispatch.group_dispatcher")
            or user.has_group("prema_dispatch.group_dispatch_manager")
            or user.has_group("base.group_system")
        )
        Booking = env["logistics.booking"].sudo()
        booking = Booking.search([("booking_number", "=", tracking_number)], limit=1)
        if not booking:
            job = env["prema.dispatch.job"].sudo().search([
                ("tracking_number", "=", tracking_number),
            ], limit=1)
            booking = job.logistics_booking_id if job else Booking
        if not booking:
            return env["prema.dispatch.job"]

        if not staff:
            owner = booking.commercial_partner_id or booking.partner_id.commercial_partner_id
            current = user.partner_id.commercial_partner_id
            if not owner or owner != current:
                return env["prema.dispatch.job"]

        jobs = env["prema.dispatch.job"].sudo().search([
            ("logistics_booking_id", "=", booking.id),
            ("active", "=", True),
        ], order="operation_date, id")
        return jobs[:1] or booking.dispatch_job_id

    @http.route(
        "/dispatch/track/<string:tracking_number>",
        type="http", auth="user", website=True, sitemap=False,
    )
    def track_shipment(self, tracking_number, **kwargs):
        del kwargs
        job = self._tracking_job_for_user(tracking_number)
        if not job:
            return request.render(
                "prema_dispatch.portal_tracking_not_found",
                {"tracking_number": tracking_number},
            )

        booking = job.logistics_booking_id
        all_jobs = request.env["prema.dispatch.job"].sudo().search([
            ("logistics_booking_id", "=", booking.id),
            ("active", "=", True),
        ], order="operation_date, id") if booking else job
        events = request.env["prema.dispatch.timeline.event"].sudo().search([
            ("job_id", "in", all_jobs.ids),
        ], order="occurred_at asc")
        customer_stops = all_jobs.mapped("stop_ids").filtered(
            lambda s: s.status != "cancelled"
            and s.stop_type in ("pickup", "dropoff", "return")
        ).sorted(lambda s: (s.job_id.operation_date or datetime.min.date(), s.sequence, s.id))

        vehicle = next((j.vehicle_id for j in all_jobs if j.vehicle_id), False)
        next_stop = next((s for s in customer_stops if s.status in ("pending", "en_route")), None)
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return request.render(
            "prema_dispatch.portal_tracking_page",
            {
                "job": job,
                "booking": booking,
                "events": events,
                "stops": customer_stops,
                "next_stop": next_stop,
                "truck_lat": vehicle.x_last_location_lat if vehicle else 0,
                "truck_lng": vehicle.x_last_location_lng if vehicle else 0,
                "google_api_key": api_key,
                "operational_status": getattr(booking, "operational_status", False) if booking else False,
            },
        )

    @http.route(
        "/dispatch/track/<string:tracking_number>/live",
        type="json", auth="user",
    )
    def track_live(self, tracking_number, **kwargs):
        del kwargs
        job = self._tracking_job_for_user(tracking_number)
        if not job:
            return {"error": "not found"}

        booking = job.logistics_booking_id
        jobs = request.env["prema.dispatch.job"].sudo().search([
            ("logistics_booking_id", "=", booking.id),
            ("active", "=", True),
        ], order="operation_date, id") if booking else job
        stops = jobs.mapped("stop_ids").filtered(
            lambda s: s.status != "cancelled" and s.stop_type in ("pickup", "dropoff", "return")
        ).sorted(lambda s: (s.job_id.operation_date or datetime.min.date(), s.sequence, s.id))
        next_stop = next((s for s in stops if s.status in ("pending", "en_route")), None)
        vehicle = next((j.vehicle_id for j in jobs if j.vehicle_id), False)
        gps_age = None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if vehicle and vehicle.x_last_location_at:
            gps_age = int((now - vehicle.x_last_location_at.replace(tzinfo=None)).total_seconds() / 60)
        return {
            "truck_lat": vehicle.x_last_location_lat if vehicle else 0,
            "truck_lng": vehicle.x_last_location_lng if vehicle else 0,
            "gps_age_min": gps_age,
            "status": getattr(booking, "operational_status", False) if booking else False,
            "next_stop_id": next_stop.id if next_stop else None,
            "next_stop_addr": (next_stop.address or "").split(",")[0] if next_stop else "",
            "next_stop_status": next_stop.status if next_stop else "",
            "stops": [{
                "id": stop.id,
                "status": stop.status,
                "city": (stop.address or "").split(",")[0],
                "lat": stop.latitude or 0,
                "lng": stop.longitude or 0,
            } for stop in stops],
        }

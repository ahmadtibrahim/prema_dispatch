import secrets

from odoo import http
from odoo.http import request
from werkzeug.exceptions import NotFound as HttpNotFound

STATUS_LABELS = {
    "confirmed": "Booking Confirmed",
    "picked_up": "Picked Up",
    "in_transit": "In Transit",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


class LogisticsTracking(http.Controller):

    @http.route("/track", type="http", auth="public", website=True, sitemap=False)
    def tracking_landing(self, **kwargs):
        return request.render("prema_logistics_booking.portal_tracking_lookup", {})

    @http.route("/track/search", type="http", auth="public", website=True, sitemap=False, methods=["POST"])
    def tracking_search(self, **kwargs):
        tracking_number = (kwargs.get("tracking_number") or "").strip()
        tracking_token = (kwargs.get("tracking_token") or "").strip()

        if not tracking_number:
            return request.render("prema_logistics_booking.portal_tracking_lookup", {
                "error": "Please enter a tracking number.",
            })

        # Security: require both booking_number AND tracking_token to prevent enumeration.
        # Sequential booking numbers (PF-YYMMDD-000001) alone must not reveal shipment data.
        if not tracking_token:
            return request.render("prema_logistics_booking.portal_tracking_lookup", {
                "error": "Please enter your tracking token (found in your confirmation email).",
            })

        booking = request.env["logistics.booking"].sudo().search([
            ("booking_number", "=", tracking_number),
            ("tracking_token", "=", tracking_token),
        ], limit=1)

        if not booking:
            return request.render("prema_logistics_booking.portal_tracking_lookup", {
                "error": "Tracking number not found. Please check and try again.",
            })

        status = booking.state
        dispatch_job = booking.dispatch_job_id
        if dispatch_job and dispatch_job.stage_id:
            stage_name = (dispatch_job.stage_id.name or "").lower()
            if "transit" in stage_name:
                status = "in_transit"
            elif "deliver" in stage_name or "complete" in stage_name:
                status = "delivered"
            elif "pickup" in stage_name or "load" in stage_name:
                status = "picked_up"

        # PRIVACY: only THIS booking's own stops are ever shown — the
        # consolidated truck route's other customers are never revealed.
        # Each stop matches its dispatch twin via the booking-stop bridge.
        stops_display = booking._tracking_stops_display()

        return request.render("prema_logistics_booking.portal_tracking_result", {
            "booking": booking,
            "status_label": STATUS_LABELS.get(status, booking.state),
            "status": status,
            "dispatch_job": dispatch_job,
            "stops_display": stops_display,
        })

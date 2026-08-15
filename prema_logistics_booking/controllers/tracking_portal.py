from odoo import http
from odoo.http import request


STATUS_LABELS = {
    "booked": "Booked",
    "planned": "Planned",
    "dispatched": "Dispatched",
    "picked_up": "Picked Up",
    "in_transit": "In Transit",
    "out_for_delivery": "Out for Delivery",
    "partially_delivered": "Partially Delivered",
    "delivered": "Delivered",
    "delayed": "Delayed / Exception",
    "cancelled": "Cancelled",
}


class LogisticsTracking(http.Controller):

    @staticmethod
    def _owned_booking(tracking_number):
        user = request.env.user
        booking = request.env["logistics.booking"].sudo().search([
            ("booking_number", "=", tracking_number),
        ], limit=1)
        if not booking:
            return booking
        staff = (
            user.has_group("prema_dispatch.group_dispatcher")
            or user.has_group("prema_dispatch.group_dispatch_manager")
            or user.has_group("base.group_system")
        )
        if staff:
            return booking
        owner = booking.commercial_partner_id or booking.partner_id.commercial_partner_id
        return booking if owner == user.partner_id.commercial_partner_id else request.env["logistics.booking"]

    @http.route("/track", type="http", auth="user", website=True, sitemap=False)
    def tracking_landing(self, **kwargs):
        del kwargs
        return request.render("prema_logistics_booking.portal_tracking_lookup", {})

    @http.route("/track/search", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def tracking_search(self, **kwargs):
        tracking_number = (kwargs.get("tracking_number") or "").strip()
        if not tracking_number:
            return request.render("prema_logistics_booking.portal_tracking_lookup", {
                "error": "Please enter a tracking number.",
            })
        booking = self._owned_booking(tracking_number)
        if not booking:
            return request.render("prema_logistics_booking.portal_tracking_lookup", {
                "error": "Tracking number not found.",
            })

        status = getattr(booking, "operational_status", False) or (
            "cancelled" if booking.state == "cancelled" else "booked"
        )
        return request.render("prema_logistics_booking.portal_tracking_result", {
            "booking": booking,
            "status_label": STATUS_LABELS.get(status, status.replace("_", " ").title()),
            "status": status,
            "dispatch_job": booking.dispatch_job_id,
        })

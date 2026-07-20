import json
import logging

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DriverAppController(http.Controller):

    # ── Main driver app page ─────────────────────────────────────

    @http.route("/dispatch/driver", type="http", auth="public", website=False)
    def driver_app(self, **kwargs):
        """Serve the driver app shell page."""
        import time
        user = request.env.user

        # Redirect to login if not authenticated
        if user._is_public():
            return request.redirect("/web/login?redirect=/dispatch/driver")

        allowed_groups = [
            "prema_dispatch.group_dispatch_driver",
            "prema_dispatch.group_dispatch_manager",
            "prema_dispatch.group_dispatcher",
            "base.group_system",
        ]
        if not any(user.has_group(g) for g in allowed_groups):
            return request.redirect("/web/login?redirect=/dispatch/driver")

        api_key    = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        dispatch_phone = request.env["ir.config_parameter"].sudo().get_param("dispatch_phone_number", "")
        dispatch_voip_uri = request.env["ir.config_parameter"].sudo().get_param("dispatch_voip_uri", "")
        cache_ver  = str(int(time.time()))
        return request.render("prema_dispatch.driver_app_page", {
            "google_api_key": api_key,
            "dispatch_phone": dispatch_phone,
            "dispatch_voip_uri": dispatch_voip_uri,
            "driver_name":    user.partner_id.name,
            "cache_ver":      cache_ver,
        })

    # ── Pin update (called from driver app map interaction) ──────

    @http.route("/dispatch/driver/dates", type="json", auth="user", methods=["POST"])
    def driver_available_dates(self, week_offset=0, **kwargs):
        return request.env["prema.dispatch.job"].get_driver_available_dates(int(week_offset))

    @http.route("/dispatch/driver/jobs", type="json", auth="user", methods=["POST"])
    def driver_jobs_by_date(self, date_str=None, **kwargs):
        return request.env["prema.dispatch.job"].get_driver_today_jobs(date_str)

    @http.route("/dispatch/driver/stops", type="json", auth="user", methods=["POST"])
    def driver_stops_by_date(self, date_str=None, **kwargs):
        return request.env["prema.dispatch.job"].get_driver_stops_for_date(date_str)

    @http.route("/dispatch/driver/evidence/add", type="json", auth="user", methods=["POST"])
    def add_evidence(self, stop_id, ev_type, data_b64, filename="photo.jpg", **kwargs):
        return request.env["prema.dispatch.job"].driver_add_evidence(stop_id, ev_type, data_b64, filename)

    @http.route("/dispatch/driver/evidence/remove", type="json", auth="user", methods=["POST"])
    def remove_evidence(self, stop_id, ev_type, att_id, **kwargs):
        return request.env["prema.dispatch.job"].driver_remove_evidence(stop_id, ev_type, att_id)

    @http.route("/dispatch/driver/chat/init", type="json", auth="user", methods=["POST"])
    def chat_init(self, **kwargs):
        return request.env["prema.dispatch.job"].get_or_create_driver_channel()

    @http.route("/dispatch/driver/chat/send", type="json", auth="user", methods=["POST"])
    def chat_send(self, channel_id, body, **kwargs):
        return request.env["prema.dispatch.job"].driver_send_message(int(channel_id), body)

    @http.route("/dispatch/driver/bus/channel", type="json", auth="user", methods=["POST"])
    def bus_channel(self, **kwargs):
        """Return the bus channel name for this driver (for real-time stop updates)."""
        partner_id = request.env.user.partner_id.id
        return {"channel": f"driver_route_{partner_id}"}

    @http.route("/dispatch/driver/chat/messages", type="json", auth="user", methods=["POST"])
    def chat_messages(self, channel_id, **kwargs):
        cid = int(channel_id)
        channel = request.env["discuss.channel"].browse(cid)
        if not channel.exists():
            return {"messages": []}
        partner = request.env.user.partner_id
        msgs = request.env["mail.message"].search(
            [("res_id", "=", cid), ("model", "=", "discuss.channel"),
             ("message_type", "in", ["comment", "email"])],
            order="date desc", limit=50
        )
        return {"messages": [{
            "id":     m.id,
            "author": m.author_id.name or "Unknown",
            "body":   m.body or "",
            "date":   m.date.isoformat() if m.date else "",
            "is_me":  m.author_id.id == partner.id,
        } for m in reversed(msgs)]}

    @http.route("/dispatch/driver/stop/delete", type="json", auth="user", methods=["POST"])
    def delete_stop(self, stop_id, **kwargs):
        return request.env["prema.dispatch.job"].driver_delete_stop(int(stop_id))

    @http.route("/dispatch/driver/stop/service_time", type="json", auth="user", methods=["POST"])
    def update_service_time(self, stop_id, minutes, **kwargs):
        return request.env["prema.dispatch.job"].driver_update_service_time(int(stop_id), int(minutes))

    @http.route("/dispatch/board/geocode_stops", type="json", auth="user", methods=["POST"])
    def geocode_stops(self, date_str=None, **kwargs):
        return request.env["prema.dispatch.job"].geocode_stops_for_date(date_str)

    @http.route("/dispatch/driver/stop/reorder", type="json", auth="user", methods=["POST"])
    def reorder_stops(self, job_id, stop_order, **kwargs):
        return request.env["prema.dispatch.job"].driver_reorder_stops(job_id, stop_order)

    @http.route("/dispatch/driver/stop/pin", type="json", auth="user", methods=["POST"])
    def update_stop_pin(self, stop_id, lat, lng, **kwargs):
        """Update a stop's parking pin from the driver app or dispatch map."""
        result = request.env["prema.dispatch.job"].driver_update_stop(
            stop_id, "update_pin", {"lat": lat, "lng": lng}
        )
        return result

    @http.route("/dispatch/driver/stop/regeocode", type="json", auth="user", methods=["POST"])
    def regeocode_stop(self, stop_id, **kwargs):
        """Reset a stop's pin back to its geocoded address ("Use Address" button)."""
        return request.env["prema.dispatch.job"].regeocode_stop(int(stop_id))

    @http.route("/dispatch/driver/weather", type="json", auth="user", methods=["POST"])
    def driver_weather(self, lat, lng, **kwargs):
        return request.env["prema.dispatch.job"].get_weather_for_location(float(lat), float(lng))

    @http.route("/dispatch/driver/stop/transfer", type="json", auth="user", methods=["POST"])
    def execute_transfer(self, stop_id, **kwargs):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = request.env["prema.dispatch.stop"].browse(int(stop_id))
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(request.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        try:
            return stop.action_execute_transfer()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ── Stop status update ───────────────────────────────────────

    @http.route("/dispatch/driver/stop/status", type="json", auth="user", methods=["POST"])
    def update_stop_status(self, stop_id, action, data=None, **kwargs):
        result = request.env["prema.dispatch.job"].driver_update_stop(
            stop_id, action, data or {}
        )
        return result

    @http.route("/dispatch/driver/job/finish", type="json", auth="user", methods=["POST"])
    def finish_job(self, job_id, **kwargs):
        return request.env["prema.dispatch.job"].driver_finish_job(job_id)

    # ── Entrance photo upload ────────────────────────────────────

    @http.route("/dispatch/driver/stop/photo", type="json", auth="user", methods=["POST"])
    def upload_entrance_photo(self, stop_id, image_b64, filename="entrance.jpg", **kwargs):
        result = request.env["prema.dispatch.job"].driver_upload_entrance_photo(
            stop_id, image_b64, filename
        )
        return result

    # ── Dispatcher: update stop pin from the dispatch board map ─

    @http.route("/dispatch/board/stop/pin", type="json", auth="user", methods=["POST"])
    def board_update_stop_pin(self, stop_id, lat, lng, **kwargs):
        """Called when dispatcher drags a stop pin on the dispatch board map."""
        user = request.env.user
        allowed = [
            "prema_dispatch.group_dispatch_manager",
            "prema_dispatch.group_dispatcher",
            "base.group_system",
        ]
        if not any(user.has_group(g) for g in allowed):
            return {"success": False, "error": "Not authorized"}
        result = request.env["prema.dispatch.job"].driver_update_stop(
            stop_id, "update_pin", {"lat": lat, "lng": lng}
        )
        return result

    # ── Saved location data for a stop ──────────────────────────

    @http.route("/dispatch/location/<int:location_id>/photo", type="http", auth="user")
    def location_photo(self, location_id, **kwargs):
        """Serve the entrance photo for a saved location."""
        loc = request.env["prema.dispatch.location"].browse(location_id)
        if not loc.exists() or not loc.entrance_photo:
            return request.not_found()
        import base64
        data = base64.b64decode(loc.entrance_photo)
        return request.make_response(data, headers=[
            ("Content-Type", "image/jpeg"),
            ("Cache-Control", "max-age=86400"),
        ])

    @http.route("/dispatch/driver/job/summaries", type="json", auth="user", methods=["POST"])
    def driver_job_summaries(self, date_str=None, **kw):
        data = request.env["prema.dispatch.job"].get_driver_today_jobs(date_str)
        return {"success": True, "jobs": data.get("jobs", [])}

    @http.route("/dispatch/driver/job/route-sheet-received", type="json", auth="user", methods=["POST"])
    def driver_route_sheet_received(self, job_id, **kw):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_add_stop
        try:
            job = request.env["prema.dispatch.job"].browse(int(job_id))
            check_driver_can_add_stop(request.env, job)
            job.write({"route_sheet_received_at": fields.Datetime.now(), "route_sheet_received_by": request.env.user.id})
            return {"success": True, "job": job._driver_job_summary()}
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}

    @http.route("/dispatch/driver/location/search", type="json", auth="user", methods=["POST"])
    def driver_location_search(self, query="", limit=20, offset=0, **kw):
        return request.env["prema.dispatch.location"].sudo().driver_search_locations(query, limit, offset)

    @http.route("/dispatch/driver/location/get", type="json", auth="user", methods=["POST"])
    def driver_location_get(self, location_id, **kw):
        loc = request.env["prema.dispatch.location"].sudo().browse(int(location_id))
        if not loc.exists():
            return {"success": False, "code": "location_not_found", "error": "Location not found"}
        return {"success": True, "location": loc._driver_payload()}

    @http.route("/dispatch/driver/stop/create", type="json", auth="user", methods=["POST"])
    def driver_stop_create(self, job_id, values=None, **kw):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_add_stop
        try:
            job = request.env["prema.dispatch.job"].browse(int(job_id))
            check_driver_can_add_stop(request.env, job)
            values = values or {}
            allowed = {"saved_location_id", "stop_type", "sequence", "address", "contact_name", "contact_phone", "dock_door", "pallets_in", "pallets_out", "pod_required", "scheduled_time"}
            vals = {k: values[k] for k in allowed if k in values}
            vals["job_id"] = job.id
            stop = request.env["prema.dispatch.stop"].sudo().create(vals)
            if job.stops_confirmation_state == "pending":
                job.sudo().write({"stops_confirmation_state": "partial"})
            return {"success": True, "stop": request.env["prema.dispatch.job"]._driver_stop_dict(stop)}
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}

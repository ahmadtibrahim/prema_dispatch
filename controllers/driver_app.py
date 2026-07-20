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

    @http.route("/dispatch/driver/stop/update", type="json", auth="user", methods=["POST"])
    def update_stop_details(self, stop_id, values=None, **kwargs):
        return request.env["prema.dispatch.job"].driver_edit_stop(int(stop_id), values or {})

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

    @http.route("/dispatch/driver/pickup/confirm", type="json", auth="user", methods=["POST"])
    def driver_pickup_confirm(self, stop_id, values=None, **kw):
        return request.env["prema.dispatch.job"].driver_confirm_pickup_actuals(int(stop_id), values or {})

    @http.route("/dispatch/driver/pickup/finalize", type="json", auth="user", methods=["POST"])
    def driver_pickup_finalize(self, stop_id, values=None, **kw):
        return request.env["prema.dispatch.job"].driver_finalize_pickup_intake(int(stop_id), values or {})

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
            if job.route_definition_mode == "stops_pending" and job.stops_confirmation_state in ("pending", "confirmed"):
                job.sudo().write({"stops_confirmation_state": "partial"})
            return {"success": True, "stop": request.env["prema.dispatch.job"]._driver_stop_dict(stop)}
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}

    # ── Manual location creation (Phase 16) ──────────────────────

    @http.route("/dispatch/driver/location/duplicates", type="json", auth="user", methods=["POST"])
    def driver_location_duplicates(self, job_id, values=None, **kw):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_create_location
        try:
            job = request.env["prema.dispatch.job"].browse(int(job_id))
            check_driver_can_create_location(request.env, job)
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}
        values = values or {}
        query = " ".join(filter(None, [values.get("chain_name"), values.get("location_number"), values.get("business_name")])) \
            or values.get("business_name") or values.get("street") or ""
        result = request.env["prema.dispatch.location"].sudo().driver_search_locations(query, limit=10)
        return {"success": True, "candidates": result.get("results", [])}

    @http.route("/dispatch/driver/location/create", type="json", auth="user", methods=["POST"])
    def driver_location_create(self, job_id, values=None, use_existing_location_id=None, **kw):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_create_location
        try:
            job = request.env["prema.dispatch.job"].browse(int(job_id))
            check_driver_can_create_location(request.env, job)
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}

        Location = request.env["prema.dispatch.location"].sudo()
        if use_existing_location_id:
            loc = Location.browse(int(use_existing_location_id))
            if not loc.exists():
                return {"success": False, "code": "location_not_found", "error": "Location not found"}
            return {"success": True, "location": loc._driver_payload(), "reused_existing": True}

        values = values or {}
        allowed = {
            "chain_name", "location_number", "business_name", "street", "street2", "unit",
            "city", "province_code", "postal_code", "dock_door", "parking_notes",
            "driver_instructions", "address", "google_place_id", "address_formatted",
            "address_validated",
        }
        vals = {k: values[k] for k in allowed if k in values}
        if not vals.get("business_name"):
            return {"success": False, "code": "business_name_required", "error": "Business name is required."}

        address_parts = [vals.get(k) for k in ("street", "unit", "city", "province_code", "postal_code")]
        full_address = (vals.get("address") or "").strip() or ", ".join(p for p in address_parts if p)
        if not full_address:
            return {"success": False, "code": "address_required", "error": "Address is required."}
        vals["address"] = full_address
        vals["name"] = values.get("name") or vals.get("business_name")
        vals["source_type"] = "google_places" if vals.get("google_place_id") else "driver_manual"
        vals["verification_state"] = "driver_submitted"
        vals["created_by_driver_id"] = request.env.user.partner_id.id
        if vals.get("address_formatted") and "address_validated" not in vals:
            vals["address_validated"] = True

        lat = values.get("lat")
        lng = values.get("lng")
        pin_source = "driver_map" if (lat and lng and values.get("exact_pin_confirmed")) else ("google_place" if vals.get("google_place_id") and lat and lng else None)
        if not (lat and lng):
            try:
                hits = request.env["premafirm.rate.estimator"].sudo().geocode_address_rpc(full_address)
                if hits:
                    lat, lng = hits[0].get("lat"), hits[0].get("lng")
                    pin_source = "geocoded_address"
            except Exception:
                _logger.warning("driver_location_create: geocoding failed for %r", full_address, exc_info=True)
        if lat and lng:
            vals["pin_lat"] = lat
            vals["pin_lng"] = lng
            vals["pin_set"] = bool(values.get("exact_pin_confirmed"))
            vals["pin_source"] = pin_source

        loc = Location.create(vals)
        return {"success": True, "location": loc._driver_payload(), "reused_existing": False}

    # ── Photo-to-location extraction: Ship To vs Invoice To (Phase 17) ─

    @http.route("/dispatch/driver/location/extract", type="json", auth="user", methods=["POST"])
    def driver_location_extract(self, job_id, data_b64, filename="scan.jpg", mimetype="image/jpeg",
                                 extraction_context="ship_to", stop_id=None, **kw):
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_driver_can_create_location
        from odoo.addons.prema_dispatch.services.location_extraction_service import LocationExtractionService

        try:
            job = request.env["prema.dispatch.job"].browse(int(job_id))
            check_driver_can_create_location(request.env, job)
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}

        try:
            image_bytes = base64.b64decode(data_b64)
        except Exception:
            return {"success": False, "code": "invalid_image", "error": "Could not decode image."}

        service = LocationExtractionService(request.env)
        try:
            normalized = service.extract_location(
                image_bytes, extraction_context, filename=filename, mimetype=mimetype,
                job_id=job.id, stop_id=int(stop_id) if stop_id else None,
            )
        except Exception as exc:
            return {"success": False, "code": str(exc), "error": str(exc)}
        return {"success": True, "extraction": normalized}

    # ── Location photo history (Phase 26) ────────────────────────

    @http.route("/dispatch/driver/location/photo/upload", type="json", auth="user", methods=["POST"])
    def driver_location_photo_upload(self, location_id, photo_type, data_b64, filename="photo.jpg", job_id=None, stop_id=None, **kw):
        loc = request.env["prema.dispatch.location"].sudo().browse(int(location_id))
        if not loc.exists():
            return {"success": False, "code": "location_not_found", "error": "Location not found"}
        try:
            image_bytes = base64.b64decode(data_b64)
        except Exception:
            return {"success": False, "code": "invalid_image", "error": "Could not decode image."}
        attachment = request.env["ir.attachment"].sudo().create({
            "name": filename, "datas": base64.b64encode(image_bytes),
            "res_model": "prema.dispatch.location.photo", "mimetype": "image/jpeg",
        })
        photo = request.env["prema.dispatch.location.photo"].sudo().create({
            "location_id": loc.id, "attachment_id": attachment.id, "photo_type": photo_type,
            "source_job_id": int(job_id) if job_id else False,
            "source_stop_id": int(stop_id) if stop_id else False,
            "uploaded_by": request.env.user.id,
        })
        attachment.write({"res_id": photo.id})
        return {"success": True, "photo_id": photo.id}

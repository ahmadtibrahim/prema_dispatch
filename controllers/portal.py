from datetime import datetime, timezone

from odoo import http
from odoo.http import request


class DispatchTrackingController(http.Controller):


    @http.route(
        "/dispatch/track/<string:tracking_number>",
        type="http", auth="public", website=True, sitemap=False,
    )
    def track_shipment(self, tracking_number, **kwargs):
        tracking_token = (kwargs.get("token") or "").strip()

        domain = [("tracking_number", "=", tracking_number)]
        if tracking_token:
            domain.append(("tracking_token", "=", tracking_token))

        job = request.env["prema.dispatch.job"].sudo().search(domain, limit=1)

        if not job:
            booking = request.env["logistics.booking"].sudo().search(
                [("booking_number", "=", tracking_number)], limit=1,
            )
            if booking and booking.dispatch_job_id:
                job = booking.dispatch_job_id
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

        if not tracking_token and not job.logistics_booking_id:
            return request.render(
                "prema_dispatch.portal_tracking_not_found",
                {"tracking_number": tracking_number},
            )

        events = request.env["prema.dispatch.timeline.event"].sudo().search(
            [("job_id", "=", job.id)], order="occurred_at asc"
        )
        vehicle = job.vehicle_id
        truck_lat = vehicle.x_last_location_lat if vehicle else 0
        truck_lng = vehicle.x_last_location_lng if vehicle else 0

        stops = job.stop_ids.filtered(lambda s: s.status not in ("cancelled",)).sorted("sequence")
        next_stop = next((s for s in stops if s.status in ("pending", "en_route")), None)
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        progress = job._board_live_progress()

        return request.render(
            "prema_dispatch.portal_tracking_page",
            {
                "job": job,
                "events": events,
                "stops": stops,
                "next_stop": next_stop,
                "truck_lat": truck_lat,
                "truck_lng": truck_lng,
                "google_api_key": api_key,
                "live_progress": progress["key"],
                "live_progress_label": progress["label"],
                "pallets": self._tracking_pallets(job, tracking_token),
            },
        )

    # ── Pallet evidence on the tracking page (spec §4-§6) ────────

    @staticmethod
    def _stop_label(stop):
        """'Costco Wholesale — Toronto' style label from the saved location
        or partner name + city. No raw model ids are shown to customers."""
        if not stop:
            return "Stop"
        loc = stop.saved_location_id
        name = (loc.business_name or stop.partner_id.name or "").strip() or "Stop"
        city = (loc.city or "").strip()
        if not city and stop.address and "," in stop.address:
            city = stop.address.split(",")[1].strip()
        return f"{name} — {city}" if city else name

    def _evidence_viewer_ok(self, job, token):
        """Photos are gated behind the tracking token OR an authenticated
        partner authorized for the booking (commercial hierarchy) — even
        when the legacy page renders without a token."""
        if token:
            tokens = {job.tracking_token}
            if job.logistics_booking_id:
                tokens.add(job.logistics_booking_id.tracking_token or "")
            if token in tokens:
                return True
        user = request.env.user
        if user._is_public():
            return False
        partner = user.partner_id
        job_partner = job.partner_id
        return bool(
            partner
            and job_partner
            and partner.commercial_partner_id.id == job_partner.commercial_partner_id.id
        )

    def _tracking_pallets(self, job, tracking_token):
        """Per-pallet card data for the tracking page: ref, status, pickup /
        delivery labels and — only for an authorized viewer — that pallet's
        own pickup photos via the token-validated evidence route."""
        Evidence = request.env["prema.dispatch.evidence"]
        show_photos = self._evidence_viewer_ok(job, tracking_token)
        pallets = []
        for item in job.item_ids.sorted("sequence"):
            photos = []
            if show_photos:
                popp_evs = Evidence.sudo().search(
                    [("job_id", "=", job.id), ("evidence_type", "=", "popp"),
                     ("pallet_id", "=", item.id)],
                    order="captured_at asc, id asc",
                )
                for ev in popp_evs:
                    photos.append({
                        "url": f"/dispatch/track/{job.tracking_number}/evidence/{ev.id}?token={job.tracking_token}",
                        "name": ev.attachment_id.name or "photo",
                    })
            status = item.status
            if status == "delivered":
                label = "Delivered"
            elif status in ("loaded", "out_for_delivery", "in_transit",
                            "partially_unloaded", "cross_docked", "staged",
                            "reloaded", "transferred"):
                label = "In Transit"
            elif status == "pending" and not photos:
                label = "Pending Pickup"
            else:
                label = "Picked Up"
            allocations = item.stop_allocation_ids.filtered("active")
            delivery_labels = [
                self._stop_label(allocation.stop_id)
                for allocation in allocations
            ]
            pallets.append({
                "ref": item.name or item.item_ref or f"Pallet {item.id}",
                "status_label": label,
                "pickup_label": self._stop_label(item.pickup_stop_id),
                # A shared physical pallet can have several customer-visible
                # destinations; keep the old singular field for clients
                # that still read it and add the complete allocation list.
                "delivery_label": ", ".join(delivery_labels)
                    or self._stop_label(item.delivery_stop_id),
                "delivery_labels": delivery_labels,
                "photos": photos,
            })
        return pallets

    @http.route(
        "/dispatch/track/<string:tracking_number>/evidence/<int:evidence_id>",
        type="http", auth="public", sitemap=False,
    )
    def track_evidence_image(self, tracking_number, evidence_id, **kwargs):
        """Secure evidence-image route: serves a shipment's pickup photo only
        when the request carries the job's/booking's tracking token (or comes
        from an authenticated partner of that booking). URLs carry the
        evidence id, never a raw ir.attachment id, and other jobs' evidence
        always 404s."""
        token = (kwargs.get("token") or "").strip()
        domain = [("tracking_number", "=", tracking_number)]
        if token:
            domain.append(("tracking_token", "=", token))
        job = request.env["prema.dispatch.job"].sudo().search(domain, limit=1)
        if not job or not self._evidence_viewer_ok(job, token):
            return request.not_found()
        ev = request.env["prema.dispatch.evidence"].sudo().browse(evidence_id)
        if not ev.exists() or ev.job_id.id != job.id:
            return request.not_found()
        if ev.evidence_type not in ("popp", "pop_general"):
            return request.not_found()
        att = ev.attachment_id
        try:
            data = att.with_context(bin_size=False).raw
        except Exception:
            data = None
        if not data:
            return request.not_found()
        return request.make_response(data, headers=[
            ("Content-Type", att.mimetype or "image/jpeg"),
            ("Content-Disposition", "inline"),
            ("Cache-Control", "private, max-age=3600"),
            ("X-Content-Type-Options", "nosniff"),
        ])

    @http.route(
        "/dispatch/track/<string:tracking_number>/live",
        type="json", auth="public",
    )
    def track_live(self, tracking_number, **kwargs):
        """Privacy-filtered live status + ETA feed for the secure tracking page."""
        tracking_token = (kwargs.get("token") or "").strip()
        domain = [("tracking_number", "=", tracking_number)]
        if tracking_token:
            domain.append(("tracking_token", "=", tracking_token))
        job = request.env["prema.dispatch.job"].sudo().search(domain, limit=1)
        if not job or not tracking_token:
            return {"error": "not found"}

        vehicle = job.vehicle_id
        stops = job.stop_ids.filtered(lambda s: s.status != "cancelled").sorted("sequence")
        next_stop = next((s for s in stops if s.status in ("pending", "en_route")), None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        gps_age = None
        if vehicle and vehicle.x_last_location_at:
            gps_age = int((now - vehicle.x_last_location_at.replace(tzinfo=None)).total_seconds() / 60)
        progress = job._board_live_progress()

        return {
            "tracking_number": job.tracking_number,
            "truck_lat": vehicle.x_last_location_lat if vehicle else 0,
            "truck_lng": vehicle.x_last_location_lng if vehicle else 0,
            "gps_age_min": gps_age,
            "live_progress": progress["key"],
            "live_progress_label": progress["label"],
            "next_stop_id": next_stop.id if next_stop else None,
            "next_stop_addr": (next_stop.address or "").split(",")[0] if next_stop else "",
            "next_stop_status": next_stop.status if next_stop else "",
            "next_stop_estimated_arrival": job._dt_iso_utc(next_stop.estimated_arrival) if next_stop else None,
            "server_time": job._dt_iso_utc(now),
            "stops": [{
                "id": s.id,
                "status": s.status,
                "city": (s.address or "").split(",")[0],
                "lat": s.latitude or 0,
                "lng": s.longitude or 0,
            } for s in stops],
        }

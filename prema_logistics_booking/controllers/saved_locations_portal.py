"""Portal controller for Customer Saved Locations — CRUD + defaults + autocomplete."""

import json

from odoo import _, http
from odoo.http import request
from werkzeug.exceptions import NotFound


def _require_auth():
    """Ensure user is authenticated. Raises NotFound if public."""
    if request.env.user._is_public():
        raise NotFound()


def _get_partner():
    """Return the authenticated user's commercial partner."""
    return request.env.user.partner_id.commercial_partner_id


class LogisticsSavedLocationsPortal(http.Controller):

    # ── Autocomplete ──────────────────────────────────────────────────
    @http.route("/my/saved-locations/autocomplete", type="http", auth="user", website=True, sitemap=False)
    def autocomplete_location(self, **kwargs):
        """JSON endpoint: search known facilities by name/chain/store#."""
        _require_auth()
        partner = _get_partner()
        query = (kwargs.get("q") or "").strip()
        if len(query) < 2:
            return request.make_response(json.dumps({"results": []}),
                                         headers=[("Content-Type", "application/json")])

        results = []

        # 1. Customer's own saved locations
        Saved = request.env["logistics.saved.location"].sudo()
        own = Saved.search([
            ("commercial_partner_id", "=", partner.id),
            ("active", "=", True),
        ])
        for loc in own:
            if self._matches_query(loc, query):
                results.append(self._format_result(loc, source="saved"))

        # 2. Shared reusable master facilities (prema.dispatch.location)
        DispatchLoc = request.env["prema.dispatch.location"].sudo()
        shared = DispatchLoc.search([
            ("portal_reusable", "=", True),
            ("active", "=", True),
        ])
        for loc in shared:
            if self._matches_dispatch_query(loc, query) and len(results) < 20:
                results.append(self._format_dispatch_result(loc, source="shared"))

        return request.make_response(
            json.dumps({"results": results[:15]}),
            headers=[("Content-Type", "application/json")],
        )

    def _matches_query(self, loc, query):
        q = query.lower()
        fields_to_check = [
            loc.name or "",
            loc.chain_name or "",
            loc.business_name or "",
            loc.branch_name or "",
            loc.store_number or "",
            loc.street or "",
            loc.city or "",
        ]
        return any(q in f.lower() for f in fields_to_check if f)

    def _matches_dispatch_query(self, loc, query):
        q = query.lower()
        fields_to_check = [
            loc.name or "",
            loc.chain_name or "",
            loc.business_name or "",
            loc.branch_name or "",
            loc.location_number or "",
            loc.location_number_normalized or "",
            loc.street or "",
            loc.city or "",
        ]
        return any(q in f.lower() for f in fields_to_check if f)

    def _format_result(self, loc, source="saved"):
        return {
            "id": loc.id,
            "source": source,
            "name": loc.name or "",
            "chain": loc.chain_name or "",
            "business": loc.business_name or loc.company_name or "",
            "branch": loc.branch_name or "",
            "store_number": loc.store_number or "",
            "street": loc.street or "",
            "city": loc.city or "",
            "province": loc.state_id.code if loc.state_id else "",
            "postal_code": loc.postal_code or "",
            "place_id": loc.google_place_id or "",
            "latitude": loc.latitude or 0,
            "longitude": loc.longitude or 0,
            "dock_info": loc.dock_info or "",
            "opening_hours": loc.opening_hours or "",
            "liftgate_required": loc.liftgate_required or False,
            "forklift_available": loc.forklift_available or False,
            "contact_name": loc.contact_name or "",
            "contact_phone": loc.contact_phone or "",
            "contact_email": loc.contact_email or "",
            "pickup_instructions": loc.pickup_instructions or "",
            "delivery_instructions": loc.delivery_instructions or "",
            "appointment_required": loc.appointment_required or False,
            "location_type": loc.location_type,
        }

    def _format_dispatch_result(self, loc, source="shared"):
        # Map master stop_type → portal location_type values
        # prema.dispatch.location.stop_type uses: pickup / delivery / both
        # logistics.saved.location.location_type uses same values
        master_stop_type = loc.stop_type or "delivery"
        # Normalize: "both" in dispatch = "both" in portal
        if master_stop_type not in ("pickup", "delivery", "both"):
            master_stop_type = "delivery"

        return {
            "id": loc.id,
            "source": source,
            "dispatch_location_id": loc.id,
            "name": (loc.chain_name or "") + (" — " + (loc.branch_name or loc.city or "")) if loc.chain_name else (loc.name or ""),
            "chain": loc.chain_name or "",
            "business": loc.business_name or "",
            "branch": loc.branch_name or "",
            "store_number": loc.location_number or "",
            "street": loc.street or "",
            "city": loc.city or "",
            "province": loc.province_code or "",
            "postal_code": loc.postal_code or "",
            "place_id": loc.google_place_id or "",
            "latitude": loc.pin_lat or 0,
            "longitude": loc.pin_lng or 0,
            "dock_info": loc.dock_door or "",
            "opening_hours": "",
            "liftgate_required": loc.liftgate_required or False,
            "forklift_available": False,
            "appointment_required": False,
            # Master facility shares PHYSICAL access info only
            # (dock door, receiving/truck entrance, gate code, parking pin).
            # Contact name/phone/email and customer-specific instructions
            # belong to logistics.saved.location — never shared from master.
            "driver_instructions": loc.driver_instructions or "",
            "gate_code": loc.gate_code or "",
            "receiving_entrance": loc.receiving_entrance or "",
            "truck_entrance": loc.truck_entrance or "",
            "location_type": master_stop_type,
        }

    # ── List ──────────────────────────────────────────────────────────
    @http.route("/my/saved-locations", type="http", auth="user", website=True, sitemap=False)
    def list_locations(self, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()

        filter_type = kwargs.get("filter", "")
        domain = [("commercial_partner_id", "=", partner.id), ("active", "=", True)]
        if filter_type == "pickup":
            domain.append(("location_type", "in", ("pickup", "both")))
        elif filter_type == "delivery":
            domain.append(("location_type", "in", ("delivery", "both")))

        locations = Saved.search(domain, order="is_default_pickup DESC, is_default_delivery DESC, name")
        return request.render("prema_logistics_booking.portal_saved_locations_list", {
            "locations": locations,
            "filter_type": filter_type,
        })

    # ── Add ───────────────────────────────────────────────────────────
    @http.route("/my/saved-locations/add", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def add_location(self, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()
        error = None

        # Determine default type from query param
        default_type = kwargs.get("type", "pickup")
        if default_type not in ("pickup", "delivery", "both"):
            default_type = "pickup"
        return_to = kwargs.get("return", "")  # "booking" or empty

        if request.httprequest.method == "POST":
            google_place_id = kwargs.get("google_place_id", "").strip()
            unit = kwargs.get("unit", "").strip()

            # Duplicate detection before save
            if google_place_id:
                dup = Saved._detect_duplicate(partner.id, google_place_id, unit)
                if dup:
                    # Mark used and return to booking if requested
                    dup.mark_used()
                    if return_to == "booking":
                        return request.redirect(
                            f"/my/booking/new?new_loc_id={dup.id}&new_loc_type={dup.location_type}"
                        )
                    return request.redirect("/my/saved-locations")

            vals = {
                "commercial_partner_id": partner.id,
                "name": kwargs.get("name", "").strip(),
                "location_type": kwargs.get("location_type", default_type),
                "chain_name": kwargs.get("chain_name", "").strip(),
                "business_name": kwargs.get("business_name", "").strip(),
                "branch_name": kwargs.get("branch_name", "").strip(),
                "store_number": kwargs.get("store_number", "").strip(),
                "street": kwargs.get("street", "").strip(),
                "street2": kwargs.get("street2", "").strip(),
                "unit": unit,
                "city": kwargs.get("city", "").strip(),
                "postal_code": kwargs.get("postal_code", "").strip(),
                "state_id": int(kwargs.get("state_id", 0) or 0) or None,
                "country_id": int(kwargs.get("country_id", 0) or 0) or None,
                "contact_name": kwargs.get("contact_name", "").strip(),
                "contact_phone": kwargs.get("contact_phone", "").strip(),
                "contact_email": kwargs.get("contact_email", "").strip(),
                "dock_info": kwargs.get("dock_info", "").strip(),
                "opening_hours": kwargs.get("opening_hours", "").strip(),
                "pickup_instructions": kwargs.get("pickup_instructions", "").strip(),
                "delivery_instructions": kwargs.get("delivery_instructions", "").strip(),
                "appointment_required": kwargs.get("appointment_required") == "1",
                "liftgate_required": kwargs.get("liftgate_required") == "1",
                "forklift_available": kwargs.get("forklift_available") == "1",
                "latitude": float(kwargs.get("latitude", 0) or 0),
                "longitude": float(kwargs.get("longitude", 0) or 0),
                "google_place_id": google_place_id,
                "formatted_address": kwargs.get("formatted_address", "").strip(),
                "google_verified": bool(google_place_id),
                "branch_name_manual": kwargs.get("branch_name_manual") == "1",
                "dispatch_location_id": int(kwargs.get("dispatch_location_id", 0)) or None,
            }

            if not vals["name"]:
                error = _("Please enter a location name.")
            elif not vals["street"]:
                error = _("Please enter a street address.")

            if not error:
                try:
                    loc = Saved.create(vals)
                    # Run region detection
                    if loc.latitude and loc.longitude:
                        loc.action_resolve_region()

                    # Return to booking if requested
                    if return_to == "booking":
                        return request.redirect(
                            f"/my/booking/new?new_loc_id={loc.id}&new_loc_type={loc.location_type}"
                        )
                    return request.redirect("/my/saved-locations")
                except Exception as e:
                    error = str(e)

        # Load provinces for the form
        states = request.env["res.country.state"].sudo().search([
            ("country_id.code", "=", "CA"),
        ], order="name")

        return request.render("prema_logistics_booking.portal_saved_location_form_enhanced", {
            "error": error,
            "location": None,
            "default_type": default_type,
            "return_to": return_to,
            "states": states,
            "editing": False,
        })

    # ── Edit ──────────────────────────────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/edit", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def edit_location(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()
        loc = Saved.browse(loc_id)

        # Security: only own locations
        if not loc.exists() or loc.commercial_partner_id.id != partner.id:
            raise NotFound()

        error = None
        if request.httprequest.method == "POST":
            google_place_id = kwargs.get("google_place_id", "").strip()
            unit = kwargs.get("unit", "").strip()

            # Duplicate detection before save (exclude self)
            if google_place_id:
                dup = Saved._detect_duplicate(partner.id, google_place_id, unit)
                if dup and dup.id != loc.id:
                    dup.mark_used()
                    return request.redirect("/my/saved-locations")

            vals = {
                "name": kwargs.get("name", "").strip(),
                "location_type": kwargs.get("location_type", loc.location_type),
                "chain_name": kwargs.get("chain_name", "").strip(),
                "business_name": kwargs.get("business_name", "").strip(),
                "branch_name": kwargs.get("branch_name", "").strip(),
                "store_number": kwargs.get("store_number", "").strip(),
                "branch_name_manual": kwargs.get("branch_name_manual") == "1",
                "street": kwargs.get("street", "").strip(),
                "street2": kwargs.get("street2", "").strip(),
                "unit": unit,
                "city": kwargs.get("city", "").strip(),
                "postal_code": kwargs.get("postal_code", "").strip(),
                "contact_name": kwargs.get("contact_name", "").strip(),
                "contact_phone": kwargs.get("contact_phone", "").strip(),
                "contact_email": kwargs.get("contact_email", "").strip(),
                "dock_info": kwargs.get("dock_info", "").strip(),
                "opening_hours": kwargs.get("opening_hours", "").strip(),
                "pickup_instructions": kwargs.get("pickup_instructions", "").strip(),
                "delivery_instructions": kwargs.get("delivery_instructions", "").strip(),
                "appointment_required": kwargs.get("appointment_required") == "1",
                "liftgate_required": kwargs.get("liftgate_required") == "1",
                "forklift_available": kwargs.get("forklift_available") == "1",
                "latitude": float(kwargs.get("latitude", loc.latitude or 0) or 0),
                "longitude": float(kwargs.get("longitude", loc.longitude or 0) or 0),
                "google_place_id": google_place_id,
                "formatted_address": kwargs.get("formatted_address", "").strip(),
            }
            if not vals["name"]:
                error = _("Please enter a location name.")
            else:
                try:
                    loc.write(vals)
                    if loc.latitude and loc.longitude:
                        loc.action_resolve_region()
                    return request.redirect("/my/saved-locations")
                except Exception as e:
                    error = str(e)

        states = request.env["res.country.state"].sudo().search([
            ("country_id.code", "=", "CA"),
        ], order="name")

        return request.render("prema_logistics_booking.portal_saved_location_form_enhanced", {
            "error": error,
            "location": loc,
            "default_type": loc.location_type,
            "return_to": "",
            "states": states,
            "editing": True,
        })

    # ── Archive ───────────────────────────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/archive", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def archive_location(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()
        loc = Saved.browse(loc_id)
        if not loc.exists() or loc.commercial_partner_id.id != partner.id:
            raise NotFound()
        loc.write({"active": False})
        return request.redirect("/my/saved-locations")

    # ── Set Default ───────────────────────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/set-default-pickup", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def set_default_pickup(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()
        loc = Saved.browse(loc_id)
        if not loc.exists() or loc.commercial_partner_id.id != partner.id:
            raise NotFound()
        if loc.location_type in ("pickup", "both"):
            loc.write({"is_default_pickup": True})
        return request.redirect("/my/saved-locations")

    @http.route("/my/saved-locations/<int:loc_id>/set-default-delivery", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def set_default_delivery(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Saved = request.env["logistics.saved.location"].sudo()
        loc = Saved.browse(loc_id)
        if not loc.exists() or loc.commercial_partner_id.id != partner.id:
            raise NotFound()
        if loc.location_type in ("delivery", "both"):
            loc.write({"is_default_delivery": True})
        return request.redirect("/my/saved-locations")

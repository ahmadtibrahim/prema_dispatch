"""Portal controller for Customer Saved Locations — CRUD + defaults + autocomplete."""

import json

from odoo import _, fields, http
from odoo.http import request
from werkzeug.exceptions import NotFound

from odoo.addons.prema_logistics_booking.services.google_places_service import (
    GooglePlacesService, valid_coordinate_pair,
)

_GOOGLE_INSTRUCTION = _(
    "Please select the address from the Google address suggestions so we can "
    "verify the location.")


class _FormLocation:
    """Minimal stand-in for logistics.saved.location so the add/edit form
    re-renders with the customer's submitted values when Google
    verification fails — nothing is typed twice. Only physical/contact
    fields; refs expose .id for the template's selects."""

    _ATTRS = (
        "name", "street", "street2", "unit", "city", "postal_code",
        "chain_name", "business_name", "branch_name", "branch_name_manual",
        "store_number", "contact_name", "contact_phone", "contact_email",
        "dock_info", "opening_hours", "pickup_instructions",
        "delivery_instructions", "location_type", "timezone",
        "formatted_address", "google_place_id",
    )

    class _Ref:
        def __init__(self, rid):
            self.id = rid

        def __bool__(self):
            return self.id is not None

    def __init__(self, kwargs):
        for attr in self._ATTRS:
            setattr(self, attr, str(kwargs.get(attr, "") or ""))
        try:
            self.latitude = float(kwargs.get("latitude", 0) or 0)
            self.longitude = float(kwargs.get("longitude", 0) or 0)
        except (TypeError, ValueError):
            self.latitude, self.longitude = 0.0, 0.0
        self.appointment_required = kwargs.get("appointment_required") == "1"
        self.liftgate_required = kwargs.get("liftgate_required") == "1"
        self.forklift_available = kwargs.get("forklift_available") == "1"
        self.state_id = self._Ref(int(kwargs.get("state_id", 0) or 0) or None)
        self.country_id = self._Ref(int(kwargs.get("country_id", 0) or 0) or None)
        self.dispatch_location_id = self._Ref(
            int(kwargs.get("dispatch_location_id", 0) or 0) or None)


def _require_auth():
    """Ensure user is authenticated. Raises NotFound if public."""
    if request.env.user._is_public():
        raise NotFound()


def _get_partner():
    """Return the authenticated user's commercial partner."""
    return request.env.user.partner_id.commercial_partner_id


def _submitted_hours_by_day(kwargs):
    """Rebuild the hours_by_day context from a submitted week so the form
    keeps the customer's operating hours on a verification error."""
    out = {}
    for row in _collect_hour_rows(kwargs):
        d = row["day"]
        if row["status"] == "open_24h":
            out[d] = {"status": "open_24h", "open": "00:00", "close": "23:59"}
        elif row["status"] == "closed":
            out[d] = {"status": "closed", "open": "08:00", "close": "17:00"}
        else:
            out[d] = {"status": "custom",
                      "open": row["open"] or "08:00", "close": row["close"] or "17:00"}
    return out


def _collect_hour_rows(kwargs):
    """Collect the per-day operating-hours selects/time inputs from the
    portal form into hour_rows [{day, status, open, close}] for days
    0..6. The selects carry name="hours_status_<day>", the custom
    open/close inputs name="hours_open_<day>" / "hours_close_<day>"."""
    rows = []
    for day in range(7):
        status = str(kwargs.get(f"hours_status_{day}", "") or "open_24h").strip()
        if status not in ("closed", "open_24h", "custom"):
            status = "open_24h"
        rows.append({
            "day": day,
            "status": status,
            "open": kwargs.get(f"hours_open_{day}", "").strip(),
            "close": kwargs.get(f"hours_close_{day}", "").strip(),
        })
    return rows


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
        return self._fuzzy_match(query, [
            loc.name or "",
            loc.chain_name or "",
            loc.business_name or "",
            loc.branch_name or "",
            loc.store_number or "",
            loc.street or "",
            loc.city or "",
        ])

    def _matches_dispatch_query(self, loc, query):
        return self._fuzzy_match(query, [
            loc.name or "",
            loc.chain_name or "",
            loc.business_name or "",
            loc.branch_name or "",
            loc.location_number or "",
            loc.location_number_normalized or "",
            loc.street or "",
            loc.city or "",
        ])

    def _fuzzy_match(self, query, fields_to_check):
        """Match query against fields using prefix-of-any-word or substring match.
        'alim' matches 'Aliments', 'koyo' matches 'Koyo', 'mon' matches 'Montréal'.
        """
        q = query.lower().strip()
        if len(q) < 2:
            return False
        for field in fields_to_check:
            if not field:
                continue
            f = field.lower()
            # Exact substring match (fast path)
            if q in f:
                return True
            # Prefix match on any word in the field
            words = f.split()
            if any(w.startswith(q) for w in words):
                return True
        return False

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

    # ── Server-side Google verification ───────────────────────────────
    def _google_verify(self, kwargs):
        """Authoritative server-side verification for add/edit POSTs.

        A Google Place ID submitted by the browser is NEVER accepted on
        its own — it must resolve through the canonical Places service to
        a valid coordinate pair (0.0/0.0 is not valid; a single zero
        component is). Returns:

          {"source": "google", <resolved physical data>}   — verified
          {"source": "master", "master": <facility>}       — shared master
                                                             with valid pins
          None                                              — unverifiable

        The master branch lets customers reuse an existing Master
        Facility (its pins were validated when the facility was created);
        the master keeps its authority, nothing is re-derived.
        """
        place_id = str(kwargs.get("google_place_id", "") or "").strip()
        if place_id:
            resolved = GooglePlacesService(request.env).resolve_place(place_id)
            if resolved:
                return {"source": "google", **resolved}
            return None

        master_id = int(kwargs.get("dispatch_location_id", 0) or 0) or None
        if master_id:
            master = request.env["prema.dispatch.location"].sudo().browse(master_id)
            if master.exists() and valid_coordinate_pair(master.pin_lat, master.pin_lng):
                return {"source": "master", "master": master}
        return None

    def _google_state_country(self, province_code, country_code):
        """Resolve province/state + country codes to their res records
        (for the canonical Google result). Returns (state_id, country_id)."""
        state = False
        if province_code:
            state = request.env["res.country.state"].sudo().search([
                ("code", "=ilike", province_code),
            ], limit=1)
        country = False
        if country_code:
            country = request.env["res.country"].sudo().search([
                ("code", "=ilike", country_code),
            ], limit=1)
        return (state.id if state else False), (country.id if country else False)

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
        return request.render("prema_logistics_booking.portal_my_saved_locations", {
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
                    # Merge the newly requested usage type into the existing
                    # row (pickup + delivery → both) instead of discarding it.
                    requested_type = kwargs.get("location_type", default_type)
                    merged = Saved._merge_location_type(dup.location_type, requested_type)
                    if merged != dup.location_type:
                        dup.write({"location_type": merged})
                    # Mark used and return to booking if requested
                    dup.mark_used()
                    if return_to == "booking":
                        return request.redirect(
                            f"/my/booking/new?new_loc_id={dup.id}&new_loc_type={merged}"
                        )
                    return request.redirect("/my/saved-locations")

            # Server-side Google verification — authoritative, even when
            # the browser already supplied coordinates. A submitted Place
            # ID means nothing until Google resolves a valid coordinate
            # pair; a shared Master Facility with valid pins passes
            # through (its physical data was validated when created).
            gv = self._google_verify(kwargs)
            if not gv:
                return request.render(
                    "prema_logistics_booking.portal_saved_location_form_enhanced", {
                        "error": _GOOGLE_INSTRUCTION,
                        "location": _FormLocation(kwargs),
                        "default_type": kwargs.get("location_type", default_type),
                        "return_to": return_to,
                        "states": request.env["res.country.state"].sudo().search([
                            ("country_id.code", "=", "CA"),
                        ], order="name"),
                        "editing": False,
                        "google_api_key": request.env["ir.config_parameter"].sudo()
                        .get_param("google_maps_api_key", ""),
                        "hours_by_day": _submitted_hours_by_day(kwargs),
                    })

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
                "branch_name_manual": kwargs.get("branch_name_manual") == "1",
                # Structured operating hours: timezone + weekly schedule.
                "timezone": kwargs.get("timezone", "").strip() or "America/Toronto",
            }

            if gv["source"] == "google":
                # Canonical Google result is the physical authority: the
                # browser may have sent its own coordinates, but the
                # resolved Place data wins — never trust browser
                # coordinates when a Place ID is present.
                state_id, country_id = self._google_state_country(
                    gv.get("province_code"), gv.get("country_code"))
                vals.update({
                    "google_place_id": gv["place_id"],
                    "latitude": gv["latitude"],
                    "longitude": gv["longitude"],
                    "formatted_address": gv.get("formatted_address") or "",
                    "street": gv.get("street") or vals["street"],
                    "city": gv.get("city") or vals["city"],
                    "postal_code": gv.get("postal_code") or vals["postal_code"],
                    "state_id": state_id or vals["state_id"],
                    "country_id": country_id or vals["country_id"],
                    "google_verified": True,
                    "google_verified_at": fields.Datetime.now(),
                })
            else:
                # Shared Master Facility: link it; its pins are the
                # physical authority (the sync hook copies them onto this
                # record) — never re-derive or overwrite the master.
                master = gv["master"]
                vals["dispatch_location_id"] = master.id
                vals["latitude"] = master.pin_lat
                vals["longitude"] = master.pin_lng
                if master.google_place_id and not vals.get("google_place_id"):
                    vals["google_place_id"] = master.google_place_id
                if master.google_verified:
                    vals["google_verified"] = True

            if not vals["name"]:
                error = _("Please enter a location name.")
            elif not vals["street"]:
                error = _("Please enter a street address.")

            if not error:
                try:
                    loc = Saved.create(vals)
                    # Persist the submitted weekly operating schedule — the
                    # portal form submits the WHOLE week, so this replaces
                    # the general-scope rows wholesale.
                    loc.sync_portal_hours(_collect_hour_rows(kwargs))
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

        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")

        return request.render("prema_logistics_booking.portal_saved_location_form_enhanced", {
            "error": error,
            "location": None,
            "default_type": default_type,
            "return_to": return_to,
            "states": states,
            "editing": False,
            "google_api_key": api_key,
            # Fresh form: portal defaults (weekdays open_24h, weekends closed).
            "hours_by_day": {
                day: {"status": "open_24h" if day < 5 else "closed",
                      "open": "08:00", "close": "17:00"}
                for day in range(7)
            },
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
                    # Merge the requested usage type into the existing row
                    # (pickup + delivery → both) instead of discarding it.
                    requested_type = kwargs.get("location_type", loc.location_type)
                    merged = Saved._merge_location_type(dup.location_type, requested_type)
                    if merged != dup.location_type:
                        dup.write({"location_type": merged})
                    dup.mark_used()
                    return request.redirect("/my/saved-locations")

            # Same server-side Google verification as add. An edit that
            # keeps an ALREADY verified location intact (its hidden google
            # fields are prefilled from the record, so its Place ID
            # resolves again) passes normally; an unverified location must
            # be re-verified from Google before it can be saved.
            gv = self._google_verify(kwargs)
            if not gv:
                master = loc.dispatch_location_id
                still_valid = loc.google_verified and (
                    (master and valid_coordinate_pair(master.pin_lat, master.pin_lng))
                    or valid_coordinate_pair(loc.latitude, loc.longitude))
                if not still_valid:
                    return request.render(
                        "prema_logistics_booking.portal_saved_location_form_enhanced", {
                            "error": _GOOGLE_INSTRUCTION,
                            "location": _FormLocation(kwargs),
                            "default_type": kwargs.get("location_type", loc.location_type),
                            "return_to": "",
                            "states": request.env["res.country.state"].sudo().search([
                                ("country_id.code", "=", "CA"),
                            ], order="name"),
                            "editing": True,
                            "google_api_key": request.env["ir.config_parameter"].sudo()
                            .get_param("google_maps_api_key", ""),
                            "hours_by_day": _submitted_hours_by_day(kwargs),
                        })

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
                "timezone": kwargs.get("timezone", "").strip() or loc.timezone or "America/Toronto",
            }

            if gv and gv["source"] == "google":
                # Canonical Google result wins over any browser-supplied
                # coordinates/address fields.
                state_id, country_id = self._google_state_country(
                    gv.get("province_code"), gv.get("country_code"))
                vals.update({
                    "google_place_id": gv["place_id"],
                    "latitude": gv["latitude"],
                    "longitude": gv["longitude"],
                    "formatted_address": gv.get("formatted_address") or "",
                    "street": gv.get("street") or vals["street"],
                    "city": gv.get("city") or vals["city"],
                    "postal_code": gv.get("postal_code") or vals["postal_code"],
                    "state_id": state_id or loc.state_id.id,
                    "country_id": country_id or loc.country_id.id,
                    "google_verified": True,
                    "google_verified_at": fields.Datetime.now(),
                })
            elif gv and gv["source"] == "master":
                master = gv["master"]
                vals["dispatch_location_id"] = master.id
                vals["latitude"] = master.pin_lat
                vals["longitude"] = master.pin_lng
                if master.google_place_id and not vals.get("google_place_id"):
                    vals["google_place_id"] = master.google_place_id
                if master.google_verified:
                    vals["google_verified"] = True

            if not vals["name"]:
                error = _("Please enter a location name.")
            else:
                try:
                    loc.write(vals)
                    # Persist the submitted weekly operating schedule
                    # (whole-week replace, general scope).
                    loc.sync_portal_hours(_collect_hour_rows(kwargs))
                    if loc.latitude and loc.longitude:
                        loc.action_resolve_region()
                    return request.redirect("/my/saved-locations")
                except Exception as e:
                    error = str(e)

        states = request.env["res.country.state"].sudo().search([
            ("country_id.code", "=", "CA"),
        ], order="name")

        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")

        # Load the persisted weekly schedule back so the edit form shows
        # the stored authority (not the form defaults).
        hours_ctx = loc.hours_context_dict()
        return request.render("prema_logistics_booking.portal_saved_location_form_enhanced", {
            "error": error,
            "location": loc,
            "default_type": loc.location_type,
            "return_to": "",
            "states": states,
            "editing": True,
            "google_api_key": api_key,
            "hours_by_day": hours_ctx["hours_by_day"],
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

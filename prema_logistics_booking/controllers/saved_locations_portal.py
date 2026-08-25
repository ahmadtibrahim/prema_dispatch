"""Portal controller for customer facility access — SAVED LOCATION
CONSOLIDATION (§9–§12).

Every route here operates on the canonical pair:
  - prema.dispatch.location            = the physical facility (one per
    building; shared by all customers using it)
  - logistics.location.customer.access = THIS customer's private data for
    that facility (alias, contact, instructions, defaults, capabilities)

logistics.saved.location is NEVER created or modified by this controller.
Legacy rows remain readable historically, but new portal data — add, edit,
archive, defaults — lives exclusively on access rows.

Privacy hard rule (§17): another customer's private fields (contact,
instructions, references, defaults) are never returned to this customer.
The autocomplete and the booking payloads emit PHYSICAL facility data for
facilities the customer does not have access to, and private data only for
the customer's OWN access rows.
"""

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


class _Ref:
    """Minimal .id-bearing stand-in for M2O fields in view models."""

    def __init__(self, rid):
        self.id = rid

    def __bool__(self):
        return self.id is not None


def _parse_bool(val):
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _time_float(val):
    """'HH:MM' (or float) → float hours, or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        parts = str(val).strip().split(":")
        return int(parts[0]) + int(parts[1]) / 60.0
    except (ValueError, IndexError):
        return None


def _float_hhmm(value):
    """float hours → 'HH:MM' string (canonical hours → form time inputs)."""
    if value is None:
        return "08:00"
    v = float(value)
    h = int(v)
    m = int(round((v - h) * 60)) % 60
    return "%02d:%02d" % (h, m)


class _FormLocation:
    """View-model for re-rendering the add/edit form with the customer's
    submitted values when Google verification fails — nothing is typed
    twice. Merges the form's physical + private fields."""

    _ATTRS = (
        "name", "street", "street2", "unit", "city", "postal_code",
        "chain_name", "business_name", "branch_name", "branch_name_manual",
        "store_number", "contact_name", "contact_phone", "contact_email",
        "dock_info", "opening_hours", "pickup_instructions",
        "delivery_instructions", "location_type", "timezone",
        "formatted_address", "google_place_id",
    )

    def __init__(self, kwargs):
        for attr in self._ATTRS:
            setattr(self, attr, str(kwargs.get(attr, "") or ""))
        try:
            self.latitude = float(kwargs.get("latitude", 0) or 0)
            self.longitude = float(kwargs.get("longitude", 0) or 0)
        except (TypeError, ValueError):
            self.latitude, self.longitude = 0.0, 0.0
        self.appointment_required = _parse_bool(kwargs.get("appointment_required"))
        self.liftgate_required = _parse_bool(kwargs.get("liftgate_required"))
        self.forklift_available = _parse_bool(kwargs.get("forklift_available"))
        self.branch_name_manual = _parse_bool(kwargs.get("branch_name_manual"))
        self.state_id = _Ref(int(kwargs.get("state_id", 0) or 0) or None)
        self.country_id = _Ref(int(kwargs.get("country_id", 0) or 0) or None)
        self.dispatch_location_id = _Ref(
            int(kwargs.get("dispatch_location_id", 0) or 0) or None)


class _EditLocation:
    """View-model for the edit GET: facility PHYSICAL + access PRIVATE."""

    def __init__(self, access):
        fac = access.facility_id
        self.id = access.id
        self.name = access.customer_alias or fac.name or fac.chain_name or ""
        self.chain_name = fac.chain_name or ""
        self.business_name = fac.business_name or ""
        self.branch_name = fac.branch_name or ""
        self.branch_name_manual = False
        self.store_number = fac.location_number or ""
        self.street = fac.street or ""
        self.street2 = fac.street2 or ""
        self.unit = fac.unit or ""
        self.city = fac.city or ""
        self.postal_code = fac.postal_code or ""
        state = False
        if fac.province_code:
            state = request.env["res.country.state"].sudo().search([
                ("code", "=ilike", fac.province_code),
            ], limit=1)
        self.state_id = _Ref(state.id if state else 0)
        self.google_place_id = fac.google_place_id or ""
        self.formatted_address = fac.address or fac.street or ""
        self.latitude = fac.pin_lat or 0.0
        self.longitude = fac.pin_lng or 0.0
        self.dispatch_location_id = _Ref(fac.id)
        self.location_type = access.location_type or "both"
        self.contact_name = access.contact_name or ""
        self.contact_phone = access.contact_phone or ""
        self.contact_email = access.contact_email or ""
        self.dock_info = fac.dock_door or ""
        self.opening_hours = access.opening_hours or ""
        self.appointment_required = access.appointment_required
        self.liftgate_required = fac.liftgate_required
        self.forklift_available = access.forklift_available
        self.branch_name_manual = False
        self.pickup_instructions = access.pickup_instructions or ""
        self.delivery_instructions = access.delivery_instructions or ""
        self.timezone = access.timezone or "America/Toronto"
        self.is_default_pickup = access.is_default_pickup
        self.is_default_delivery = access.is_default_delivery


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

    # ── Autocomplete (§11) ─────────────────────────────────────────────
    @http.route("/my/saved-locations/autocomplete", type="http", auth="user", website=True, sitemap=False)
    def autocomplete_location(self, **kwargs):
        """JSON endpoint: search CANONICAL facilities only
        (prema.dispatch.location). This customer's own access rows may
        contribute their PRIVATE data (it is theirs); every other facility
        contributes PHYSICAL data only — another customer's contact,
        instructions, references or defaults are never returned."""
        _require_auth()
        partner = _get_partner()
        query = (kwargs.get("q") or "").strip()
        if len(query) < 2:
            return request.make_response(json.dumps({"results": []}),
                                         headers=[("Content-Type", "application/json")])

        results = []

        # 1. This customer's own facilities (private data allowed)
        Access = request.env["logistics.location.customer.access"].sudo()
        own = Access.search([
            ("commercial_partner_id", "=", partner.id),
            ("active", "=", True),
            ("portal_enabled", "=", True),
        ])
        for acc in own:
            fac = acc.facility_id
            if self._matches_dispatch_query(fac, query):
                results.append(self._format_own_access(acc))

        # 2. Public reusable facilities — PHYSICAL data only
        DispatchLoc = request.env["prema.dispatch.location"].sudo()
        shared = DispatchLoc.search([
            ("portal_reusable", "=", True),
            ("active", "=", True),
        ])
        for loc in shared:
            if self._matches_dispatch_query(loc, query) and len(results) < 20:
                results.append(self._format_dispatch_result(loc))

        return request.make_response(
            json.dumps({"results": results[:15]}),
            headers=[("Content-Type", "application/json")],
        )

    def _matches_dispatch_query(self, loc, query):
        # Access rows expose the store number as `store_number` (facility
        # physical proxy), legacy saved rows as `location_number` — read
        # both so the union is matchable either way.
        number = (loc.location_number if hasattr(loc, "location_number") else "")
        number = number or (loc.store_number if hasattr(loc, "store_number") else "")
        return self._fuzzy_match(query, [
            loc.name or "",
            loc.chain_name or "",
            loc.business_name or "",
            loc.branch_name or "",
            number or "",
            loc.street or "",
            loc.city or "",
        ])

    def _fuzzy_match(self, query, fields_to_check):
        """Match query against fields using prefix-of-any-word or substring
        match. 'alim' matches 'Aliments', 'koyo' matches 'Koyo', 'mon'
        matches 'Montréal'."""
        q = query.lower().strip()
        if len(q) < 2:
            return False
        for field in fields_to_check:
            if not field:
                continue
            f = field.lower()
            if q in f:
                return True
            words = f.split()
            if any(w.startswith(q) for w in words):
                return True
        return False

    def _format_own_access(self, acc):
        """Own access row: facility physical + THIS customer's private
        data (alias as name, contact, instructions, preferences)."""
        fac = acc.facility_id
        master_stop_type = fac.stop_type or "delivery"
        if master_stop_type not in ("pickup", "delivery", "both"):
            master_stop_type = "delivery"
        return {
            "id": acc.id,
            "source": "saved",
            "dispatch_location_id": fac.id,
            "name": acc.customer_alias or fac.name or fac.chain_name or "",
            "chain": fac.chain_name or "",
            "business": fac.business_name or "",
            "branch": fac.branch_name or "",
            "store_number": fac.location_number or "",
            "street": fac.street or "",
            "city": fac.city or "",
            "province": fac.province_code or "",
            "postal_code": fac.postal_code or "",
            "place_id": fac.google_place_id or "",
            "latitude": fac.pin_lat or 0,
            "longitude": fac.pin_lng or 0,
            "dock_info": fac.dock_door or "",
            "opening_hours": acc.opening_hours or "",
            "liftgate_required": fac.liftgate_required or False,
            "forklift_available": acc.forklift_available or False,
            "contact_name": acc.contact_name or "",
            "contact_phone": acc.contact_phone or "",
            "contact_email": acc.contact_email or "",
            "pickup_instructions": acc.pickup_instructions or "",
            "delivery_instructions": acc.delivery_instructions or "",
            "appointment_required": acc.appointment_required or False,
            "location_type": acc.location_type,
        }

    def _format_dispatch_result(self, loc, source="shared"):
        """Public reusable facility: PHYSICAL data only — never another
        customer's contact/instructions/references/defaults (§11/§17)."""
        master_stop_type = loc.stop_type or "delivery"
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
            # Master facility shares PHYSICAL access info only (dock door,
            # receiving/truck entrance, gate code, parking pin). Contact
            # name/phone/email and customer-specific instructions belong to
            # the access rows — never shared from the master.
            "driver_instructions": loc.driver_instructions or "",
            "gate_code": loc.gate_code or "",
            "receiving_entrance": loc.receiving_entrance or "",
            "truck_entrance": loc.truck_entrance or "",
            "location_type": master_stop_type,
        }

    # ── Server-side Google verification (§16) ──────────────────────────
    def _google_verify(self, kwargs):
        """Authoritative server-side verification for add/edit POSTs.

        A Google Place ID submitted by the browser is NEVER accepted on
        its own — it must resolve through the canonical Places service to
        a valid coordinate pair (0.0/0.0 is not valid). Returns:

          {"source": "google", <resolved physical data>}   — verified
          {"source": "master", "master": <facility>}       — existing
                                                             facility with
                                                             valid pins
          None                                              — unverifiable
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

    # ── Facility find-or-create (§4 dedupe priority) ───────────────────
    def _resolve_facility(self, gv, unit, vals):
        """Dedupe priority: Google Place ID + normalized unit → normalized
        address + unit hash → create. Never a fuzzy business-name merge.
        Returns (facility, created_bool)."""
        DispatchLoc = request.env["prema.dispatch.location"].sudo()
        if gv["source"] == "master":
            return gv["master"], False

        place_id = (gv.get("place_id") or "").strip()
        norm_unit = DispatchLoc._normalize_unit(unit or "")
        facility = DispatchLoc.browse()
        if place_id:
            facility = DispatchLoc.search([
                ("google_place_id", "=", place_id),
                ("active", "=", True),
            ], limit=1)
        if not facility and (place_id or (gv.get("street") and gv.get("city"))):
            import hashlib
            raw_addr = " ".join(p for p in [
                gv.get("street") or "", gv.get("city") or "",
                gv.get("province_code") or "",
                DispatchLoc._normalize_postal(gv.get("postal_code") or ""),
                gv.get("country_code") or "",
            ] if p)
            normalized = DispatchLoc._normalize_address_street(raw_addr)
            if normalized:
                facility = DispatchLoc.search([
                    ("normalized_address_hash", "=",
                     hashlib.sha256(normalized.encode()).hexdigest()),
                    ("normalized_unit", "=", norm_unit),
                    ("active", "=", True),
                ], limit=1)
        if facility:
            return facility, False

        state_id, country_id = self._google_state_country(
            gv.get("province_code"), gv.get("country_code"))
        facility = DispatchLoc.create({
            "name": vals.get("name") or gv.get("street") or "",
            "address": gv.get("formatted_address") or ", ".join(p for p in [
                gv.get("street") or "", gv.get("city") or "",
            ] if p) or vals.get("name") or "",
            "street": gv.get("street") or vals.get("street") or "",
            "street2": vals.get("street2") or "",
            "unit": unit or "",
            "city": gv.get("city") or vals.get("city") or "",
            "province_code": gv.get("province_code") or "",
            "country_id": country_id or vals.get("country_id") or False,
            "pin_lat": gv.get("latitude") or 0.0,
            "pin_lng": gv.get("longitude") or 0.0,
            "google_place_id": place_id or "",
            "google_verified": bool(place_id),
            "partner_id": vals.get("partner_id") or False,
            "business_name": vals.get("business_name") or "",
            "chain_name": vals.get("chain_name") or "",
            "branch_name": vals.get("branch_name") or "",
            "location_number": vals.get("store_number") or "",
            "postal_code": gv.get("postal_code") or vals.get("postal_code") or "",
            "stop_type": vals.get("stop_type") or "delivery",
            "dock_door": vals.get("dock_info") or "",
            "liftgate_required": bool(vals.get("liftgate_required")),
        })
        return facility, True

    # ── Canonical facility hours ───────────────────────────────────────
    def _write_facility_hours(self, facility, hour_rows):
        """Whole-week replace of the general-scope canonical hours
        (prema.dispatch.location.hours). Only called for a facility this
        customer may claim — newly created by their add/edit, or solo."""
        CanH = request.env["prema.dispatch.location.hours"].sudo()
        old = CanH.search([
            ("facility_id", "=", facility.id),
            ("service_scope", "=", "general"),
            ("active", "=", True),
        ])
        if old:
            old.write({"active": False})
        for row in hour_rows or []:
            status = (row.get("status") or "open_24h").strip()
            if status not in ("closed", "open_24h", "custom"):
                status = "open_24h"
            open_t = _time_float(row.get("open")) if status == "custom" else 0.0
            close_t = _time_float(row.get("close")) if status == "custom" else 24.0
            if status == "closed":
                open_t, close_t = 0.0, 0.0
            CanH.create({
                "facility_id": facility.id,
                "day_of_week": str(int(row.get("day", 0)) % 7),
                "service_scope": "general",
                "status": status,
                "open_time": open_t or 0.0,
                "close_time": close_t or 24.0,
                "sequence": 10,
                "active": True,
            })
        facility.write({"hours_review_required": False})
        return True

    def _facility_hours_by_day(self, facility):
        """{day: {status, open, close}} from the canonical general-scope
        rows (empty facility → portal defaults)."""
        CanH = request.env["prema.dispatch.location.hours"].sudo()
        rows = CanH.search([
            ("facility_id", "=", facility.id),
            ("service_scope", "=", "general"),
            ("active", "=", True),
        ])
        out = {}
        for day in range(7):
            day_rows = rows.filtered(lambda r, d=str(day): r.day_of_week == d)
            row = day_rows[:1]
            if not row:
                out[day] = {"status": "open_24h" if day < 5 else "closed",
                            "open": "08:00", "close": "17:00"}
            elif row.status == "closed":
                out[day] = {"status": "closed", "open": "08:00", "close": "17:00"}
            elif row.status == "open_24h":
                out[day] = {"status": "open_24h", "open": "00:00", "close": "23:59"}
            else:
                out[day] = {"status": "custom",
                            "open": _float_hhmm(row.open_time),
                            "close": _float_hhmm(row.close_time)}
        return out

    # ── List (§9) ──────────────────────────────────────────────────────
    @http.route("/my/saved-locations", type="http", auth="user", website=True, sitemap=False)
    def list_locations(self, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()

        filter_type = kwargs.get("filter", "")
        domain = [("commercial_partner_id", "=", partner.id), ("active", "=", True)]
        if filter_type == "pickup":
            domain.append(("can_pickup", "=", True))
        elif filter_type == "delivery":
            domain.append(("can_delivery", "=", True))

        locations = Access.search(
            domain, order="is_default_pickup DESC, is_default_delivery DESC, facility_id")
        return request.render("prema_logistics_booking.portal_my_saved_locations", {
            "locations": locations,
            "filter_type": filter_type,
        })

    # ── Add (§10) ──────────────────────────────────────────────────────
    @http.route("/my/saved-locations/add", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def add_location(self, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()
        error = None

        # Determine default type from query param
        default_type = kwargs.get("type", "pickup")
        if default_type not in ("pickup", "delivery", "both"):
            default_type = "pickup"
        return_to = kwargs.get("return", "")  # "booking" or empty

        if request.httprequest.method == "POST":
            google_place_id = kwargs.get("google_place_id", "").strip()
            unit = kwargs.get("unit", "").strip()

            # Server-side Google verification — authoritative, even when
            # the browser already supplied coordinates (§16).
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

            name = kwargs.get("name", "").strip()
            if not name:
                error = _("Please enter a location name.")
            elif not (kwargs.get("street") or "").strip() and not (gv.get("street") or "").strip():
                error = _("Please enter a street address.")

            location_type = kwargs.get("location_type", default_type)
            if location_type not in ("pickup", "delivery", "both"):
                location_type = "delivery"

            if not error:
                try:
                    # Find-or-create the canonical facility (§4 priority:
                    # Place ID + unit → address + unit → create; NO fuzzy
                    # business-name merges).
                    vals = {
                        "partner_id": partner.id,
                        "name": name,
                        "street2": kwargs.get("street2", "").strip(),
                        "unit": unit,
                        "business_name": kwargs.get("business_name", "").strip(),
                        "chain_name": kwargs.get("chain_name", "").strip(),
                        "branch_name": kwargs.get("branch_name", "").strip(),
                        "store_number": kwargs.get("store_number", "").strip(),
                        "stop_type": location_type,
                        "dock_info": kwargs.get("dock_info", "").strip(),
                        "liftgate_required": _parse_bool(kwargs.get("liftgate_required")),
                    }
                    state_id = int(kwargs.get("state_id", 0) or 0) or None
                    if state_id:
                        state = request.env["res.country.state"].sudo().browse(state_id)
                        if state.exists():
                            gv.setdefault("province_code", state.code or "")
                            gv.setdefault("country_code", state.country_id.code or "")
                    facility, created = self._resolve_facility(gv, unit, vals)

                    # Access row ONLY — never a logistics.saved.location.
                    access = Access.ensure_access(
                        facility, partner,
                        portal_enabled=True,
                        can_pickup=location_type in ("pickup", "both"),
                        can_delivery=location_type in ("delivery", "both"),
                        customer_alias=name,
                        contact_name=kwargs.get("contact_name", "").strip(),
                        contact_phone=kwargs.get("contact_phone", "").strip(),
                        contact_email=kwargs.get("contact_email", "").strip(),
                        pickup_instructions=kwargs.get("pickup_instructions", "").strip(),
                        delivery_instructions=kwargs.get("delivery_instructions", "").strip(),
                        appointment_required=_parse_bool(kwargs.get("appointment_required")),
                        forklift_available=_parse_bool(kwargs.get("forklift_available")),
                        opening_hours=kwargs.get("opening_hours", "").strip(),
                        timezone=kwargs.get("timezone", "").strip() or "America/Toronto",
                    )
                    # Hours are claimed only on a NEWLY created facility —
                    # an existing facility keeps its canonical schedule
                    # (one customer must never clobber a shared schedule).
                    if created:
                        self._write_facility_hours(facility, _collect_hour_rows(kwargs))
                    access.mark_used()

                    if return_to == "booking":
                        return request.redirect(
                            f"/my/booking/new?new_loc_id={access.id}&new_loc_type={location_type}"
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

    # ── Edit (§12) ─────────────────────────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/edit", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def edit_location(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()
        acc = Access.browse(loc_id)

        # Security: only own access rows
        if not acc.exists() or acc.commercial_partner_id.id != partner.id:
            raise NotFound()

        error = None
        if request.httprequest.method == "POST":
            unit = kwargs.get("unit", "").strip()

            # Same server-side verification as add. An edit that keeps an
            # ALREADY verified facility intact (hidden google fields
            # prefilled from the record) passes normally.
            gv = self._google_verify(kwargs)
            if not gv:
                fac = acc.facility_id
                still_valid = fac.google_verified and valid_coordinate_pair(
                    fac.pin_lat, fac.pin_lng)
                if not still_valid:
                    return request.render(
                        "prema_logistics_booking.portal_saved_location_form_enhanced", {
                            "error": _GOOGLE_INSTRUCTION,
                            "location": _FormLocation(kwargs),
                            "default_type": kwargs.get("location_type", acc.location_type),
                            "return_to": "",
                            "states": request.env["res.country.state"].sudo().search([
                                ("country_id.code", "=", "CA"),
                            ], order="name"),
                            "editing": True,
                            "google_api_key": request.env["ir.config_parameter"].sudo()
                            .get_param("google_maps_api_key", ""),
                            "hours_by_day": _submitted_hours_by_day(kwargs),
                        })

            location_type = kwargs.get("location_type", acc.location_type)
            if location_type not in ("pickup", "delivery", "both"):
                location_type = "delivery"

            # §12 — private fields → access row ONLY
            access_vals = {
                "customer_alias": kwargs.get("name", "").strip() or acc.customer_alias,
                "can_pickup": location_type in ("pickup", "both"),
                "can_delivery": location_type in ("delivery", "both"),
                "contact_name": kwargs.get("contact_name", "").strip(),
                "contact_phone": kwargs.get("contact_phone", "").strip(),
                "contact_email": kwargs.get("contact_email", "").strip(),
                "pickup_instructions": kwargs.get("pickup_instructions", "").strip(),
                "delivery_instructions": kwargs.get("delivery_instructions", "").strip(),
                "appointment_required": _parse_bool(kwargs.get("appointment_required")),
                "forklift_available": _parse_bool(kwargs.get("forklift_available")),
                "opening_hours": kwargs.get("opening_hours", "").strip(),
                "timezone": kwargs.get("timezone", "").strip() or acc.timezone or "America/Toronto",
            }
            acc.write(access_vals)

            facility = acc.facility_id
            other_customers = Access.search_count([
                ("facility_id", "=", facility.id),
                ("active", "=", True),
                ("commercial_partner_id", "!=", partner.id),
            ])

            # §12 — physical edit policy
            identity_changed = self._physical_identity_changed(facility, gv, unit, kwargs)
            if identity_changed:
                if other_customers == 0:
                    # Solo-customer facility → controlled canonical update.
                    self._write_facility_physical(facility, gv, unit, kwargs)
                    self._write_facility_hours(facility, _collect_hour_rows(kwargs))
                else:
                    # Shared facility → resolve the NEW Google address to a
                    # (possibly new) facility and MOVE ONLY this customer's
                    # access; the old facility keeps the other customers.
                    new_fac, created = self._resolve_facility(gv, unit, {
                        "partner_id": partner.id,
                        "name": kwargs.get("name", "").strip() or facility.name or "",
                        "street2": kwargs.get("street2", "").strip(),
                        "business_name": kwargs.get("business_name", "").strip(),
                        "chain_name": kwargs.get("chain_name", "").strip(),
                        "branch_name": kwargs.get("branch_name", "").strip(),
                        "store_number": kwargs.get("store_number", "").strip(),
                        "stop_type": location_type,
                        "dock_info": kwargs.get("dock_info", "").strip(),
                        "liftgate_required": _parse_bool(kwargs.get("liftgate_required")),
                    })
                    if new_fac.id != facility.id:
                        acc.write({"facility_id": new_fac.id})
                        facility = new_fac
                    if created:
                        self._write_facility_hours(facility, _collect_hour_rows(kwargs))
            else:
                # Non-identity physical fields: solo facilities accept the
                # canonical update; SHARED facilities keep the master's
                # physical data as the authority — one customer never
                # rewrites a shared facility's dock/liftgate/hours. Their
                # private fields were already written above.
                if other_customers == 0:
                    self._write_facility_physical(facility, gv, unit, kwargs)
                    self._write_facility_hours(facility, _collect_hour_rows(kwargs))

            if not kwargs.get("name", "").strip():
                error = _("Please enter a location name.")
            else:
                return request.redirect("/my/saved-locations")

        states = request.env["res.country.state"].sudo().search([
            ("country_id.code", "=", "CA"),
        ], order="name")

        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")

        # Facility canonical hours → form week. While a facility has NO
        # canonical hours rows at all (pre-migration legacy window), the
        # linked legacy saved location's schedule is shown instead.
        hours_by_day = self._facility_hours_by_day(acc.facility_id)
        if not request.env["prema.dispatch.location.hours"].sudo().search_count([
                ("facility_id", "=", acc.facility_id.id)]):
            saved = request.env["logistics.saved.location"].sudo().search([
                ("dispatch_location_id", "=", acc.facility_id.id),
                ("active", "=", True)], limit=1)
            if saved:
                hours_by_day = saved.hours_context_dict().get(
                    "hours_by_day", hours_by_day)

        return request.render("prema_logistics_booking.portal_saved_location_form_enhanced", {
            "error": error,
            "location": _EditLocation(acc),
            "default_type": acc.location_type,
            "return_to": "",
            "states": states,
            "editing": True,
            "google_api_key": api_key,
            "hours_by_day": hours_by_day,
        })

    def _physical_identity_changed(self, facility, gv, unit, kwargs):
        """Did the submitted PHYSICAL IDENTITY (place id or address+unit)
        move the facility? Shared facilities follow the §12 move rule on
        True; non-identity changes never move a facility."""
        DispatchLoc = request.env["prema.dispatch.location"].sudo()
        if gv["source"] == "master":
            return gv["master"].id != facility.id
        new_place = (gv.get("place_id") or "").strip()
        if new_place and new_place != (facility.google_place_id or "").strip():
            return True
        new_street = ((gv.get("street") or kwargs.get("street", "") or "").strip()
                      or (facility.street or "").strip())
        new_city = ((gv.get("city") or kwargs.get("city", "") or "").strip()
                    or (facility.city or "").strip())
        new_unit = DispatchLoc._normalize_unit(unit or "")
        new_postal = DispatchLoc._normalize_postal(
            (kwargs.get("postal_code") or facility.postal_code or "").strip())
        return (
            new_street != (facility.street or "").strip()
            or new_city != (facility.city or "").strip()
            or new_unit != DispatchLoc._normalize_unit(facility.unit or "")
            or new_postal != DispatchLoc._normalize_postal(facility.postal_code or "")
        )

    def _write_facility_physical(self, facility, gv, unit, kwargs):
        """Controlled canonical update of the facility's physical data
        (solo facilities / newly claimed facilities only)."""
        vals = {
            "street": facility.street,
            "street2": kwargs.get("street2", "").strip() or facility.street2,
            "unit": unit or facility.unit,
            "city": facility.city,
            "postal_code": facility.postal_code,
            "chain_name": kwargs.get("chain_name", "").strip() or facility.chain_name,
            "business_name": kwargs.get("business_name", "").strip() or facility.business_name,
            "branch_name": kwargs.get("branch_name", "").strip() or facility.branch_name,
            "location_number": kwargs.get("store_number", "").strip() or facility.location_number,
            "stop_type": kwargs.get("location_type", facility.stop_type),
            "dock_door": kwargs.get("dock_info", "").strip() or facility.dock_door,
            "liftgate_required": _parse_bool(kwargs.get("liftgate_required"))
            if kwargs.get("liftgate_required") is not None else facility.liftgate_required,
        }
        if gv["source"] == "google":
            vals.update({
                "google_place_id": (gv.get("place_id") or "").strip()
                or facility.google_place_id,
                "address": gv.get("formatted_address") or facility.address,
                "street": gv.get("street") or vals["street"],
                "street2": kwargs.get("street2", "").strip() or facility.street2,
                "city": gv.get("city") or vals["city"],
                "postal_code": gv.get("postal_code") or vals["postal_code"],
                "province_code": gv.get("province_code") or facility.province_code,
                "country_id": self._google_state_country(
                    gv.get("province_code"), gv.get("country_code"))[1]
                or facility.country_id.id,
                "pin_lat": gv.get("latitude") or facility.pin_lat,
                "pin_lng": gv.get("longitude") or facility.pin_lng,
                "google_verified": True,
            })
        facility.write(vals)
        return facility

    # ── Archive (§6: no hard deletes) ─────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/archive", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def archive_location(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()
        acc = Access.browse(loc_id)
        if not acc.exists() or acc.commercial_partner_id.id != partner.id:
            raise NotFound()
        # Deactivate the ACCESS row only — the facility (and any other
        # customers' rows) are untouched.
        acc.write({"active": False})
        return request.redirect("/my/saved-locations")

    # ── Set Default ───────────────────────────────────────────────────
    @http.route("/my/saved-locations/<int:loc_id>/set-default-pickup", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def set_default_pickup(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()
        acc = Access.browse(loc_id)
        if not acc.exists() or acc.commercial_partner_id.id != partner.id:
            raise NotFound()
        if acc.can_pickup:
            acc.write({"is_default_pickup": True})
        return request.redirect("/my/saved-locations")

    @http.route("/my/saved-locations/<int:loc_id>/set-default-delivery", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def set_default_delivery(self, loc_id, **kwargs):
        _require_auth()
        partner = _get_partner()
        Access = request.env["logistics.location.customer.access"].sudo()
        acc = Access.browse(loc_id)
        if not acc.exists() or acc.commercial_partner_id.id != partner.id:
            raise NotFound()
        if acc.can_delivery:
            acc.write({"is_default_delivery": True})
        return request.redirect("/my/saved-locations")

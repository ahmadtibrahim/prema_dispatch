import json

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from werkzeug.exceptions import NotFound

def _portal_enabled():
    val = request.env["ir.config_parameter"].sudo().get_param("logistics_booking.portal_enabled")
    return str(val).strip().lower() in ("true", "1")


def _is_beta_tester():
    user = request.env.user
    if user._is_public():
        return False
    return user.has_group("prema_logistics_booking.group_booking_beta_tester")


def require_visible():
    """First line of every route in this controller. A public visitor or a
    normal (non-beta) portal user gets a genuine 404 -- never a redirect,
    never a 'coming soon' page -- while logistics_booking.portal_enabled is
    False."""
    if not (_portal_enabled() or _is_beta_tester()):
        raise NotFound()


def is_approved_customer():
    user = request.env.user
    if user._is_public():
        return False
    partner = user.partner_id.commercial_partner_id
    return (
        partner.logistics_pricing_status == "approved"
        and user.has_group("prema_logistics_booking.group_logistics_customer")
    )


class LogisticsBookingPortal(http.Controller):

    @http.route("/booking", type="http", auth="public", website=True, sitemap=False)
    def booking_landing(self, **kwargs):
        require_visible()
        user = request.env.user
        if user._is_public():
            return request.render("prema_logistics_booking.portal_landing_anonymous", {})
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        return request.redirect("/my/booking/new")

    # ------------------------------------------------------------------
    # Step 1: Saved Locations (primary) + postal fallback
    # ------------------------------------------------------------------
    @http.route("/my/booking/new", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def booking_step1(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        user = request.env.user
        partner = user.partner_id.commercial_partner_id
        SavedLocation = request.env["logistics.saved.location"].sudo()

        error = None
        if request.httprequest.method == "POST":
            pickup_loc_id = kwargs.get("pickup_saved_location_id")
            pickup_postal = kwargs.get("pickup_postal_code")
            delivery_postal = kwargs.get("delivery_postal_code")

            # Collect delivery stop IDs from indexed form fields
            delivery_loc_ids = []
            for key, val in kwargs.items():
                if key.startswith("delivery_saved_location_id_") and val:
                    try:
                        delivery_loc_ids.append(int(val))
                    except (ValueError, TypeError):
                        pass
            # Fallback: single legacy field
            if not delivery_loc_ids:
                single_del = kwargs.get("delivery_saved_location_id")
                if single_del:
                    try:
                        delivery_loc_ids.append(int(single_del))
                    except (ValueError, TypeError):
                        pass

            # Route 1: Saved Location selected
            if pickup_loc_id and delivery_loc_ids:
                pickup_loc = SavedLocation.browse(int(pickup_loc_id))
                delivery_locs = SavedLocation.browse(delivery_loc_ids)
                # Security: ensure all locations belong to this customer
                if pickup_loc.commercial_partner_id.id != partner.id:
                    error = _("Invalid pickup location selection.")
                elif any(dl.commercial_partner_id.id != partner.id for dl in delivery_locs if dl.exists()):
                    error = _("Invalid delivery location selection.")
                elif not pickup_loc.latitude:
                    error = _("Pickup location must have valid coordinates.")
                elif any(not dl.latitude for dl in delivery_locs if dl.exists()):
                    error = _("All delivery locations must have valid coordinates.")
                else:
                    # Build URL with first delivery + all delivery loc IDs
                    first_del = delivery_locs[0]
                    params = (
                        f"pickup_lat={pickup_loc.latitude}&pickup_lng={pickup_loc.longitude}"
                        f"&delivery_lat={first_del.latitude}&delivery_lng={first_del.longitude}"
                        f"&pickup_loc_id={pickup_loc_id}"
                        f"&delivery_loc_id={first_del.id}"
                    )
                    for i, dl in enumerate(delivery_locs):
                        params += f"&delivery_loc_id_{i+1}={dl.id}"
                    return request.redirect(f"/my/booking/details?{params}")

            # Route 2: Postal code fallback
            elif pickup_postal and delivery_postal:
                Fsa = request.env["logistics.fsa"].sudo()
                pickup_fsa = Fsa.resolve_from_postal(pickup_postal)
                delivery_fsa = Fsa.resolve_from_postal(delivery_postal)
                if not pickup_fsa or not pickup_fsa.pickup_supported:
                    error = _("We don't recognize that pickup postal code, or we don't yet service that area.")
                elif not delivery_fsa or not delivery_fsa.delivery_supported:
                    error = _("We don't recognize that delivery postal code, or we don't yet service that area.")
                else:
                    return request.redirect(
                        f"/my/booking/details?pickup={pickup_fsa.fsa}&delivery={delivery_fsa.fsa}"
                    )

            else:
                error = _("Please select both a pickup and delivery location, or enter postal codes.")

        # Load customer's saved locations
        pickup_locations = SavedLocation.search([
            ("commercial_partner_id", "=", partner.id),
            ("active", "=", True),
            ("location_type", "in", ("pickup", "both")),
        ], order="is_default_pickup DESC, last_used_date DESC, name")
        delivery_locations = SavedLocation.search([
            ("commercial_partner_id", "=", partner.id),
            ("active", "=", True),
            ("location_type", "in", ("delivery", "both")),
        ], order="is_default_delivery DESC, last_used_date DESC, name")

        # Handle return from Add New Location (auto-select newly created location)
        new_loc_id = kwargs.get("new_loc_id")
        new_loc_type = kwargs.get("new_loc_type", "")

        # Load customer's recent bookings for sidebar
        customer_bookings = request.env["logistics.booking"].sudo().search([
            ("commercial_partner_id", "=", partner.id),
        ], order="id desc", limit=10)

        return request.render("prema_logistics_booking.portal_step1_locations", {
            "error": error,
            "pickup_locations": pickup_locations,
            "delivery_locations": delivery_locations,
            "has_saved_locations": bool(pickup_locations or delivery_locations),
            "new_loc_id": new_loc_id,
            "new_loc_type": new_loc_type,
            "customer_bookings": customer_bookings,
        })

    # ------------------------------------------------------------------
    # Step 2: shipment basics → instant price + schedule result
    # Accepts both postal codes (FSA) and Saved Location lat/lng.
    # ------------------------------------------------------------------
    # ── Smart calendar: return eligible pickup dates for a movement ──
    @http.route("/my/booking/eligible-dates", type="http", auth="user", website=True, sitemap=False)
    def eligible_pickup_dates(self, **kwargs):
        """JSON: return eligible pickup dates for the given movement coordinates."""
        require_visible()
        if not is_approved_customer():
            return request.make_response(
                json.dumps({"error": "Not authorized"}),
                headers=[("Content-Type", "application/json")],
            )

        try:
            pickup_lat = float(kwargs.get("pickup_lat", 0))
            pickup_lng = float(kwargs.get("pickup_lng", 0))
            delivery_lat = float(kwargs.get("delivery_lat", 0))
            delivery_lng = float(kwargs.get("delivery_lng", 0))
        except (ValueError, TypeError):
            return request.make_response(
                json.dumps({"error": "Invalid coordinates"}),
                headers=[("Content-Type", "application/json")],
            )

        if not pickup_lat or not delivery_lat:
            return request.make_response(
                json.dumps({"dates": []}),
                headers=[("Content-Type", "application/json")],
            )

        from ..services.shipment_routing_service import ShipmentRoutingService
        svc = ShipmentRoutingService(request.env)
        dates = svc.get_eligible_pickup_dates(
            pickup_lat, pickup_lng,
            delivery_lat, delivery_lng,
            pallets=int(kwargs.get("pallets", 1)),
            weight_lbs=float(kwargs.get("weight_lbs", 500) or 500),
            equipment=kwargs.get("equipment", "dry"),
        )

        return request.make_response(
            json.dumps({"dates": dates}),
            headers=[("Content-Type", "application/json")],
        )

    @http.route("/my/booking/details", type="http", auth="user", website=True, sitemap=False)
    def booking_step2(self, pickup=None, delivery=None, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        pickup_lat = kwargs.get("pickup_lat")
        pickup_lng = kwargs.get("pickup_lng")
        delivery_lat = kwargs.get("delivery_lat")
        delivery_lng = kwargs.get("delivery_lng")
        pickup_loc_id = kwargs.get("pickup_loc_id")
        delivery_loc_id = kwargs.get("delivery_loc_id")

        # Collect all delivery stop IDs from indexed params
        delivery_loc_ids = []
        for key, val in kwargs.items():
            if key.startswith("delivery_loc_id_") and val:
                try:
                    delivery_loc_ids.append(int(val))
                except (ValueError, TypeError):
                    pass

        Fsa = request.env["logistics.fsa"].sudo()
        SavedLocation = request.env["logistics.saved.location"].sudo()
        partner = request.env.user.partner_id.commercial_partner_id

        # Route A: Saved Location with coordinates
        if pickup_lat and delivery_lat:
            pickup_loc = None
            delivery_locs = []
            pickup_fsa = None
            delivery_fsa = None

            # Fetch pickup saved location
            if pickup_loc_id:
                pickup_loc = SavedLocation.browse(int(pickup_loc_id))
                if not pickup_loc.exists() or pickup_loc.commercial_partner_id.id != partner.id:
                    return request.redirect("/my/booking/new")

            # Fetch all delivery saved locations
            if delivery_loc_ids:
                delivery_locs = SavedLocation.browse(delivery_loc_ids)
            elif delivery_loc_id:
                single = SavedLocation.browse(int(delivery_loc_id))
                if single.exists():
                    delivery_locs = [single]

            # Validate ownership
            for dl in delivery_locs:
                if dl.commercial_partner_id.id != partner.id:
                    return request.redirect("/my/booking/new")

            # Resolve FSA from pickup location
            if pickup_loc and pickup_loc.postal_code:
                pickup_fsa = Fsa.resolve_from_postal(pickup_loc.postal_code)
            if not pickup_fsa and pickup_loc and pickup_loc.postal_code:
                pickup_fsa = Fsa.search([("fsa", "=", pickup_loc.postal_code.strip().upper()[:3])], limit=1)

            # Resolve FSA from first delivery location (for calendar)
            first_delivery = delivery_locs[0] if delivery_locs else None
            if first_delivery and first_delivery.postal_code:
                delivery_fsa = Fsa.resolve_from_postal(first_delivery.postal_code)
            if not delivery_fsa and first_delivery and first_delivery.postal_code:
                delivery_fsa = Fsa.search([("fsa", "=", first_delivery.postal_code.strip().upper()[:3])], limit=1)

            # Last resort: search FSA by coordinates
            from ..services.region_resolver import RegionResolver
            resolver = RegionResolver(request.env)
            if not pickup_fsa:
                pu_result = resolver.resolve(float(pickup_lat), float(pickup_lng or 0))
                if pu_result.matched_region_code:
                    pickup_fsa = Fsa.search([("service_region_id.code", "=", pu_result.matched_region_code)], limit=1)
            if not delivery_fsa and first_delivery:
                de_result = resolver.resolve(first_delivery.latitude, first_delivery.longitude)
                if de_result.matched_region_code:
                    delivery_fsa = Fsa.search([("service_region_id.code", "=", de_result.matched_region_code)], limit=1)

            # delivery_loc_id may be None if Step 1 only passed indexed
            # delivery_loc_id_N params (multi-stop URL scheme). Derive it
            # from the first entry so the single-stop template branch still
            # renders the hidden form field.
            if not delivery_loc_id and delivery_loc_ids:
                delivery_loc_id = delivery_loc_ids[0]

            # Build template context
            return request.render("prema_logistics_booking.portal_step2_shipment", {
                "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
                "pickup_loc": pickup_loc, "delivery_locs": delivery_locs,
                "delivery_loc": first_delivery,
                "pickup_lat": float(pickup_lat), "pickup_lng": float(pickup_lng or 0),
                "delivery_lat": float(delivery_lat), "delivery_lng": float(delivery_lng or 0),
                "pickup_loc_id": pickup_loc_id, "delivery_loc_ids": delivery_loc_ids,
                "delivery_loc_id": delivery_loc_id,
            })

        # Route B: FSA postal code fallback
        pickup_fsa = Fsa.search([("fsa", "=", pickup)], limit=1)
        delivery_fsa = Fsa.search([("fsa", "=", delivery)], limit=1)
        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/my/booking/new")
        return request.render("prema_logistics_booking.portal_step2_shipment", {
            "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
            "pickup_loc": None, "delivery_loc": None,
            "pickup_lat": 0, "pickup_lng": 0,
            "delivery_lat": 0, "delivery_lng": 0,
            "pickup_loc_id": None, "delivery_loc_id": None,
        })

    @http.route("/my/booking/quote", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_quote(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        Fsa = request.env["logistics.fsa"].sudo()
        SavedLocation = request.env["logistics.saved.location"].sudo()
        partner = request.env.user.partner_id.commercial_partner_id

        # Resolve pickup FSA: prefer saved location, fall back to FSA code
        pickup_fsa = None
        pickup_loc_id = kwargs.get("pickup_loc_id")
        if pickup_loc_id:
            pickup_loc = SavedLocation.browse(int(pickup_loc_id))
            if pickup_loc.exists() and pickup_loc.commercial_partner_id.id == partner.id:
                if pickup_loc.postal_code:
                    pickup_fsa = Fsa.resolve_from_postal(pickup_loc.postal_code)
                if not pickup_fsa and pickup_loc.postal_code:
                    pickup_fsa = Fsa.search([("fsa", "=", pickup_loc.postal_code.strip().upper()[:3])], limit=1)
        if not pickup_fsa:
            pickup_fsa = Fsa.search([("fsa", "=", kwargs.get("pickup_fsa"))], limit=1)

        # Resolve delivery FSA: prefer saved location, fall back to FSA code
        delivery_fsa = None
        delivery_loc_id = kwargs.get("delivery_loc_id")
        if delivery_loc_id:
            delivery_loc = SavedLocation.browse(int(delivery_loc_id))
            if delivery_loc.exists() and delivery_loc.commercial_partner_id.id == partner.id:
                if delivery_loc.postal_code:
                    delivery_fsa = Fsa.resolve_from_postal(delivery_loc.postal_code)
                if not delivery_fsa and delivery_loc.postal_code:
                    delivery_fsa = Fsa.search([("fsa", "=", delivery_loc.postal_code.strip().upper()[:3])], limit=1)
        if not delivery_fsa:
            delivery_fsa = Fsa.search([("fsa", "=", kwargs.get("delivery_fsa"))], limit=1)

        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/my/booking/new")

        try:
            pallets = int(kwargs.get("pallets") or 0)
            weight_lbs = float(kwargs.get("weight_lbs") or 0)
            # Physical pallets: the actual handling units on the truck.
            # Defaults to `pallets` (global input) for backward compatibility.
            physical_pallets = int(kwargs.get("physical_pallets") or pallets or 1)
            shared_pallet_mode = bool(kwargs.get("shared_pallet_mode"))
        except ValueError:
            # Build error context with all required template vars
            pu_loc_for_err = SavedLocation.browse(int(pickup_loc_id)) if pickup_loc_id and SavedLocation.browse(int(pickup_loc_id)).exists() else None
            de_loc_for_err = SavedLocation.browse(int(delivery_loc_id)) if delivery_loc_id and SavedLocation.browse(int(delivery_loc_id)).exists() else None
            return request.render("prema_logistics_booking.portal_step2_shipment", {
                "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
                "pickup_loc": pu_loc_for_err, "delivery_loc": de_loc_for_err,
                "pickup_lat": float(kwargs.get("pickup_lat") or 0) if kwargs.get("pickup_lat") else 0,
                "pickup_lng": float(kwargs.get("pickup_lng") or 0) if kwargs.get("pickup_lng") else 0,
                "delivery_lat": float(kwargs.get("delivery_lat") or 0) if kwargs.get("delivery_lat") else 0,
                "delivery_lng": float(kwargs.get("delivery_lng") or 0) if kwargs.get("delivery_lng") else 0,
                "pickup_loc_id": pickup_loc_id, "delivery_loc_id": delivery_loc_id,
                "error": _("Please enter a valid pallet count and weight."),
            })

        shipment_type = kwargs.get("shipment_type") or "ltl"
        temperature_mode = kwargs.get("temperature_mode") or "dry"
        from ..services.temperature_compat import parse_required_temperature_c
        required_temperature_c = parse_required_temperature_c(
            kwargs.get("required_temperature_c")
        )
        liftgate_pickup = bool(kwargs.get("liftgate_pickup"))
        liftgate_delivery = bool(kwargs.get("liftgate_delivery"))
        appointment = bool(kwargs.get("appointment"))
        residential = bool(kwargs.get("residential"))
        same_day_requested = bool(kwargs.get("same_day_requested"))

        from ..services.booking_orchestration_service import BookingOrchestrationService

        partner = request.env.user.partner_id
        service = BookingOrchestrationService(request.env)

        # Build pickup stops with coordinates when saved location is available
        pickup_stops = [{"postal_code": pickup_fsa.fsa}]
        if pickup_loc_id:
            pu_loc = SavedLocation.browse(int(pickup_loc_id))
            if pu_loc.exists() and pu_loc.commercial_partner_id.id == partner.id:
                pickup_stops[0]["latitude"] = pu_loc.latitude
                pickup_stops[0]["longitude"] = pu_loc.longitude
                pickup_stops[0]["address"] = pu_loc.street or ""
                pickup_stops[0]["city"] = pu_loc.city or ""
                pickup_stops[0]["saved_location_id"] = pu_loc.id
        # Also try coordinates from hidden form fields
        pu_lat = kwargs.get("pickup_lat")
        pu_lng = kwargs.get("pickup_lng")
        if pu_lat and pu_lng and "latitude" not in pickup_stops[0]:
            pickup_stops[0]["latitude"] = float(pu_lat)
            pickup_stops[0]["longitude"] = float(pu_lng)

        # Collect all delivery stop IDs (multi-stop support)
        delivery_loc_ids = []
        for key, val in kwargs.items():
            if key.startswith("delivery_loc_id_") and val:
                try:
                    delivery_loc_ids.append(int(val))
                except (ValueError, TypeError):
                    pass
        if not delivery_loc_ids and delivery_loc_id:
            delivery_loc_ids.append(int(delivery_loc_id))

        # Build delivery stops with per-stop data
        delivery_stops = []
        for i, dl_id in enumerate(delivery_loc_ids):
            dl = SavedLocation.browse(dl_id)
            if not dl.exists() or dl.commercial_partner_id.id != partner.id:
                continue
            # Per-stop pallets/weight from form
            stop_pallets = int(kwargs.get(f"delivery_pallets_{i+1}") or 1)
            stop_weight = float(kwargs.get(f"delivery_weight_{i+1}") or stop_pallets * 500)
            stop_shared = bool(kwargs.get(f"delivery_shared_pallet_{i+1}"))
            stop = {
                "postal_code": dl.postal_code or "",
                "latitude": dl.latitude,
                "longitude": dl.longitude,
                "address": dl.street or "",
                "city": dl.city or "",
                "saved_location_id": dl.id,
                "pallets": stop_pallets,
                "weight_lbs": stop_weight,
                "shared_pallet": stop_shared or shared_pallet_mode,
                "liftgate_delivery": bool(kwargs.get(f"delivery_liftgate_{i+1}")),
                "appointment": bool(kwargs.get(f"delivery_appointment_{i+1}")),
                "instructions": kwargs.get(f"delivery_instructions_{i+1}", "").strip() or "",
            }
            delivery_stops.append(stop)

        # Fallback: single FSA-only mode
        if not delivery_stops:
            delivery_stops = [{"postal_code": delivery_fsa.fsa}]

        # Requested pickup date from form
        requested_pickup_date = kwargs.get("requested_pickup_date", "").strip() or None

        try:
            normalized = service.normalize_request({
                "partner_id": partner.id,
                "pickup_stops": pickup_stops,
                "delivery_stops": delivery_stops,
                "load_type": shipment_type,
                "equipment_type": temperature_mode,
                "required_temperature_c": required_temperature_c,
                "pallets": physical_pallets,  # use physical pallets for pricing (NOT sum of per-stop)
                "physical_pallets": physical_pallets,
                "shared_pallet_mode": shared_pallet_mode,
                "weight_lbs": weight_lbs,
                "liftgate_pickup": liftgate_pickup,
                "liftgate_delivery": liftgate_delivery,
                "appointment": appointment,
                "residential": residential,
                "same_day_requested": same_day_requested,
                "pricing_method": "corridor",
                "requested_pickup_date": requested_pickup_date,
            }, source_channel="portal")
            quote = service.prepare_quote(normalized)
        except UserError as exc:
            return request.render("prema_logistics_booking.portal_not_available", {
                "reason": str(exc),
            })

        session = request.env["logistics.pricing.session"].sudo().search([
            ("token", "=", quote["quote_token"]),
        ], limit=1)
        if not session:
            return request.render("prema_logistics_booking.portal_not_available", {
                "reason": _("The price session could not be created. Please try again."),
            })

        # Fetch saved locations for display
        pickup_loc = session.pickup_saved_location_id if session.pickup_saved_location_id else None
        delivery_stops = session.delivery_stop_ids if session.delivery_stop_ids else None

        return request.render("prema_logistics_booking.portal_step3_result", {
            "session": session,
            "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
            "pickup_loc": pickup_loc, "delivery_stops": delivery_stops,
        })

    # ------------------------------------------------------------------
    # Step 4: confirm (full addresses -> atomic transaction)
    # ------------------------------------------------------------------
    @http.route("/my/booking/confirm", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_confirm(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        token = kwargs.get("token")
        Session = request.env["logistics.pricing.session"].sudo()
        session = Session.search([("token", "=", token)], limit=1)
        if not session:
            return request.render("prema_logistics_booking.portal_booking_error", {
                "message": _("Session expired. Please start over."),
            })

        # Pull address data from session's frozen saved locations first,
        # fall back to form fields (for postal-code-only quotes)
        pu_loc = session.pickup_saved_location_id
        de_loc = session.delivery_saved_location_id

        # Per-stop contact/instructions (UAT-011)
        delivery_stops_data = []
        for stop in session.delivery_stop_ids:
            seq = stop.sequence
            delivery_stops_data.append({
                "sequence": seq,
                "saved_location_id": stop.saved_location_id.id if stop.saved_location_id else None,
                "contact_name": kwargs.get(f"delivery_contact_name_{seq}") or (stop.saved_location_id.contact_name if stop.saved_location_id else ""),
                "phone": kwargs.get(f"delivery_phone_{seq}") or (stop.saved_location_id.contact_phone if stop.saved_location_id else ""),
                "dock_info": kwargs.get(f"delivery_dock_info_{seq}") or (stop.saved_location_id.dock_info if stop.saved_location_id else ""),
                "instructions": kwargs.get(f"delivery_instructions_{seq}") or (stop.saved_location_id.delivery_instructions if stop.saved_location_id else ""),
            })

        address_vals = {
            "pickup_company": pu_loc.business_name or pu_loc.name if pu_loc else kwargs.get("pickup_company"),
            "pickup_postal_code": pu_loc.postal_code if pu_loc else kwargs.get("pickup_postal_code"),
            "pickup_address": pu_loc.street if pu_loc else kwargs.get("pickup_address"),
            "pickup_contact_name": kwargs.get("pickup_contact_name") or (pu_loc.contact_name if pu_loc else ""),
            "pickup_phone": kwargs.get("pickup_phone") or (pu_loc.contact_phone if pu_loc else ""),
            "pickup_instructions": kwargs.get("pickup_instructions") or (pu_loc.pickup_instructions if pu_loc else ""),
            "pickup_dock_info": kwargs.get("pickup_dock_info") or (pu_loc.dock_info if pu_loc else ""),
            "delivery_company": de_loc.business_name or de_loc.name if de_loc else kwargs.get("delivery_company"),
            "delivery_postal_code": de_loc.postal_code if de_loc else kwargs.get("delivery_postal_code"),
            "delivery_address": de_loc.street if de_loc else kwargs.get("delivery_address"),
            "delivery_contact_name": delivery_stops_data[0]["contact_name"] if delivery_stops_data else kwargs.get("delivery_contact_name"),
            "delivery_phone": delivery_stops_data[0]["phone"] if delivery_stops_data else kwargs.get("delivery_phone"),
            "delivery_instructions": delivery_stops_data[0]["instructions"] if delivery_stops_data else kwargs.get("delivery_instructions"),
            "delivery_stops_data": delivery_stops_data,
        }
        try:
            booking = request.env["logistics.booking"].confirm_from_session(token, address_vals)
        except (UserError, AccessError) as exc:
            return request.render("prema_logistics_booking.portal_booking_error", {"message": str(exc)})

        return request.redirect(f"/my/bookings/{booking.id}")

    # ------------------------------------------------------------------
    # Cancel Booking
    # ------------------------------------------------------------------
    @http.route("/my/bookings/cancel", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_cancel(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        booking_id = int(kwargs.get("booking_id", 0))
        reason = kwargs.get("reason", "").strip()
        if not reason:
            return request.redirect(f"/my/bookings/{booking_id}?error=Please+provide+a+cancellation+reason")
        booking = request.env["logistics.booking"].sudo().search([
            ("id", "=", booking_id),
            ("commercial_partner_id", "=", request.env.user.partner_id.commercial_partner_id.id),
        ], limit=1)
        if not booking:
            raise NotFound()
        try:
            booking.action_cancel(reason=reason, source="customer")
        except UserError as e:
            return request.render("prema_logistics_booking.portal_booking_error", {"message": str(e)})
        return request.redirect(f"/my/bookings/{booking_id}")

    # ------------------------------------------------------------------
    # My Bookings
    # ------------------------------------------------------------------
    @http.route("/my/bookings", type="http", auth="user", website=True, sitemap=False)
    def my_bookings(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        bookings = request.env["logistics.booking"].search([], order="id desc")
        return request.render("prema_logistics_booking.portal_my_bookings", {"bookings": bookings})

    @http.route("/my/bookings/<int:booking_id>", type="http", auth="user", website=True, sitemap=False)
    def my_booking_detail(self, booking_id, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        booking = request.env["logistics.booking"].search([("id", "=", booking_id)], limit=1)
        if not booking:
            # Record rule already scopes this to the caller's own company --
            # an id belonging to another customer simply won't be found.
            raise NotFound()
        return request.render("prema_logistics_booking.portal_booking_detail", {"booking": booking})

    # ------------------------------------------------------------------
    # Where We Go — Customer Portal
    # ------------------------------------------------------------------
    @http.route("/my/where-we-go", type="http", auth="user", website=True, sitemap=False)
    def portal_where_we_go(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        return request.render("prema_logistics_booking.portal_where_we_go", {})

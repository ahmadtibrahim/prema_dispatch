import datetime

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from werkzeug.exceptions import NotFound

from ..services.pricing_service import PricingService

PRICING_SESSION_TTL_MINUTES = 20


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
    # Step 1: postal codes
    # ------------------------------------------------------------------
    @http.route("/my/booking/new", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def booking_step1(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        error = None
        if request.httprequest.method == "POST":
            Fsa = request.env["logistics.fsa"].sudo()
            pickup_fsa = Fsa.resolve_from_postal(kwargs.get("pickup_postal_code"))
            delivery_fsa = Fsa.resolve_from_postal(kwargs.get("delivery_postal_code"))
            if not pickup_fsa or not pickup_fsa.pickup_supported:
                error = _("We don't recognize that pickup postal code, or we don't yet service that area.")
            elif not delivery_fsa or not delivery_fsa.delivery_supported:
                error = _("We don't recognize that delivery postal code, or we don't yet service that area.")
            else:
                return request.redirect(f"/my/booking/details?pickup={pickup_fsa.fsa}&delivery={delivery_fsa.fsa}")

        return request.render("prema_logistics_booking.portal_step1_postal", {"error": error})

    # ------------------------------------------------------------------
    # Step 2/3: shipment basics -> instant price + schedule result
    # ------------------------------------------------------------------
    @http.route("/my/booking/details", type="http", auth="user", website=True, sitemap=False)
    def booking_step2(self, pickup=None, delivery=None, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})
        Fsa = request.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.search([("fsa", "=", pickup)], limit=1)
        delivery_fsa = Fsa.search([("fsa", "=", delivery)], limit=1)
        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/my/booking/new")
        return request.render("prema_logistics_booking.portal_step2_shipment", {
            "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
        })

    @http.route("/my/booking/quote", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_quote(self, **kwargs):
        require_visible()
        if not is_approved_customer():
            return request.render("prema_logistics_booking.portal_pending_approval", {})

        Fsa = request.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.search([("fsa", "=", kwargs.get("pickup_fsa"))], limit=1)
        delivery_fsa = Fsa.search([("fsa", "=", kwargs.get("delivery_fsa"))], limit=1)
        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/my/booking/new")

        try:
            pallets = int(kwargs.get("pallets") or 0)
            weight_lbs = float(kwargs.get("weight_lbs") or 0)
        except ValueError:
            return request.render("prema_logistics_booking.portal_step2_shipment", {
                "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
                "error": _("Please enter a valid pallet count and weight."),
            })

        shipment_type = kwargs.get("shipment_type") or "ltl"
        temperature_mode = kwargs.get("temperature_mode") or "dry"
        liftgate_pickup = bool(kwargs.get("liftgate_pickup"))
        liftgate_delivery = bool(kwargs.get("liftgate_delivery"))
        appointment = bool(kwargs.get("appointment"))
        residential = bool(kwargs.get("residential"))
        same_day_requested = bool(kwargs.get("same_day_requested"))

        partner = request.env.user.partner_id
        result = PricingService(request.env).calculate(
            pickup_fsa, delivery_fsa, shipment_type, temperature_mode, pallets, weight_lbs,
            liftgate_pickup, liftgate_delivery, appointment, residential, same_day_requested,
            partner=partner, resolve_departures=True,
        )

        if not result.available:
            return request.render("prema_logistics_booking.portal_not_available", {
                "reason": result.reason,
            })

        expires_at = datetime.datetime.now() + datetime.timedelta(minutes=PRICING_SESSION_TTL_MINUTES)
        session = request.env["logistics.pricing.session"].sudo().create({
            "partner_id": partner.id,
            "pickup_fsa_id": pickup_fsa.id,
            "delivery_fsa_id": delivery_fsa.id,
            "service_offering_id": result.service_offering.id,
            "rate_plan_id": result.rate_plan.id,
            "shipment_type": shipment_type,
            "temperature_mode": temperature_mode,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "liftgate_pickup": liftgate_pickup,
            "liftgate_delivery": liftgate_delivery,
            "appointment": appointment,
            "residential": residential,
            "same_day_requested": same_day_requested,
            "pickup_date": result.pickup_date,
            "delivery_date_estimate": result.delivery_date_estimate,
            "price_snapshot": result.price_lines,
            "route_snapshot": result.route_snapshot,
            "calculated_price": result.calculated_price,
            "expires_at": expires_at,
        })

        return request.render("prema_logistics_booking.portal_step3_result", {
            "session": session, "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
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
        address_vals = {
            "pickup_company": kwargs.get("pickup_company"),
            "pickup_postal_code": kwargs.get("pickup_postal_code"),
            "pickup_address": kwargs.get("pickup_address"),
            "pickup_contact_name": kwargs.get("pickup_contact_name"),
            "pickup_phone": kwargs.get("pickup_phone"),
            "pickup_instructions": kwargs.get("pickup_instructions"),
            "delivery_company": kwargs.get("delivery_company"),
            "delivery_postal_code": kwargs.get("delivery_postal_code"),
            "delivery_address": kwargs.get("delivery_address"),
            "delivery_contact_name": kwargs.get("delivery_contact_name"),
            "delivery_phone": kwargs.get("delivery_phone"),
            "delivery_instructions": kwargs.get("delivery_instructions"),
        }
        try:
            booking = request.env["logistics.booking"].confirm_from_session(token, address_vals)
        except (UserError, AccessError) as exc:
            return request.render("prema_logistics_booking.portal_booking_error", {"message": str(exc)})

        return request.redirect(f"/my/bookings/{booking.id}")

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

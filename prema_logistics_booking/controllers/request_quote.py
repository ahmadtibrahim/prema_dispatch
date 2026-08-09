"""Customer booking portal — scheduled LTL delivery-option-first flow.

Flow:
  1. Locations (pickup + delivery postal codes)
  2. Shipment details (pallets, weight, temperature, accessorials)
  3. Select week (default: current week)
  4. Choose delivery option from available services
  5. Review & Book
"""
import datetime
import json

from odoo import _, http
from odoo.http import request
from werkzeug.exceptions import NotFound as HttpNotFound

from ..services.pricing_service import PricingService
from ..services.availability_service import ScheduledAvailabilityService

PRICING_SESSION_TTL_MINUTES = 20


def _public_test_mode():
    val = request.env["ir.config_parameter"].sudo().get_param("logistics_booking.public_test_mode")
    return str(val).strip().lower() in ("true", "1")


def _portal_enabled():
    val = request.env["ir.config_parameter"].sudo().get_param("logistics_booking.portal_enabled")
    return str(val).strip().lower() in ("true", "1")


def _is_beta_tester():
    user = request.env.user
    if user._is_public():
        return False
    return user.has_group("prema_logistics_booking.group_booking_beta_tester")


def _require_visible():
    if not (_portal_enabled() or _is_beta_tester() or _public_test_mode()):
        raise HttpNotFound()


class LogisticsRequestQuote(http.Controller):

    # ==================================================================
    # STEP 1 — LOCATIONS (redirects to canonical Saved Locations flow)
    # ==================================================================
    @http.route("/request-a-quote", type="http", auth="user", website=True, sitemap=False)
    def step1_locations(self, **kwargs):
        _require_visible()
        # Redirect to the canonical authenticated booking flow with Saved Locations
        return request.redirect("/my/booking/new")

    @http.route("/request-a-quote/locations", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def step1_submit(self, **kwargs):
        _require_visible()
        pickup_postal = (kwargs.get("pickup_postal_code") or "").strip()
        delivery_postal = (kwargs.get("delivery_postal_code") or "").strip()

        Fsa = request.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.resolve_from_postal(pickup_postal)
        delivery_fsa = Fsa.resolve_from_postal(delivery_postal)
        error = None

        if not pickup_fsa or not pickup_fsa.pickup_supported:
            error = _("We don't recognize that pickup postal code, or it's outside our service area. Try a nearby major city postal code.")
        elif not delivery_fsa or not delivery_fsa.delivery_supported:
            error = _("We don't recognize that delivery postal code, or it's outside our service area. Try a nearby major city postal code.")
        elif not pickup_fsa.region_id or not delivery_fsa.region_id:
            error = _("We couldn't determine your region. Please try a different postal code.")

        if error:
            return request.render("prema_logistics_booking.portal_booking_step1_locations", {"error": error})

        return request.redirect(
            f"/request-a-quote/shipment?pickup_fsa={pickup_fsa.fsa}&delivery_fsa={delivery_fsa.fsa}"
            f"&pickup_city={pickup_fsa.display_city or pickup_fsa.fsa}"
            f"&delivery_city={delivery_fsa.display_city or delivery_fsa.fsa}"
            f"&pickup_region={pickup_fsa.region_id.name}"
            f"&delivery_region={delivery_fsa.region_id.name}"
        )

    # ==================================================================
    # STEP 2 — SHIPMENT DETAILS
    # ==================================================================
    @http.route("/request-a-quote/shipment", type="http", auth="user", website=True, sitemap=False)
    def step2_shipment(self, **kwargs):
        _require_visible()
        return request.render("prema_logistics_booking.portal_booking_step2_shipment", {
            "pickup_fsa": kwargs.get("pickup_fsa", ""),
            "delivery_fsa": kwargs.get("delivery_fsa", ""),
            "pickup_city": kwargs.get("pickup_city", ""),
            "delivery_city": kwargs.get("delivery_city", ""),
            "pickup_region": kwargs.get("pickup_region", ""),
            "delivery_region": kwargs.get("delivery_region", ""),
        })

    # ==================================================================
    # STEP 3 — SEARCH NEXT 14 DAYS + SHOW DELIVERY OPTIONS
    # ==================================================================
    @http.route("/request-a-quote/delivery-options", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def step3_delivery_options(self, **kwargs):
        _require_visible()
        Fsa = request.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.search([("fsa", "=", kwargs.get("pickup_fsa"))], limit=1)
        delivery_fsa = Fsa.search([("fsa", "=", kwargs.get("delivery_fsa"))], limit=1)

        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/request-a-quote")

        try:
            pallets = int(kwargs.get("pallets") or 1)
            weight_lbs = float(kwargs.get("weight_lbs") or pallets * 500)
        except ValueError:
            pallets, weight_lbs = 1, 500.0

        from ..services.temperature_compat import to_canonical_temperature_mode, parse_required_temperature_c
        temperature_mode = to_canonical_temperature_mode(kwargs.get("temperature_mode") or "dry")
        required_temperature_c = parse_required_temperature_c(kwargs.get("required_temperature_c"))
        liftgate_pickup = bool(kwargs.get("liftgate_pickup"))
        liftgate_delivery = bool(kwargs.get("liftgate_delivery"))
        appointment = bool(kwargs.get("appointment"))
        residential = bool(kwargs.get("residential"))
        # Search the complete customer-visible eight-week corridor horizon.
        avail_svc = ScheduledAvailabilityService(request.env)
        all_options = []
        seen_dates = set()

        for week_offset in range(8):
            availability = avail_svc.find_available_services(
                pickup_fsa, delivery_fsa, pallets, weight_lbs, temperature_mode, week_offset,
                required_temperature_c=required_temperature_c,
            )
            for opt in availability.options:
                if opt.priority == "custom_quote":
                    continue
                key = str(opt.delivery_date) if opt.delivery_date else ""
                if key and key not in seen_dates:
                    seen_dates.add(key)
                    remaining = opt.available_pallets
                    cap_label = "AVAILABLE"
                    if remaining < pallets:
                        cap_label = "SOLD_OUT"
                    elif remaining <= 3:
                        cap_label = "LIMITED_SPACE"

                    all_options.append({
                        "priority": opt.priority,
                        "delivery_date": str(opt.delivery_date) if opt.delivery_date else "",
                        "delivery_day_name": opt.delivery_date.strftime("%A") if opt.delivery_date else "",
                        "delivery_date_formatted": opt.delivery_date.strftime("%A %B %d") if opt.delivery_date else "",
                        "pickup_date": str(opt.pickup_date) if opt.pickup_date else "",
                        "pickup_date_formatted": opt.pickup_date.strftime("%A %B %d") if opt.pickup_date else "",
                        "price": opt.price,
                        "service_label": opt.service_label,
                        "routing_strategy": opt.routing_strategy,
                        "available_pallets": opt.available_pallets,
                        "capacity_label": cap_label,
                        "capacity_ok": opt.capacity_ok,
                    })

        # Check if any custom_quote needed
        has_any = len(all_options) > 0
        first_sold_out = None
        for o in all_options:
            if o["capacity_label"] == "SOLD_OUT" and not first_sold_out:
                first_sold_out = o

        return request.render("prema_logistics_booking.portal_booking_step3_options", {
            "pickup_fsa": pickup_fsa,
            "delivery_fsa": delivery_fsa,
            "pickup_city": kwargs.get("pickup_city", pickup_fsa.display_city or ""),
            "delivery_city": kwargs.get("delivery_city", delivery_fsa.display_city or ""),
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "temperature_mode": temperature_mode,
            "required_temperature_c": required_temperature_c if required_temperature_c is not None else "",
            "liftgate_pickup": liftgate_pickup,
            "liftgate_delivery": liftgate_delivery,
            "appointment": appointment,
            "residential": residential,
            "options": all_options,
            "has_scheduled": has_any,
            "has_custom_quote": not has_any,
            "first_sold_out": first_sold_out,
        })

    # ==================================================================
    # STEP 4 — SELECT & PRICE A SPECIFIC OPTION
    # ==================================================================
    @http.route("/request-a-quote/select", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def step4_select(self, **kwargs):
        _require_visible()
        Fsa = request.env["logistics.fsa"].sudo()
        pickup_fsa = Fsa.search([("fsa", "=", kwargs.get("pickup_fsa"))], limit=1)
        delivery_fsa = Fsa.search([("fsa", "=", kwargs.get("delivery_fsa"))], limit=1)

        if not pickup_fsa or not delivery_fsa:
            return request.redirect("/request-a-quote")

        try:
            pallets = int(kwargs.get("pallets") or 1)
            weight_lbs = float(kwargs.get("weight_lbs") or 800)
        except ValueError:
            pallets, weight_lbs = 1, 800.0

        from ..services.temperature_compat import to_canonical_temperature_mode, parse_required_temperature_c
        temperature_mode = to_canonical_temperature_mode(kwargs.get("temperature_mode") or "dry")
        required_temperature_c = parse_required_temperature_c(kwargs.get("required_temperature_c"))
        liftgate_pickup = bool(kwargs.get("liftgate_pickup"))
        liftgate_delivery = bool(kwargs.get("liftgate_delivery"))
        appointment = bool(kwargs.get("appointment"))
        residential = bool(kwargs.get("residential"))
        requested_pickup_date = None
        try:
            if kwargs.get("requested_pickup_date"):
                requested_pickup_date = datetime.date.fromisoformat(
                    kwargs["requested_pickup_date"]
                )
        except ValueError:
            return request.redirect("/request-a-quote")

        # Get real pricing
        result = PricingService(request.env).calculate(
            pickup_fsa, delivery_fsa, "ltl", temperature_mode, pallets, weight_lbs,
            liftgate_pickup, liftgate_delivery, appointment, residential,
            partner=None, required_temperature_c=required_temperature_c,
            resolve_departures=True,
            reference_dt=requested_pickup_date,
        )

        if not result.available:
            return request.render("prema_logistics_booking.portal_custom_quote_offer", {
                "pickup_fsa": pickup_fsa,
                "delivery_fsa": delivery_fsa,
                "reason": result.reason,
            })

        # Create pricing session
        expires_at = datetime.datetime.now() + datetime.timedelta(minutes=PRICING_SESSION_TTL_MINUTES)
        session = request.env["logistics.pricing.session"].sudo().create({
            "partner_id": request.env.user.partner_id.id if not request.env.user._is_public() else 1,
            "pickup_fsa_id": pickup_fsa.id,
            "delivery_fsa_id": delivery_fsa.id,
            "corridor_id": result.corridor.id,
            "service_offering_id": result.service_offering.id if result.service_offering else False,
            "rate_plan_id": result.rate_plan.id if result.rate_plan else False,
            "shipment_type": "ltl",
            "temperature_mode": temperature_mode,
            "required_temperature_c": required_temperature_c if required_temperature_c is not None else 0.0,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "liftgate_pickup": liftgate_pickup,
            "liftgate_delivery": liftgate_delivery,
            "appointment": appointment,
            "residential": residential,
            "same_day_requested": False,
            "pickup_date": result.pickup_date,
            "delivery_date_estimate": result.delivery_date_estimate,
            "route_snapshot": result.route_snapshot,
            "price_snapshot": result.price_lines,
            "calculated_price": result.calculated_price,
            "expires_at": expires_at,
        })

        delivery_day = kwargs.get("delivery_day", "")
        pickup_day = kwargs.get("pickup_day", "")

        return request.render("prema_logistics_booking.portal_booking_step4_review", {
            "session": session,
            "pickup_fsa": pickup_fsa,
            "delivery_fsa": delivery_fsa,
            "pickup_city": kwargs.get("pickup_city", pickup_fsa.display_city or ""),
            "delivery_city": kwargs.get("delivery_city", delivery_fsa.display_city or ""),
            "delivery_day": delivery_day,
            "pickup_day": pickup_day,
        })

    # ==================================================================
    # BOOKING CONFIRMATION
    # ==================================================================
    @http.route("/request-a-quote/confirm", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def confirm_booking(self, **kwargs):
        _require_visible()
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
        except Exception as exc:
            return request.render("prema_logistics_booking.portal_quote_error", {"message": str(exc)})

        return request.render("prema_logistics_booking.portal_booking_confirmed", {
            "booking": booking,
        })

    # ==================================================================
    # CUSTOM QUOTE SUBMISSION
    # ==================================================================
    @http.route("/request-a-quote/submit", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def submit_custom_quote(self, **kwargs):
        _require_visible()
        vals = {
            "contact_name": kwargs.get("contact_name"),
            "contact_email": kwargs.get("contact_email"),
            "contact_phone": kwargs.get("contact_phone"),
            "company_name": kwargs.get("company_name"),
            "pickup_postal_code": kwargs.get("pickup_postal_code"),
            "pickup_address": kwargs.get("pickup_address"),
            "delivery_postal_code": kwargs.get("delivery_postal_code"),
            "delivery_address": kwargs.get("delivery_address"),
            "pallets": int(kwargs.get("pallets") or 1),
            "weight_lbs": float(kwargs.get("weight_lbs") or 800),
            "temperature_mode": kwargs.get("temperature_mode") or "dry",
            "commodity": kwargs.get("commodity"),
            "notes": kwargs.get("notes"),
            "source": "website",
            "state": "new",
        }
        Fsa = request.env["logistics.fsa"].sudo()
        pf = Fsa.resolve_from_postal(vals["pickup_postal_code"])
        df = Fsa.resolve_from_postal(vals["delivery_postal_code"])
        if pf:
            vals["resolved_fsa_pickup"] = pf.fsa
            vals["resolved_region_pickup"] = pf.region_id.id if pf.region_id else False
        if df:
            vals["resolved_fsa_delivery"] = df.fsa
            vals["resolved_region_delivery"] = df.region_id.id if df.region_id else False

        quote = request.env["logistics.custom.quote"].sudo().create(vals)
        return request.render("prema_logistics_booking.portal_custom_quote_confirmed", {"quote": quote})

import json
import logging
import traceback

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from werkzeug.exceptions import NotFound

from odoo.addons.prema_logistics_booking.models.logistics_pricing_session import (
    _customer_safe_leg_label,
)

def _allocate_transportation(route_total, cumulative_distances, onboard_counts):
    """Display-only explanatory allocation of an EXISTING route-level
    transportation total across stops.

    segment_weight_i = incremental_distance_i × pallets_onboard_i
    stop_amount_i    = route_total × segment_weight_i / Σ segment_weights

    Falls back to distance-share (onboard ignored) when onboard counts are
    incomplete. Residual rounding goes to the final stop. Never changes the
    authoritative total."""
    amounts = [0.0] * len(cumulative_distances)
    if not cumulative_distances or route_total <= 0:
        return amounts
    increments = []
    previous = 0.0
    for cumulative in cumulative_distances:
        increments.append(max(0.0, cumulative - previous))
        previous = cumulative
    has_onboard = len(onboard_counts) == len(increments) and all(
        count > 0 for count in onboard_counts)
    weights = []
    for index, increment in enumerate(increments):
        onboard = onboard_counts[index] if has_onboard else 1
        weights.append(increment * onboard)
    total_weight = sum(weights)
    if total_weight <= 0:
        return amounts
    remaining = route_total
    for index in range(len(amounts) - 1):
        share = round(route_total * weights[index] / total_weight, 2)
        amounts[index] = share
        remaining -= share
    amounts[-1] = round(remaining, 2)
    return amounts


def _allocated_stop_weights(session, delivery_stops):
    """Display-only stop weights derived from the actual session values:
    average_pallet_weight = total_weight_lbs / physical_pallets, then
    stop_weight = average × pallets assigned to that stop. Shared pallets
    are counted once, at their first assigned stop (no physical weight
    duplication). Falls back to the stop dicts' own values when no
    allocation data exists."""
    stops = []
    for index, stop in enumerate(delivery_stops or []):
        if not isinstance(stop, dict):
            # Records pass through untouched — the template renders them
            # with their own fields.
            stops.append(stop)
            continue
        entry = dict(stop)
        entry.setdefault("sequence", index + 1)
        stops.append(entry)
    if not stops:
        return stops
    physical = session.physical_pallets or session.pallets or 1
    allocations = session.pallet_allocations or []
    if not allocations:
        return stops
    average = (session.weight_lbs or 0.0) / float(physical)
    assigned = {index + 1: 0 for index in range(len(stops))}
    for alloc in allocations:
        stops_of_pallet = alloc.get("stops") or []
        if stops_of_pallet:
            # Shared pallets count once, at their first stop.
            target = min(stops_of_pallet)
            assigned[target] = assigned.get(target, 0) + 1
    for index, stop in enumerate(stops):
        count = assigned.get(index + 1, 0)
        if count:
            stop["weight_lbs"] = round(average * count, 1)
    return stops


def _build_stop_pricing(session):
    """Customer-facing pricing breakdown built ONLY from the session's
    existing price_snapshot lines. No pricing is computed here; the
    components reconcile to calculated_price by construction.

    Sections (frozen at quote time by the canonical LTL calculator):
    - Base Pallet Transportation (per leg: pallets × per-pallet price)
    - Weight (per leg: actual / included / excess lb × excess rate)
    - Additional Stops / Pickups, Volume Discount, Minimum Adjustment
      (booking-level lines from the same snapshot)
    The sum of every shown line equals session.calculated_price exactly.

    Attribution rule:
    - One leg per stop → leg amounts map to stops directly.
    - ONE route-level leg for MULTIPLE stops → show a single "Route
      Transportation" line and mark every stop "Included in Route"
      (never assign the whole route price to one arbitrary stop)."""
    stops = []
    for index, stop in enumerate(session.delivery_stop_ids.sorted("sequence")):
        stops.append({
            "index": index + 1,
            "name": stop.location_name or stop.city or ("Stop %d" % (index + 1)),
            "city": stop.city or "",
            "amount": 0.0,
        })
    booking_level = []
    route_transportation = 0.0
    leg_lines = []
    for line in session.price_snapshot or []:
        if not isinstance(line, dict):
            continue
        label = line.get("label", "")
        amount = line.get("amount", 0.0) or 0.0
        if label.startswith("Leg "):
            leg_lines.append(amount)
        elif any(key in label for key in ("Volume discount", "Additional Stop", "Minimum booking")):
            booking_level.append({"label": label, "amount": amount})
    if leg_lines and len(stops) > 1 and len(leg_lines) == 1:
        route_transportation = round(sum(leg_lines), 2)
        # Explanatory per-stop allocation (display-only): corridor segment
        # distances from each stop's resolved region × pallets onboard.
        try:
            from ..services.region_resolver import RegionResolver
            resolver = RegionResolver(session.env)
            corridor = session.corridor_id
            corridor_stops = corridor.stop_ids.filtered(
                lambda s: s.active and s.region_id).sorted("sequence") if corridor else []
            cumulative = []
            onboard = []
            allocations = session.pallet_allocations or []
            for stop in session.delivery_stop_ids.sorted("sequence"):
                region = False
                if stop.latitude and stop.longitude:
                    match = resolver.resolve(stop.latitude, stop.longitude)
                    region = match.matched_region
                matched = next(
                    (s for s in corridor_stops if region and s.region_id == region),
                    None,
                )
                cumulative.append(
                    matched.distance_from_origin_km if matched else 0.0)
                delivered = len([
                    a for a in allocations
                    if a.get("stops") and stop.sequence in a.get("stops", [])
                ])
                onboard.append(delivered if delivered else 0)
            if all(cumulative):
                amounts = _allocate_transportation(
                    route_transportation, cumulative, onboard)
                for index, stop in enumerate(stops):
                    stop["amount"] = amounts[index]
        except Exception:
            pass
    else:
        for leg_no, amount in enumerate(leg_lines):
            if stops:
                target = min(leg_no, len(stops) - 1)
                stops[target]["amount"] = round(stops[target]["amount"] + amount, 2)
    # Route label: pickup city → stop cities (display only).
    route_label = ""
    if route_transportation:
        pickup_city = (session.pickup_saved_location_id.city
                       if session.pickup_saved_location_id else "")
        cities = [s["city"] for s in stops if s["city"]]
        route_label = " → ".join([c for c in [pickup_city] + cities if c])
    # ── Frozen per-leg breakdown (spec sections: "Base Pallet
    #    Transportation" + "Weight"), built strictly from the snapshot
    #    lines produced by the canonical calculator at quote time. ──
    legs = []
    weight_rows = []
    base_total = 0.0
    weight_total = 0.0
    # Customer-safe labels: internal routing language (leg numbers, corridor
    # names, hub role codes) is mapped for display only — prices untouched.
    leg_index = 0
    total_legs = len(leg_lines)
    for line in session.price_snapshot or []:
        if not isinstance(line, dict) or not str(line.get("label", "")).startswith("Leg "):
            continue
        leg_index += 1
        display_label = _customer_safe_leg_label(
            line.get("label", ""), leg_index, total_legs)
        pallets = line.get("pallets") or 0
        rate = line.get("pallet_rate_per_km") or 0.0
        distance = line.get("distance_km") or 0.0
        base_charge = float(
            line.get("base_leg_charge") if line.get("base_leg_charge") is not None
            else line.get("amount") or 0.0)
        per_pallet = round(rate * distance, 2) if (rate and distance) else (
            base_charge / pallets if pallets else 0.0)
        legs.append({
            "label": line.get("label", ""),
            "display_label": display_label,
            "pallets": pallets,
            "per_pallet_price": round(per_pallet, 2),
            "base_charge": round(base_charge, 2),
        })
        base_total += base_charge
        excess_charge = float(line.get("excess_weight_charge") or 0.0)
        if excess_charge > 0:
            weight_rows.append({
                "label": line.get("label", ""),
                "display_label": display_label,
                "actual_weight_lbs": float(line.get("actual_weight_lbs") or 0.0),
                "included_weight_lbs": float(line.get("included_weight_lbs") or 0.0),
                "excess_weight_lbs": float(line.get("excess_weight_lbs") or 0.0),
                "excess_weight_rate_per_lb": float(line.get("excess_weight_rate_per_lb") or 0.0),
                "excess_weight_charge": round(excess_charge, 2),
            })
            weight_total += excess_charge
    if len(weight_rows) == 1:
        # Single weight row: plain "Excess Weight" heading with the frozen
        # detail sub-line (actual / included / excess lb × rate) beneath it.
        weight_rows[0]["display_label"] = "Excess Weight"
    base_total = round(base_total, 2)
    weight_total = round(weight_total, 2)
    booking_total = round(
        sum(float(b.get("amount") or 0.0) for b in booking_level), 2)
    breakdown_total = round(base_total + weight_total + booking_total, 2)
    calculated = float(session.calculated_price or 0.0)
    if calculated and abs(breakdown_total - calculated) > 0.01:
        logging.getLogger(__name__).warning(
            "Step-3 breakdown mismatch: breakdown=%s calculated_price=%s "
            "snapshot=%s", breakdown_total, calculated, session.price_snapshot)
    return {
        "stops": stops,
        "booking_level": booking_level,
        "route_transportation": route_transportation,
        "route_label": route_label,
        "legs": legs,
        "weight_rows": weight_rows,
        "base_total": base_total,
        "weight_total": weight_total,
        "breakdown_total": breakdown_total,
        "total": calculated,
    }


def _saved_locations_builder_payload(partner):
    """JSON payload for the portal route builder: every saved location
    owned by the customer with coordinates and operating hours, so stop
    cards can offer location selection and show facility hours without
    extra round-trips."""
    SavedLocation = request.env["logistics.saved.location"].sudo()
    Hours = request.env["logistics.saved.location.hours"].sudo()
    locations = SavedLocation.search([
        ("commercial_partner_id", "=", partner.id),
        ("active", "=", True),
    ], order="name")
    payload = []
    for loc in locations:
        hours = {}
        for day in range(7):
            rows = Hours.search([
                ("saved_location_id", "=", loc.id),
                ("day_of_week", "=", str(day)),
                ("active", "=", True),
            ])
            general = rows.filtered(lambda r: r.service_scope == "general") or rows[:1]
            if not general:
                hours[str(day)] = None
                continue
            row = general[0]
            if row.status == "closed":
                hours[str(day)] = None
            elif row.status == "open_24h":
                hours[str(day)] = [0.0, 24.0]
            else:
                hours[str(day)] = [float(row.open_time or 0.0), float(row.close_time or 24.0)]
        payload.append({
            "id": loc.id,
            "name": loc.name or "",
            "business_name": loc.business_name or "",
            "city": loc.city or "",
            "postal_code": loc.postal_code or "",
            "latitude": loc.latitude or 0.0,
            "longitude": loc.longitude or 0.0,
            "timezone": loc.timezone or "America/Toronto",
            "location_type": loc.location_type or "both",
            "liftgate_required": bool(loc.liftgate_required),
            "dock_info": bool(loc.dock_info),
            "hours": hours,
        })
    return payload


def _reconcile_pallet_allocations(physical_pallets, allocations):
    """Make the allocation list length match the submitted physical pallet
    count. Missing pallets are padded with default unallocated records
    (pallet N, no stops, dedicated); extra records are dropped. This keeps
    the quote payload, pricing session, and booking consistent even when
    frontend state is stale — never a silent mismatch, never a 500."""
    reconciled = []
    for index in range(1, physical_pallets + 1):
        record = next(
            (a for a in (allocations or [])
             if isinstance(a, dict) and a.get("pallet") == index),
            None,
        )
        if record:
            reconciled.append(record)
        else:
            reconciled.append({"pallet": index, "stops": [], "shared": False})
    return reconciled


def _parse_time_float(val):
    """Convert HH:MM string to float hours (e.g. '11:30' → 11.5)."""
    if not val:
        return None
    try:
        parts = val.strip().split(":")
        return int(parts[0]) + int(parts[1]) / 60.0
    except (ValueError, IndexError):
        return None

def _parse_bool(val):
    """Strict portal checkbox parsing — the single helper for every
    checkbox flag in this controller. Only 1/true/yes/on are True:
    bool("0") is a classic Python pitfall (bool("0") == True), so raw
    `bool(...)` on portal POST values is never used. Real booleans
    (e.g. parsed JSON) pass through untouched."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")

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


class LogisticsBookingPortal(http.Controller):

    @http.route("/booking", type="http", auth="public", website=True, sitemap=False)
    def booking_landing(self, **kwargs):
        require_visible()
        user = request.env.user
        if user._is_public():
            return request.render("prema_logistics_booking.portal_landing_anonymous", {})
        return request.redirect("/my/booking/new")

    # ------------------------------------------------------------------
    # Step 1: Saved Locations (primary) + postal fallback
    # ------------------------------------------------------------------
    @http.route("/my/booking/new", type="http", auth="user", website=True, sitemap=False, methods=["GET", "POST"])
    def booking_step1(self, **kwargs):
        require_visible()

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

        partner = request.env.user.partner_id.commercial_partner_id
        SavedLocation = request.env["logistics.saved.location"].sudo()

        # Preferred route: the full stop list. All delivery saved-location
        # IDs are resolved server-side (ownership-validated) so the date
        # engine evaluates the COMPLETE shipment — every delivery stop,
        # pallet count, equipment and capacity — never just the first
        # delivery's coordinates.
        delivery_loc_ids = []
        raw_ids = kwargs.get("delivery_loc_ids", "")
        if isinstance(raw_ids, str):
            delivery_loc_ids = [
                int(x) for x in raw_ids.split(",") if x.strip().lstrip("-").isdigit()
            ]
        stops = []
        pickup_loc_id = kwargs.get("pickup_loc_id")
        if pickup_loc_id and delivery_loc_ids:
            pickup_loc = SavedLocation.browse(int(pickup_loc_id))
            delivery_locs = SavedLocation.browse(delivery_loc_ids)
            if (pickup_loc.exists() and pickup_loc.commercial_partner_id.id == partner.id
                    and pickup_loc.latitude
                    and all(
                        dl.exists() and dl.commercial_partner_id.id == partner.id and dl.latitude
                        for dl in delivery_locs
                    )):
                stops.append({
                    "stop_type": "pickup",
                    "latitude": pickup_loc.latitude,
                    "longitude": pickup_loc.longitude,
                    "saved_location_id": pickup_loc.id,
                    "postal_code": pickup_loc.postal_code or "",
                })
                for dl in delivery_locs:
                    stops.append({
                        "stop_type": "delivery",
                        "latitude": dl.latitude,
                        "longitude": dl.longitude,
                        "saved_location_id": dl.id,
                        "city": dl.city or "",
                        "postal_code": dl.postal_code or "",
                    })

        if not stops:
            # Legacy fallback: single coordinate pair (postal / step-1 URL).
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
            stops.append({"stop_type": "pickup", "latitude": pickup_lat, "longitude": pickup_lng})
            stops.append({"stop_type": "delivery", "latitude": delivery_lat, "longitude": delivery_lng})

        from ..services.shipment_routing_service import ShipmentRoutingService
        svc = ShipmentRoutingService(request.env)
        # ONE eligibility authority: the same strict stop resolution the
        # Get Price path runs. When the pickup (or any delivery) is outside
        # the scheduled network the response carries manual_quote=True and
        # NO dates — the portal renders the Manual Quote Required banner
        # instead of fake selectable dates.
        verdict = svc.calendar_availability(
            stops,
            physical_pallets=int(kwargs.get("pallets", 1)),
            weight_lbs=float(kwargs.get("weight_lbs", 500) or 500),
            equipment=kwargs.get("equipment", "dry"),
        )

        return request.make_response(
            json.dumps({
                "dates": verdict["dates"],
                "manual_quote": verdict["manual_quote"],
                "reason": verdict["reason"],
            }),
            headers=[("Content-Type", "application/json")],
        )

    @http.route("/my/booking/details", type="http", auth="user", website=True, sitemap=False)
    def booking_step2(self, pickup=None, delivery=None, **kwargs):
        require_visible()

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

        # Collect additional pickup stop IDs (milk-run route builder)
        pickup_loc_ids = []
        for key, val in kwargs.items():
            if key.startswith("pickup_loc_id_") and val:
                try:
                    pickup_loc_ids.append(int(val))
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
                    pickup_fsa = Fsa.search([("region_id.code", "=", pu_result.matched_region_code)], limit=1)
            if not delivery_fsa and first_delivery:
                de_result = resolver.resolve(first_delivery.latitude, first_delivery.longitude)
                if de_result.matched_region_code:
                    delivery_fsa = Fsa.search([("region_id.code", "=", de_result.matched_region_code)], limit=1)

            # delivery_loc_id may be None if Step 1 only passed indexed
            # delivery_loc_id_N params (multi-stop URL scheme). Derive it
            # from the first entry so the single-stop template branch still
            # renders the hidden form field.
            if not delivery_loc_id and delivery_loc_ids:
                delivery_loc_id = delivery_loc_ids[0]

            # Build template context
            delivery_locs_payload = [{
                "id": dl.id,
                "name": dl.name or "",
                "business_name": dl.business_name or "",
                "city": dl.city or "",
                "latitude": dl.latitude,
                "longitude": dl.longitude,
            } for dl in delivery_locs]
            pickup_loc_payload = None
            if pickup_loc and pickup_loc.exists():
                pickup_loc_payload = {
                    "id": pickup_loc.id,
                    "name": pickup_loc.name or "",
                    "business_name": pickup_loc.business_name or "",
                    "city": pickup_loc.city or "",
                    "latitude": pickup_loc.latitude,
                    "longitude": pickup_loc.longitude,
                }
            return request.render("prema_logistics_booking.portal_step2_shipment", {
                "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
                "pickup_loc": pickup_loc, "delivery_locs": delivery_locs,
                "delivery_loc": first_delivery,
                "pickup_lat": float(pickup_lat), "pickup_lng": float(pickup_lng or 0),
                "delivery_lat": float(delivery_lat), "delivery_lng": float(delivery_lng or 0),
                "pickup_loc_id": pickup_loc_id, "delivery_loc_ids": delivery_loc_ids,
                "delivery_loc_id": delivery_loc_id,
                "pickup_loc_ids": pickup_loc_ids,
                "saved_locations_json": json.dumps(
                    _saved_locations_builder_payload(partner)),
                "delivery_locs_json": json.dumps(delivery_locs_payload),
                "pickup_loc_json": json.dumps(pickup_loc_payload),
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
            "saved_locations_json": json.dumps(
                _saved_locations_builder_payload(partner)),
        })

    @http.route("/my/booking/quote", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_quote(self, **kwargs):
        require_visible()

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

        # Require FSA OR coordinates — coordinates alone can drive ShipmentRoutingService
        has_pickup_coords = kwargs.get("pickup_lat") and kwargs.get("pickup_lng")
        has_delivery_coords = kwargs.get("delivery_lat") and kwargs.get("delivery_lng")
        if (not pickup_fsa and not has_pickup_coords) or (not delivery_fsa and not has_delivery_coords):
            return request.redirect("/my/booking/new")

        try:
            pallets = int(kwargs.get("pallets") or 0)
            weight_lbs = float(kwargs.get("weight_lbs") or 0)
            # Physical pallets: the actual handling units on the truck.
            # Defaults to `pallets` (global input) for backward compatibility.
            physical_pallets = int(kwargs.get("physical_pallets") or pallets or 1)
            shared_pallet_mode = _parse_bool(kwargs.get("shared_pallet_mode"))
            # Parse per-pallet allocations from the new portal UI
            pallet_allocations = []
            allocs_json = kwargs.get("pallet_allocations_json", "").strip()
            if allocs_json:
                try:
                    pallet_allocations = json.loads(allocs_json)
                except (json.JSONDecodeError, TypeError):
                    pallet_allocations = []
            # Hard consistency: the submitted allocation records must match
            # the submitted physical pallet count exactly.
            pallet_allocations = _reconcile_pallet_allocations(
                physical_pallets, pallet_allocations)

            # Generalized milk-run payload from the portal route builder:
            # ordered stops with stable stop keys + canonical pallet
            # movements. Both or neither — a half-built payload never
            # silently becomes a legacy booking.
            route_stops = []
            pallet_movements = []
            route_stops_json = kwargs.get("route_stops_json", "").strip()
            movements_json = kwargs.get("pallet_movements_json", "").strip()
            if route_stops_json and movements_json:
                try:
                    route_stops = json.loads(route_stops_json)
                    pallet_movements = json.loads(movements_json)
                except (json.JSONDecodeError, TypeError):
                    route_stops = []
                    pallet_movements = []
                if route_stops and pallet_movements:
                    physical_pallets = len(pallet_movements)
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

        # ── Server-side capacity pre-check (Get Price) ─────────────────
        # The customer can never quote more pallets than the selected
        # departure's truck can carry. The authoritative locked re-check
        # still runs at confirmation.
        requested_pickup_date = kwargs.get("requested_pickup_date", "").strip() or None
        if pickup_fsa or (kwargs.get("pickup_lat") and kwargs.get("pickup_lng")):
            try:
                from ..services.region_resolver import RegionResolver
                from ..services.vehicle_capacity_service import VehicleCapacityService
                pickup_region = pickup_fsa.region_id if pickup_fsa else False
                if not pickup_region and kwargs.get("pickup_lat") and kwargs.get("pickup_lng"):
                    match = RegionResolver(request.env).resolve(
                        float(kwargs["pickup_lat"]), float(kwargs["pickup_lng"]))
                    pickup_region = match.matched_region
                capacity = VehicleCapacityService.for_pickup_date(
                    request.env, pickup_region, requested_pickup_date)
                if capacity.get("available") and physical_pallets > capacity["remaining_pallets"]:
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
                        # Non-disclosing: never reveal the remaining pallet
                        # count — exact capacity is internal.
                        "error": _(
                            "This pallet quantity is not available on the "
                            "selected departure. Reduce the quantity or "
                            "choose another pickup date.",
                        ),
                    })
            except (ValueError, TypeError):
                pass

        shipment_type = kwargs.get("shipment_type") or "ltl"
        temperature_mode = kwargs.get("temperature_mode") or "dry"
        from ..services.temperature_compat import parse_required_temperature_c
        required_temperature_c = parse_required_temperature_c(
            kwargs.get("required_temperature_c")
        )
        liftgate_pickup = _parse_bool(kwargs.get("liftgate_pickup"))
        liftgate_delivery = _parse_bool(kwargs.get("liftgate_delivery"))
        appointment = _parse_bool(kwargs.get("appointment"))
        residential = _parse_bool(kwargs.get("residential"))
        same_day_requested = _parse_bool(kwargs.get("same_day_requested"))

        from ..services.booking_orchestration_service import BookingOrchestrationService

        # Saved locations are owned by the commercial partner (same as
        # every other handler in this file) — the bare user partner breaks
        # ownership for contact-type users (e.g. admin) and silently drops
        # their delivery stop, collapsing the quote to FSA-only mode.
        partner = request.env.user.partner_id.commercial_partner_id
        service = BookingOrchestrationService(request.env)

        # Build pickup stops with coordinates when saved location is available
        # (postal may be empty for a coordinates-only request — the routing
        # service geocodes from latitude/longitude in that case)
        pickup_stops = [{"postal_code": pickup_fsa.fsa if pickup_fsa else ""}]
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
            # Single-stop: the global physical pallet count and total
            # shipment weight are authoritative SERVER-SIDE — stale hidden
            # per-stop fields (which can lag behind a Step-1 change) are
            # never trusted. Multi-stop keeps the per-stop form values.
            if len(delivery_loc_ids) > 1:
                stop_pallets = int(kwargs.get(f"delivery_pallets_{i+1}") or 1)
                stop_weight = float(kwargs.get(f"delivery_weight_{i+1}") or stop_pallets * 500)
            else:
                stop_pallets = physical_pallets
                stop_weight = weight_lbs
            stop_shared = _parse_bool(kwargs.get(f"delivery_shared_pallet_{i+1}"))
            timing_type = kwargs.get(f"delivery_timing_type_{i+1}") or "flexible"
            wstart = kwargs.get(f"delivery_window_start_{i+1}", "").strip()
            wend = kwargs.get(f"delivery_window_end_{i+1}", "").strip()
            stop = {
                "postal_code": dl.postal_code or "",
                "latitude": dl.latitude,
                "longitude": dl.longitude,
                "address": dl.street or "",
                "city": dl.city or "",
                "saved_location_id": dl.id,
                "pallets": stop_pallets,
                "weight_lbs": stop_weight,
                # Sharing a pallet across stops requires 2+ stops — a
                # single stop is never "shared".
                "shared_pallet": (stop_shared or shared_pallet_mode) if len(delivery_loc_ids) > 1 else False,
                "timing_type": timing_type,
                "window_start": _parse_time_float(wstart) if wstart else None,
                "window_end": _parse_time_float(wend) if wend else None,
                "timezone": dl.timezone or "America/Toronto",
                "liftgate_delivery": _parse_bool(kwargs.get(f"delivery_liftgate_{i+1}")),
                "appointment": _parse_bool(kwargs.get(f"delivery_appointment_{i+1}")),
                "instructions": kwargs.get(f"delivery_instructions_{i+1}", "").strip() or "",
            }
            delivery_stops.append(stop)

        # Fallback: single FSA-only mode
        if not delivery_stops:
            delivery_stops = [{"postal_code": delivery_fsa.fsa if delivery_fsa else ""}]

        # Also try coordinates from hidden form fields (mirror of the
        # pickup fallback above — keeps the delivery stop geocoded when
        # its saved location record has no stored coordinates).
        de_lat = kwargs.get("delivery_lat")
        de_lng = kwargs.get("delivery_lng")
        if de_lat and de_lng and delivery_stops and not delivery_stops[0].get("latitude"):
            delivery_stops[0]["latitude"] = float(de_lat)
            delivery_stops[0]["longitude"] = float(de_lng)

        # Generalized milk-run payload: ordered stops with stable stop keys
        # drive BOTH the pickup and delivery stop lists. Ownership of every
        # selected saved location is re-validated server-side.
        if route_stops and pallet_movements:
            gen_pickup_stops, gen_delivery_stops = [], []
            for rs in route_stops:
                loc = None
                loc_id = rs.get("saved_location_id")
                if loc_id:
                    loc = SavedLocation.browse(int(loc_id))
                    if not loc.exists() or loc.commercial_partner_id.id != partner.id:
                        return request.redirect("/my/booking/new")
                entry = {
                    "stop_key": rs.get("stop_key") or "",
                    "location_name": rs.get("location_name")
                        or (loc.business_name or loc.name if loc else ""),
                    "postal_code": (loc.postal_code if loc else rs.get("postal_code")) or "",
                    "latitude": (loc.latitude if loc else rs.get("latitude")) or 0.0,
                    "longitude": (loc.longitude if loc else rs.get("longitude")) or 0.0,
                    "address": (loc.street if loc else rs.get("address")) or "",
                    "city": (loc.city if loc else rs.get("city")) or "",
                    "saved_location_id": loc.id if loc else None,
                    "liftgate_required": bool(rs.get("liftgate_required")),
                    "dock_available": bool(rs.get("dock_available")),
                    "appointment_required": bool(rs.get("appointment_required")),
                    "timing_type": rs.get("timing_type") or "flexible",
                    "window_start": _parse_time_float(rs.get("window_start")) if rs.get("window_start") else None,
                    "window_end": _parse_time_float(rs.get("window_end")) if rs.get("window_end") else None,
                    "appointment_time": _parse_time_float(rs.get("appointment_time")) if rs.get("appointment_time") else None,
                    "service_time_minutes": int(rs.get("service_time_minutes") or 15),
                    "instructions": rs.get("instructions") or "",
                    "timezone": rs.get("timezone") or (loc.timezone if loc else "America/Toronto"),
                }
                if rs.get("stop_type") == "pickup":
                    gen_pickup_stops.append(entry)
                else:
                    gen_delivery_stops.append(entry)
            pickup_stops = gen_pickup_stops
            delivery_stops = gen_delivery_stops

        # A shared-pallet shipment requires at least two delivery stops —
        # "Shared Across 1 Stop" is never priced or displayed. Re-derived
        # from the FINAL stop list (which the milk-run payload may rebuild).
        if len(delivery_stops) <= 1:
            shared_pallet_mode = False

        # Requested pickup date from form
        requested_pickup_date = kwargs.get("requested_pickup_date", "").strip() or None

        # Calendar-selected EXACT departure (hidden input set when the
        # customer picks a card). Server-re-validated inside the routing
        # service (_build_leg): it must belong to the corridor serving this
        # route on the requested date, be active and still scheduled — an
        # arbitrary portal-supplied departure id is never trusted.
        pickup_departure_id = None
        raw_departure_id = str(kwargs.get("pickup_departure_id") or "").strip()
        if raw_departure_id and raw_departure_id.lstrip("-").isdigit():
            pickup_departure_id = int(raw_departure_id)

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
                "pallet_allocations": pallet_allocations,
                # Generalized milk-run: explicit architecture discriminator
                # + ordered stops + canonical movements.
                "route_model_version": (
                    "movement_v1" if (route_stops and pallet_movements) else "legacy"
                ),
                "route_stops": route_stops,
                "pallet_movements": pallet_movements,
                "weight_lbs": weight_lbs,
                "liftgate_pickup": liftgate_pickup,
                "liftgate_delivery": liftgate_delivery,
                "appointment": appointment,
                "residential": residential,
                "same_day_requested": same_day_requested,
                "pricing_method": "corridor",
                "requested_pickup_date": requested_pickup_date,
            }, source_channel="portal")
            quote = service.prepare_quote(normalized, requested_departure_id=pickup_departure_id)
        except UserError as exc:
            return request.render("prema_logistics_booking.portal_not_available", {
                "reason": str(exc),
            })
        except Exception:
            import traceback, secrets, logging
            _logger = logging.getLogger(__name__)
            error_ref = f"ERR-{secrets.token_hex(4)[:8]}"
            _logger.exception("booking_quote unexpected error [%s]", error_ref)
            return request.render("prema_logistics_booking.portal_booking_error", {
                "message": _("We couldn't calculate this shipment right now. Please try again or contact us."),
                "error_ref": error_ref,
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
            "pickup_loc": pickup_loc,
            "delivery_stops": _allocated_stop_weights(session, delivery_stops),
            "stop_pricing": _build_stop_pricing(session),
            "quote": quote,
        })

    # ------------------------------------------------------------------
    # Step 4: confirm (full addresses -> atomic transaction)
    # ------------------------------------------------------------------
    @http.route("/my/booking/capacity", type="http", auth="user", website=True, sitemap=False, methods=["GET"])
    def booking_capacity(self, **kwargs):
        """Dynamic remaining pallet capacity for the departure serving the
        pickup region on the requested date. Consumed by the Total Physical
        Pallets stepper (max, helper text, disabled state)."""
        require_visible()
        from ..services.region_resolver import RegionResolver
        from ..services.vehicle_capacity_service import VehicleCapacityService

        resolver = RegionResolver(request.env)
        region = False
        if kwargs.get("pickup_lat") and kwargs.get("pickup_lng"):
            try:
                match = resolver.resolve(
                    float(kwargs["pickup_lat"]), float(kwargs["pickup_lng"]))
                region = match.matched_region
            except (ValueError, TypeError):
                region = False
        if not region and kwargs.get("pickup_fsa"):
            # Coordinates outside every polygon (Mascouche J7K …): bridge
            # the FSA through the canonical mapping — never the raw
            # logistics.fsa.region_id (those rows still carry OLD region
            # ids, which no corridor stop references).
            region = resolver.canonical_region(str(kwargs["pickup_fsa"]).strip().upper())
        if not region and kwargs.get("pickup_loc_id"):
            raw = str(kwargs["pickup_loc_id"]).strip()
            if raw.lstrip("-").isdigit():
                partner = request.env.user.partner_id.commercial_partner_id
                loc = request.env["logistics.saved.location"].sudo().browse(int(raw))
                if loc.exists() and loc.commercial_partner_id.id == partner.id and loc.postal_code:
                    region = resolver.canonical_region(loc.postal_code)

        # The calendar binds the stepper to the EXACT departure the
        # customer selected — never an independently re-searched one.
        # Server-validated: active, still scheduled, vehicle assigned.
        #
        # DISCLOSURE POLICY: the response carries ONLY generic state
        # (available / capacity_state / can_fit_requested_quantity) — never
        # max_pallets, reserved_pallets, remaining_pallets, layouts, or any
        # other exact-capacity number. The customer needs a yes/no for the
        # quantity they entered; the authoritative capacity enforcement
        # stays server-side (Get Price pre-check + confirmation lock).
        requested_departure_id = kwargs.get("departure_id")
        requested_pallets = None
        raw_pallets = kwargs.get("pallets")
        if raw_pallets not in (None, ""):
            try:
                requested_pallets = int(raw_pallets)
            except (ValueError, TypeError):
                requested_pallets = None
        Departure = request.env["logistics.corridor.departure"].sudo()
        if requested_departure_id and str(requested_departure_id).lstrip("-").isdigit():
            departure = Departure.browse(int(requested_departure_id)).exists()
            if (departure
                    and departure.active
                    and departure.status not in ("cancelled", "completed")
                    and departure.vehicle_id):
                capacity_result = VehicleCapacityService(request.env).evaluate(
                    departure.vehicle_id, departure, 0)
                remaining = capacity_result["remaining_pallets"]
                corridor = departure.corridor_id
                data = {
                    "available": True,
                    "departure_id": departure.id,
                    "capacity_state": "available" if remaining > 0 else "full",
                    "can_fit_requested_quantity": (
                        requested_pallets is None or remaining >= requested_pallets),
                    # Per-pallet default weight from the corridor's own
                    # configuration — the portal weight auto-calc source.
                    # (Weight guidance, not capacity disclosure.)
                    "per_pallet_weight": (
                        corridor.included_weight_per_pallet or 0.0) if corridor else 0.0,
                }
                return request.make_json_response(data)

        result = VehicleCapacityService.for_pickup_date(
            request.env, region, kwargs.get("pickup_date"))
        remaining = result.get("remaining_pallets", 0)
        data = {
            "available": bool(result.get("available")),
            "departure_id": result.get("departure_id") or False,
            "capacity_state": (
                "full" if not result.get("available") or remaining <= 0 else "available"),
            "can_fit_requested_quantity": (
                bool(result.get("available"))
                and (requested_pallets is None or remaining >= requested_pallets)),
            "per_pallet_weight": result.get("per_pallet_weight", 0.0),
        }
        return request.make_json_response(data)

    @http.route("/my/booking/confirm", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_confirm(self, **kwargs):
        require_visible()

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
        partner = request.env.user.partner_id.commercial_partner_id
        filter_status = kwargs.get("status", "all")
        domain = [("commercial_partner_id", "=", partner.id)]
        if filter_status == "ongoing":
            domain.append(("state", "not in", ("cancelled", "completed", "delivered")))
        elif filter_status == "completed":
            domain.append(("state", "in", ("completed", "delivered")))
        elif filter_status == "cancelled":
            domain.append(("state", "=", "cancelled"))
        bookings = request.env["logistics.booking"].sudo().with_context(active_test=False).search(
            domain, order="pickup_date asc, id desc"
        )
        return request.render("prema_logistics_booking.portal_my_bookings", {
            "bookings": bookings,
            "filter_status": filter_status,
        })

    @http.route("/my/bookings/<int:booking_id>", type="http", auth="user", website=True, sitemap=False)
    def my_booking_detail(self, booking_id, **kwargs):
        require_visible()
        partner = request.env.user.partner_id.commercial_partner_id
        booking = request.env["logistics.booking"].sudo().search([
            ("id", "=", booking_id),
            ("commercial_partner_id", "=", partner.id),
        ], limit=1)
        if not booking:
            # Explicit ownership domain -- an id belonging to another
            # customer simply won't be found.
            raise NotFound()
        try:
            return request.render("prema_logistics_booking.portal_booking_detail", {"booking": booking})
        except Exception:
            logging.getLogger(__name__).exception(
                "portal booking detail render failed for booking %s", booking_id
            )
            raise

    # ------------------------------------------------------------------
    # Where We Go — Customer Portal
    # ------------------------------------------------------------------
    @http.route("/my/network-topology", type="json", auth="user")
    def portal_network_topology(self, **kwargs):
        """Portal-safe corridor topology. No dispatch staff required."""
        require_visible()
        Corridor = request.env["logistics.corridor"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Region = request.env["logistics.region"].sudo()
        hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return {"error": "No hub found"}
        hp = {
            "id": hub.id, "name": hub.public_name or hub.name,
            "lat": hub.latitude or (hub.saved_location_id.pin_lat if hub.saved_location_id else 0),
            "lng": hub.longitude or (hub.saved_location_id.pin_lng if hub.saved_location_id else 0),
        }
        stops = request.env["logistics.corridor.stop"].sudo().search(
            [("active", "=", True), ("corridor_id.active", "=", True)],
            order="corridor_id, sequence")
        cids = list(set(stops.mapped("corridor_id").ids))
        DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        clist, rids = [], set()
        for c in Corridor.search([("id", "in", cids), ("active", "=", True)]):
            rs = []
            for s in stops.filtered(lambda s, cid=c.id: s.corridor_id.id == cid):
                r = s.region_id; rids.add(r.id)
                rs.append({"region_id":r.id,"code":r.code,"name":r.name,
                    "lat":r.marker_latitude,"lng":r.marker_longitude})
            od = [d[:3] for d in DAYS if getattr(c, f"operate_{d}")]
            clist.append({"id":c.id,"name":c.name,"operating_days":od,"regions":rs})
        regs = []
        for r in Region.search([("id","in",list(rids))]):
            regs.append({"id":r.id,"code":r.code,"name":r.name,
                "lat":r.marker_latitude,"lng":r.marker_longitude,"main_city":r.main_city or ""})
        return {"hub":hp,"corridors":clist,"regions":regs}

    @http.route("/my/where-we-go", type="http", auth="user", website=True, sitemap=False)
    def portal_where_we_go(self, **kwargs):
        require_visible()
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return request.render("prema_logistics_booking.portal_where_we_go", {
            "google_maps_api_key": api_key,
        })

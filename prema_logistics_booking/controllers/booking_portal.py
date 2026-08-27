import json
import logging
import re
import traceback
from urllib.parse import urlencode

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
    ftl_rows = []
    route_transportation = 0.0
    leg_lines = []
    for line in session.price_snapshot or []:
        if not isinstance(line, dict):
            continue
        label = line.get("label", "")
        amount = line.get("amount", 0.0) or 0.0
        if label.startswith("Leg "):
            leg_lines.append(amount)
        elif label.startswith(("Dedicated FTL Transportation",
                               "Additional Regional Delivery — ",
                               "Additional Same-Region Delivery — ")):
            # Frozen FTL multi-stop breakdown (built server-side at quote
            # time) — rendered under "DEDICATED FTL SERVICE".
            ftl_rows.append({"label": label, "amount": amount})
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
    # Route label: pickup city → stop cities (display only). The
    # canonical access row is the preferred pickup source.
    route_label = ""
    if route_transportation:
        pickup_city = ""
        if session.pickup_customer_access_id:
            pickup_city = session.pickup_customer_access_id.city or ""
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
    ftl_total = round(
        sum(float(f.get("amount") or 0.0) for f in ftl_rows), 2)
    breakdown_total = round(base_total + weight_total + booking_total + ftl_total, 2)
    calculated = float(session.calculated_price or 0.0)
    if calculated and abs(breakdown_total - calculated) > 0.01:
        logging.getLogger(__name__).warning(
            "Step-3 breakdown mismatch: breakdown=%s calculated_price=%s "
            "snapshot=%s", breakdown_total, calculated, session.price_snapshot)
    return {
        "stops": stops,
        "booking_level": booking_level,
        "ftl_rows": ftl_rows,
        "route_transportation": route_transportation,
        "route_label": route_label,
        "legs": legs,
        "weight_rows": weight_rows,
        "base_total": base_total,
        "weight_total": weight_total,
        "ftl_total": ftl_total,
        "breakdown_total": breakdown_total,
        "total": calculated,
    }


def _saved_locations_builder_payload(partner):
    """JSON payload for the portal route builder: every saved location
    owned by the customer (access rows canonical-first, legacy rows
    during the transition window) with coordinates and operating hours,
    so stop cards can offer location selection and show facility hours
    without extra round-trips."""
    payload = []
    for loc in _partner_locations(request.env, partner):
        eff = _portal_coord_pair(loc)
        payload.append({
            "id": loc.id,
            "name": loc.name or "",
            "business_name": loc.business_name or "",
            "city": loc.city or "",
            "postal_code": loc.postal_code or "",
            "latitude": eff[0] or 0.0,
            "longitude": eff[1] or 0.0,
            "timezone": loc.timezone or "America/Toronto",
            "location_type": loc.location_type or "both",
            "liftgate_required": bool(loc.liftgate_required),
            "dock_info": bool(loc.dock_info),
            "hours": _loc_hours_by_day(loc),
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


def _quote_error_context(kwargs, partner, pickup_fsa, delivery_fsa, error):
    """Re-render quote validation errors from the complete submitted route.

    The error page is still only a form view: the quote endpoint remains the
    authority.  Keeping the generalized JSON and every indexed stop here
    prevents a failed quote from falling back to the legacy first pickup and
    first delivery fields on the next click.
    """
    pickup_loc_id = kwargs.get("pickup_loc_id")
    delivery_loc_id = kwargs.get("delivery_loc_id")
    pickup_loc_ids = _indexed_ints(kwargs, "pickup_loc_id_")
    delivery_loc_ids = _indexed_ints(kwargs, "delivery_loc_id_")
    if not pickup_loc_ids and pickup_loc_id:
        try:
            pickup_loc_ids = [int(pickup_loc_id)]
        except (TypeError, ValueError):
            pickup_loc_ids = []
    if not delivery_loc_ids and delivery_loc_id:
        try:
            delivery_loc_ids = [int(delivery_loc_id)]
        except (TypeError, ValueError):
            delivery_loc_ids = []

    pickup_stop_keys = _indexed_keys(kwargs, "pickup_stop_key_")
    delivery_stop_keys = _indexed_keys(kwargs, "delivery_stop_key_")
    route_stops_json = str(kwargs.get("route_stops_json") or "").strip()
    movements_json = str(kwargs.get("pallet_movements_json") or "").strip()
    allocations_json = str(kwargs.get("pallet_allocations_json") or "[]").strip()
    try:
        route_stops = json.loads(route_stops_json) if route_stops_json else []
    except (json.JSONDecodeError, TypeError):
        route_stops = []
    if not isinstance(route_stops, list) or not route_stops:
        route_stops = []
        for index, loc_id in enumerate(pickup_loc_ids, 1):
            route_stops.append({
                "stop_key": _safe_stop_key(
                    pickup_stop_keys[index - 1] if index <= len(pickup_stop_keys) else "",
                    "pickup", index),
                "stop_type": "pickup", "saved_location_id": loc_id,
            })
        for index, loc_id in enumerate(delivery_loc_ids, 1):
            route_stops.append({
                "stop_key": _safe_stop_key(
                    delivery_stop_keys[index - 1] if index <= len(delivery_stop_keys) else "",
                    "delivery", index),
                "stop_type": "delivery", "saved_location_id": loc_id,
            })

    def submitted_value(value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (list, tuple)):
            return [submitted_value(item) for item in value]
        return str(value)

    submitted = {str(key): submitted_value(value) for key, value in kwargs.items()
                 if key != "csrf_token"}
    physical_pallets = kwargs.get("physical_pallets") or kwargs.get("pallets") or 1
    try:
        physical_pallets = int(physical_pallets)
    except (TypeError, ValueError):
        physical_pallets = 1
    try:
        weight_lbs = float(kwargs.get("weight_lbs") or 0)
    except (TypeError, ValueError):
        weight_lbs = 0.0
    pickup_loc = _resolve_loc(request.env, partner, pickup_loc_id) if pickup_loc_id else None
    pickup_locs = [
        loc for loc_id in pickup_loc_ids
        if (loc := _resolve_loc(request.env, partner, loc_id))
    ]
    if not pickup_locs and pickup_loc:
        pickup_locs = [pickup_loc]
    delivery_locs = [
        loc for loc_id in delivery_loc_ids
        if (loc := _resolve_loc(request.env, partner, loc_id))
    ]
    first_delivery = delivery_locs[0] if delivery_locs else None
    pickup_payload = None
    if pickup_loc:
        eff = _portal_coord_pair(pickup_loc)
        pickup_payload = {
            "id": pickup_loc.id, "name": pickup_loc.name or "",
            "business_name": pickup_loc.business_name or "", "city": pickup_loc.city or "",
            "latitude": eff[0], "longitude": eff[1],
        }
    delivery_payload = []
    for loc in delivery_locs:
        eff = _portal_coord_pair(loc)
        delivery_payload.append({
            "id": loc.id, "name": loc.name or "",
            "business_name": loc.business_name or "", "city": loc.city or "",
            "latitude": eff[0], "longitude": eff[1],
        })
    return {
        "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
        "pickup_loc": pickup_loc, "pickup_locs": pickup_locs,
        "delivery_loc": first_delivery,
        "delivery_locs": delivery_locs, "pickup_loc_ids": pickup_loc_ids,
        "delivery_loc_ids": delivery_loc_ids, "pickup_loc_id": pickup_loc_id,
        "delivery_loc_id": delivery_loc_id,
        "pickup_stop_keys": pickup_stop_keys, "delivery_stop_keys": delivery_stop_keys,
        "pickup_lat": float(kwargs.get("pickup_lat") or 0) if kwargs.get("pickup_lat") else 0,
        "pickup_lng": float(kwargs.get("pickup_lng") or 0) if kwargs.get("pickup_lng") else 0,
        "delivery_lat": float(kwargs.get("delivery_lat") or 0) if kwargs.get("delivery_lat") else 0,
        "delivery_lng": float(kwargs.get("delivery_lng") or 0) if kwargs.get("delivery_lng") else 0,
        "initial_route_stops_json": json.dumps(route_stops),
        "saved_locations_json": json.dumps(_saved_locations_builder_payload(partner)),
        "delivery_locs_json": json.dumps(delivery_payload),
        "pickup_loc_json": json.dumps(pickup_payload),
        "route_stops_json": route_stops_json,
        "pallet_movements_json": movements_json,
        "pallet_allocations_json": allocations_json,
        "submitted_form_json": json.dumps(submitted),
        "pallets": physical_pallets, "physical_pallets": physical_pallets,
        "weight_lbs": weight_lbs, "shipment_type": kwargs.get("shipment_type") or "ltl",
        "temperature_mode": kwargs.get("temperature_mode") or "dry",
        "required_temperature_c": kwargs.get("required_temperature_c") or "",
        "pallet_weight_mode": kwargs.get("pallet_weight_mode") or "auto",
        "error": error,
    }

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


def _portal_coord_pair(loc):
    """Effective (latitude, longitude) for a saved location, or (None, None).

    Delegates to the model's canonical helper so every portal consumer
    (validation, redirect URL, stops, JSON markers) resolves the SAME
    coordinates: linked Master Facility pin pair first, own lat/lng
    second. A copy whose duplicated fields are stale or 0/0 placeholders
    still routes via its master. Access rows implement the same contract
    (physical pins from the canonical facility)."""
    if not loc:
        return (None, None)
    eff = loc._get_effective_coordinates()
    # Tuple contract (lat, lng) — the canonical helper on access rows
    # returns a pair, not a dict. Dict-style indexing here crashed every
    # portal booking request with TypeError (caught by the demo route
    # matrix 2026-08-25).
    return (eff[0], eff[1])


def _resolve_loc(env, partner, loc_id):
    """Resolve a submitted location id to the canonical record (SAVED
    LOCATION CONSOLIDATION §14).

    Portal location ids are logistics.location.customer.access rows
    (access, whose id may also be referenced as `new_loc_id` / loc
    payload ids). Ownership is enforced here — records that do not
    belong to the partner resolve to an empty recordset. The legacy
    logistics.saved.location fallback was retired in 18.0.13.25.0."""
    Access = env["logistics.location.customer.access"].sudo()
    try:
        raw_id = int(loc_id or 0)
    except (TypeError, ValueError):
        return Access.browse()
    if raw_id:
        acc = Access.browse(raw_id)
        if acc.exists() and acc.active and acc.commercial_partner_id.id == partner.id:
            return acc
    return Access.browse()


def _indexed_ints(kwargs, prefix):
    """Return indexed integer form values in DOM order.

    Portal stop rows are reorderable.  The field suffix is only the current
    form position; the stable stop key travels with the row and is handled
    separately.  Keeping this helper deterministic also prevents a sparse or
    malicious field set from silently changing the selected route.
    """
    values = []
    def index_key(item):
        match = re.search(r"_(\d+)$", item[0])
        return int(match.group(1)) if match else 0
    for key, value in sorted(kwargs.items(), key=index_key):
        if not key.startswith(prefix) or not value:
            continue
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    return values


def _indexed_keys(kwargs, prefix):
    """Return non-empty stable stop keys in indexed form order."""
    values = []
    def index_key(item):
        match = re.search(r"_(\d+)$", item[0])
        return int(match.group(1)) if match else 0
    for key, value in sorted(kwargs.items(), key=index_key):
        if key.startswith(prefix) and value:
            values.append(str(value).strip())
    return values


def _safe_stop_key(value, stop_type, fallback_number):
    """Normalize a portal stop key without trusting its visual position."""
    value = str(value or "").strip()
    prefix = "PU" if stop_type == "pickup" else "DL"
    if not re.fullmatch(r"%s[A-Za-z0-9_-]{0,30}" % prefix, value):
        return "%s%d" % (prefix, fallback_number)
    return value


def _validate_movement_payload(route_stops, pallet_movements, physical_pallets):
    """Validate the movement_v1 graph before pricing or persistence.

    This is intentionally independent of ORM records: it rejects malformed
    JSON before any route engine can infer a default pickup or delivery.
    Ownership and saved-location capability checks happen when the stop IDs
    are resolved by the portal controller.
    """
    if not isinstance(route_stops, list) or not isinstance(pallet_movements, list):
        raise UserError(_("The route stops or pallet movements are invalid."))
    if len(pallet_movements) != int(physical_pallets):
        raise UserError(_("Every physical pallet must have one movement."))
    keys = [str(stop.get("stop_key") or "").strip() for stop in route_stops
            if isinstance(stop, dict)]
    if len(keys) != len(route_stops) or not all(keys) or len(set(keys)) != len(keys):
        raise UserError(_("Each route stop must have a unique stable key."))
    by_key = {stop["stop_key"]: stop for stop in route_stops}
    if any(stop.get("stop_type") not in ("pickup", "delivery")
           for stop in route_stops):
        raise UserError(_("Route stops must be pickup or delivery stops."))
    pickup_keys = {key for key, stop in by_key.items()
                   if stop.get("stop_type") == "pickup"}
    delivery_keys = {key for key, stop in by_key.items()
                     if stop.get("stop_type") == "delivery"}
    if not pickup_keys or not delivery_keys:
        raise UserError(_("A booking requires at least one pickup and one delivery stop."))
    movement_keys = set()
    for movement in pallet_movements:
        if not isinstance(movement, dict):
            raise UserError(_("Each pallet movement must be an object."))
        movement_key = str(movement.get("key") or "").strip()
        pickup_key = str(movement.get("pickup_stop_key") or "").strip()
        destinations = movement.get("delivery_stop_keys") or []
        if not movement_key or movement_key in movement_keys:
            raise UserError(_("Each physical pallet must have a unique movement key."))
        if pickup_key not in pickup_keys:
            raise UserError(_("A pallet must reference one valid pickup stop."))
        if not isinstance(destinations, list) or not destinations:
            raise UserError(_("Every physical pallet must have at least one delivery stop."))
        if len(set(destinations)) != len(destinations) or any(
                destination not in delivery_keys for destination in destinations):
            raise UserError(_("A pallet references an invalid delivery stop."))
        try:
            if float(movement.get("weight_lbs") or 0) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise UserError(_("Pallet weights must be valid non-negative numbers."))
        movement_keys.add(movement_key)


def _partner_locations(env, partner, loc_type=None):
    """Every portal location of this customer: active access rows
    (logistics.location.customer.access — the sole portal location
    model since the legacy logistics.saved.location was retired in
    18.0.13.25.0). Access rows expose the physical/private field names
    via computed proxies, so templates and the sort below stay
    type-agnostic. Returns a Python list sorted default-first,
    last-used-first, then name."""
    Access = env["logistics.location.customer.access"].sudo()
    access_domain = [("commercial_partner_id", "=", partner.id), ("active", "=", True)]
    if loc_type == "pickup":
        access_domain.append(("can_pickup", "=", True))
    elif loc_type == "delivery":
        access_domain.append(("can_delivery", "=", True))

    merged = list(Access.search(access_domain))

    def _sort_key(r):
        if loc_type == "pickup":
            dflt = r.is_default_pickup
        elif loc_type == "delivery":
            dflt = r.is_default_delivery
        else:
            dflt = r.is_default_pickup or r.is_default_delivery
        last = r.last_used_date
        return (0 if dflt else 1,
                -last.timestamp() if last else -float("inf"),
                r.name or "")

    merged.sort(key=_sort_key)
    return merged


def _loc_hours_by_day(loc):
    """{day: [open, close] or None} operating hours for an access row:
    the CANONICAL facility hours (prema.dispatch.location.hours, general
    scope). The legacy logistics.saved.location.hours fallback was
    retired in 18.0.13.25.0."""
    CanH = request.env["prema.dispatch.location.hours"].sudo()
    rows = CanH.search([
        ("facility_id", "=", loc.facility_id.id if loc and loc.facility_id else -1),
        ("service_scope", "=", "general"),
        ("active", "=", True),
    ])
    hours = {}
    for day in range(7):
        day_rows = rows.filtered(lambda r, d=str(day): r.day_of_week == d)
        general = day_rows.filtered(lambda r: r.service_scope == "general") or day_rows[:1]
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
    return hours


def _stop_loc_refs(loc):
    """Canonical id keys for a stop dict: access rows carry
    customer_access_id (+facility_id), legacy rows carry
    saved_location_id. The orchestration service's _stop_saved_ids
    prefers the canonical branch."""
    if not loc:
        return {}
    if hasattr(loc, "facility_id") and loc.facility_id:
        return {"customer_access_id": loc.id, "facility_id": loc.facility_id.id}
    return {"saved_location_id": loc.id}

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

        error = None
        if request.httprequest.method == "POST":
            # Both stop groups use indexed fields.  The first legacy field is
            # accepted for old bookmarked/forms, but new rows carry a stable
            # key alongside their select so reorder/remove never changes a
            # pallet's source stop identity.
            pickup_loc_ids = _indexed_ints(kwargs, "pickup_saved_location_id_")
            pickup_loc_id = kwargs.get("pickup_saved_location_id")
            if not pickup_loc_ids and pickup_loc_id:
                try:
                    pickup_loc_ids = [int(pickup_loc_id)]
                except (TypeError, ValueError):
                    pickup_loc_ids = []
            pickup_stop_keys = _indexed_keys(kwargs, "pickup_stop_key_")
            pickup_postal = kwargs.get("pickup_postal_code")
            delivery_postal = kwargs.get("delivery_postal_code")

            # Collect delivery stop IDs from indexed form fields
            delivery_loc_ids = _indexed_ints(kwargs, "delivery_saved_location_id_")
            delivery_stop_keys = _indexed_keys(kwargs, "delivery_stop_key_")
            # Fallback: single legacy field
            if not delivery_loc_ids:
                single_del = kwargs.get("delivery_saved_location_id")
                if single_del:
                    try:
                        delivery_loc_ids.append(int(single_del))
                    except (ValueError, TypeError):
                        pass

            # Route 1: Saved Location selected (access rows canonical,
            # legacy rows during the transition window — _resolve_loc
            # enforces ownership on both).
            if pickup_loc_ids and delivery_loc_ids:
                pickup_locs = [
                    loc for loc_id in pickup_loc_ids
                    if (loc := _resolve_loc(request.env, partner, loc_id))
                ]
                delivery_locs = [
                    loc for loc_id in delivery_loc_ids
                    if (loc := _resolve_loc(request.env, partner, loc_id))
                ]
                # Effective coordinates: canonical facility pin pair
                # preferred, own lat/lng second — a copy with stale or 0/0
                # placeholder coordinates still validates via its master.
                pickup_loc = pickup_locs[0] if pickup_locs else None
                pu_eff = _portal_coord_pair(pickup_loc)
                de_effs = {dl.id: _portal_coord_pair(dl) for dl in delivery_locs}
                # Security: ensure all locations belong to this customer
                if len(pickup_locs) != len(pickup_loc_ids):
                    error = _("Invalid pickup location selection.")
                elif any(not loc.can_pickup for loc in pickup_locs):
                    error = _("Every pickup must be a valid pickup location.")
                elif any(not loc.can_delivery for loc in delivery_locs):
                    error = _("Every delivery must be a valid delivery location.")
                elif not pickup_loc:
                    error = _("Invalid pickup location selection.")
                elif len(delivery_locs) != len(delivery_loc_ids):
                    error = _("Invalid delivery location selection.")
                elif pu_eff[0] is None:
                    error = _("Pickup location must have valid coordinates.")
                elif any(eff[0] is None for eff in de_effs.values()):
                    error = _("All delivery locations must have valid coordinates.")
                else:
                    # Build a stable, ordered handoff to Step 2.  Keys are
                    # carried separately from the visual array position.
                    first_del = delivery_locs[0]
                    de_eff = de_effs[first_del.id]
                    params = {
                        "pickup_lat": pu_eff[0], "pickup_lng": pu_eff[1],
                        "delivery_lat": de_eff[0], "delivery_lng": de_eff[1],
                        "pickup_loc_id": pickup_locs[0].id,
                        "delivery_loc_id": first_del.id,
                    }
                    for i, loc in enumerate(pickup_locs):
                        params["pickup_loc_id_%d" % (i + 1)] = loc.id
                        params["pickup_stop_key_%d" % (i + 1)] = _safe_stop_key(
                            pickup_stop_keys[i] if i < len(pickup_stop_keys) else "",
                            "pickup", i + 1)
                    for i, loc in enumerate(delivery_locs):
                        params["delivery_loc_id_%d" % (i + 1)] = loc.id
                        params["delivery_stop_key_%d" % (i + 1)] = _safe_stop_key(
                            delivery_stop_keys[i] if i < len(delivery_stop_keys) else "",
                            "delivery", i + 1)
                    return request.redirect("/my/booking/details?%s" % urlencode(params))

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

        # Load customer's saved locations — canonical access rows +
        # legacy rows during the transition window (consolidation).
        pickup_locations = _partner_locations(request.env, partner, "pickup")
        delivery_locations = _partner_locations(request.env, partner, "delivery")

        # Handle return from Add New Location (auto-select newly created location)
        new_loc_id = kwargs.get("new_loc_id")
        new_loc_type = kwargs.get("new_loc_type", "")

        # Load customer's recent bookings for sidebar
        customer_bookings = request.env["logistics.booking"].sudo().search([
            ("commercial_partner_id", "=", partner.id),
        ], order="id desc", limit=10)

        # Preserve the customer's selections when a POST validation error
        # re-renders the page — never silently reset to defaults.
        selected_pickup_id = None
        selected_pickup_ids = []
        selected_pickup_rows = []
        selected_delivery_ids = []
        raw_pickup = kwargs.get("pickup_saved_location_id")
        if raw_pickup:
            try:
                selected_pickup_id = int(raw_pickup)
            except (ValueError, TypeError):
                selected_pickup_id = None
        posted_pickup_keys = _indexed_keys(kwargs, "pickup_stop_key_")
        for idx, loc_id in enumerate(_indexed_ints(kwargs, "pickup_saved_location_id_"), 1):
            selected_pickup_ids.append(loc_id)
            selected_pickup_rows.append({
                "loc_id": loc_id, "idx": idx,
                "stop_key": _safe_stop_key(
                    posted_pickup_keys[idx - 1] if idx <= len(posted_pickup_keys) else "",
                    "pickup", idx),
            })
        if not selected_pickup_ids and selected_pickup_id:
            selected_pickup_ids = [selected_pickup_id]
            selected_pickup_rows = [{"loc_id": selected_pickup_id, "idx": 1, "stop_key": "PU1"}]
        selected_delivery_keys = _indexed_keys(kwargs, "delivery_stop_key_")
        selected_delivery_ids = _indexed_ints(kwargs, "delivery_saved_location_id_")
        if not selected_delivery_ids:
            raw_single = kwargs.get("delivery_saved_location_id")
            if raw_single:
                try:
                    selected_delivery_ids.append(int(raw_single))
                except (ValueError, TypeError):
                    pass

        return request.render("prema_logistics_booking.portal_step1_locations", {
            "error": error,
            "pickup_locations": pickup_locations,
            "delivery_locations": delivery_locations,
            "has_saved_locations": bool(pickup_locations or delivery_locations),
            "new_loc_id": new_loc_id,
            "new_loc_type": new_loc_type,
            "customer_bookings": customer_bookings,
            "selected_pickup_id": selected_pickup_id,
            "selected_pickup_ids": selected_pickup_ids,
            "selected_pickup_rows": selected_pickup_rows,
            "selected_delivery_ids": selected_delivery_ids,
            "selected_delivery_rows": [
                {"loc_id": loc_id, "idx": i + 1,
                 "stop_key": _safe_stop_key(
                     selected_delivery_keys[i] if i < len(selected_delivery_keys) else "",
                     "delivery", i + 1)}
                for i, loc_id in enumerate(selected_delivery_ids)
            ],
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
        pickup_loc_ids = []
        raw_pickup_ids = kwargs.get("pickup_loc_ids", "")
        if isinstance(raw_pickup_ids, str):
            pickup_loc_ids = [int(x) for x in raw_pickup_ids.split(",")
                              if x.strip().lstrip("-").isdigit()]
        if not pickup_loc_ids and kwargs.get("pickup_loc_id"):
            try:
                pickup_loc_ids = [int(kwargs.get("pickup_loc_id"))]
            except (TypeError, ValueError):
                pickup_loc_ids = []
        # When Step 2 has a generalized route, it is the availability
        # authority too. Resolve every submitted stop through the customer's
        # canonical access rows; do not silently downgrade to the first
        # scalar pickup/delivery pair.
        route_stops_json = str(kwargs.get("route_stops_json") or "").strip()
        route_payload = None
        if route_stops_json:
            try:
                route_payload = json.loads(route_stops_json)
            except (json.JSONDecodeError, TypeError):
                route_payload = None
            if not isinstance(route_payload, list) or not route_payload:
                return request.make_response(
                    json.dumps({"dates": [], "manual_quote": True,
                                "reason": "Could not resolve the selected route stops."}),
                    headers=[("Content-Type", "application/json")],
                )
            route_stops = []
            seen_keys = set()
            for rs in route_payload:
                if not isinstance(rs, dict) or rs.get("stop_type") not in ("pickup", "delivery"):
                    route_stops = []
                    break
                stop_key = str(rs.get("stop_key") or "").strip()
                loc = _resolve_loc(request.env, partner, rs.get("saved_location_id"))
                if not stop_key or stop_key in seen_keys or not loc:
                    route_stops = []
                    break
                if (rs["stop_type"] == "pickup" and not loc.can_pickup) or (
                        rs["stop_type"] == "delivery" and not loc.can_delivery):
                    route_stops = []
                    break
                eff = _portal_coord_pair(loc)
                if eff[0] is None or eff[1] is None:
                    route_stops = []
                    break
                seen_keys.add(stop_key)
                route_stops.append({
                    "stop_key": stop_key,
                    "stop_type": rs["stop_type"],
                    "latitude": eff[0], "longitude": eff[1],
                    "postal_code": loc.postal_code or "",
                    **_stop_loc_refs(loc),
                })
            if not route_stops or not any(s["stop_type"] == "pickup" for s in route_stops) \
                    or not any(s["stop_type"] == "delivery" for s in route_stops):
                return request.make_response(
                    json.dumps({"dates": [], "manual_quote": True,
                                "reason": "Could not resolve the selected route stops."}),
                    headers=[("Content-Type", "application/json")],
                )
            stops = route_stops

        if route_payload is None and pickup_loc_ids and delivery_loc_ids:
            pickup_locs = [_resolve_loc(request.env, partner, loc_id)
                           for loc_id in pickup_loc_ids]
            delivery_locs = [
                loc for loc_id in delivery_loc_ids
                if (loc := _resolve_loc(request.env, partner, loc_id))
            ]
            if pickup_locs:
                pu_eff = _portal_coord_pair(pickup_locs[0])
            else:
                pu_eff = (None, None)
            if (len(pickup_locs) == len(pickup_loc_ids)
                    and all(loc and loc.can_pickup for loc in pickup_locs)
                    and all(loc.can_delivery for loc in delivery_locs)
                    and len(delivery_locs) == len(delivery_loc_ids)
                    and pu_eff[0] is not None
                    and all(_portal_coord_pair(dl)[0] is not None for dl in delivery_locs)):
                for pickup_loc in pickup_locs:
                    pu_eff = _portal_coord_pair(pickup_loc)
                    stops.append({
                        "stop_type": "pickup",
                        "latitude": pu_eff[0], "longitude": pu_eff[1],
                        "postal_code": pickup_loc.postal_code or "",
                        **_stop_loc_refs(pickup_loc),
                    })
                for dl in delivery_locs:
                    de_eff = _portal_coord_pair(dl)
                    stops.append({
                        "stop_type": "delivery",
                        "latitude": de_eff[0],
                        "longitude": de_eff[1],
                        "city": dl.city or "",
                        "postal_code": dl.postal_code or "",
                        **_stop_loc_refs(dl),
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
        # The customer's explicit Shipment Type selection is authoritative
        # for the calendar — never inferred from pallet count. FTL only
        # receives dedicated direct dates (same verdict as Get Price).
        shipment_type = kwargs.get("shipment_type", "ltl")
        if shipment_type not in ("ltl", "ftl"):
            shipment_type = "ltl"
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
            shipment_type=shipment_type,
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

        # Collect complete ordered stop selections.  The first scalar fields
        # are compatibility inputs; indexed values are the canonical Step 1
        # handoff and preserve the customer's reordered rows.
        delivery_loc_ids = _indexed_ints(kwargs, "delivery_loc_id_")
        if not delivery_loc_ids and delivery_loc_id:
            try:
                delivery_loc_ids = [int(delivery_loc_id)]
            except (TypeError, ValueError):
                delivery_loc_ids = []
        pickup_loc_ids = _indexed_ints(kwargs, "pickup_loc_id_")
        if not pickup_loc_ids and pickup_loc_id:
            try:
                pickup_loc_ids = [int(pickup_loc_id)]
            except (TypeError, ValueError):
                pickup_loc_ids = []
        pickup_stop_keys = _indexed_keys(kwargs, "pickup_stop_key_")
        delivery_stop_keys = _indexed_keys(kwargs, "delivery_stop_key_")

        # The first ordered pickup is the compatibility origin for the
        # availability API, but it is derived from the generalized stop list
        # rather than allowing a missing scalar field to erase a multi-stop
        # route.
        if not pickup_loc_id and pickup_loc_ids:
            pickup_loc_id = pickup_loc_ids[0]

        Fsa = request.env["logistics.fsa"].sudo()
        partner = request.env.user.partner_id.commercial_partner_id

        # Route A: access-row location with coordinates
        if pickup_lat and delivery_lat:
            pickup_loc = None
            delivery_locs = []
            pickup_fsa = None
            delivery_fsa = None

            # Fetch pickup saved location (access row canonical, legacy
            # row during the transition window — ownership enforced).
            if pickup_loc_id:
                pickup_loc = _resolve_loc(request.env, partner, pickup_loc_id)
                if not pickup_loc:
                    return request.redirect("/my/booking/new")

            # Fetch all delivery saved locations
            if delivery_loc_ids:
                delivery_locs = [
                    loc for loc_id in delivery_loc_ids
                    if (loc := _resolve_loc(request.env, partner, loc_id))
                ]
                if len(delivery_locs) != len(delivery_loc_ids):
                    return request.redirect("/my/booking/new")
            elif delivery_loc_id:
                single = _resolve_loc(request.env, partner, delivery_loc_id)
                if single:
                    delivery_locs = [single]

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
                de_eff = _portal_coord_pair(first_delivery)
                de_result = resolver.resolve(de_eff[0] or 0, de_eff[1] or 0)
                if de_result.matched_region_code:
                    delivery_fsa = Fsa.search([("region_id.code", "=", de_result.matched_region_code)], limit=1)

            # delivery_loc_id may be None if Step 1 only passed indexed
            # delivery_loc_id_N params (multi-stop URL scheme). Derive it
            # from the first entry so the single-stop template branch still
            # renders the hidden form field.
            if not delivery_loc_id and delivery_loc_ids:
                delivery_loc_id = delivery_loc_ids[0]

            # Build template context (effective coordinates — master pin
            # pair preferred, so stale/0/0 copies still place correctly)
            delivery_locs_payload = []
            for dl in delivery_locs:
                de_eff = _portal_coord_pair(dl)
                delivery_locs_payload.append({
                    "id": dl.id,
                    "name": dl.name or "",
                    "business_name": dl.business_name or "",
                    "city": dl.city or "",
                    "latitude": de_eff[0],
                    "longitude": de_eff[1],
                })
            pickup_loc_payload = None
            if pickup_loc and pickup_loc.exists():
                pu_eff = _portal_coord_pair(pickup_loc)
                pickup_loc_payload = {
                    "id": pickup_loc.id,
                    "name": pickup_loc.name or "",
                    "business_name": pickup_loc.business_name or "",
                    "city": pickup_loc.city or "",
                    "latitude": pu_eff[0],
                    "longitude": pu_eff[1],
                }
            initial_route_stops = []
            pickup_locs = []
            for i, loc in enumerate([_resolve_loc(request.env, partner, loc_id)
                                     for loc_id in pickup_loc_ids], 1):
                if not loc:
                    return request.redirect("/my/booking/new")
                pickup_locs.append(loc)
                pu_eff = _portal_coord_pair(loc)
                initial_route_stops.append({
                    "stop_key": _safe_stop_key(
                        pickup_stop_keys[i - 1] if i <= len(pickup_stop_keys) else "",
                        "pickup", i),
                    "stop_type": "pickup", "saved_location_id": loc.id,
                    "latitude": pu_eff[0], "longitude": pu_eff[1],
                    "postal_code": loc.postal_code or "",
                    **_stop_loc_refs(loc),
                })
            for i, loc in enumerate(delivery_locs, 1):
                de_eff = _portal_coord_pair(loc)
                initial_route_stops.append({
                    "stop_key": _safe_stop_key(
                        delivery_stop_keys[i - 1] if i <= len(delivery_stop_keys) else "",
                        "delivery", i),
                    "stop_type": "delivery", "saved_location_id": loc.id,
                    "latitude": de_eff[0], "longitude": de_eff[1],
                    "postal_code": loc.postal_code or "",
                    **_stop_loc_refs(loc),
                })
            return request.render("prema_logistics_booking.portal_step2_shipment", {
                "pickup_fsa": pickup_fsa, "delivery_fsa": delivery_fsa,
                "pickup_loc": pickup_loc, "pickup_locs": pickup_locs,
                "delivery_locs": delivery_locs,
                "delivery_loc": first_delivery,
                "pickup_lat": float(pickup_lat), "pickup_lng": float(pickup_lng or 0),
                "delivery_lat": float(delivery_lat), "delivery_lng": float(delivery_lng or 0),
                "pickup_loc_id": pickup_loc_id, "delivery_loc_ids": delivery_loc_ids,
                "delivery_loc_id": delivery_loc_id,
                "pickup_loc_ids": pickup_loc_ids,
                "pickup_stop_keys": pickup_stop_keys,
                "delivery_stop_keys": delivery_stop_keys,
                "initial_route_stops_json": json.dumps(initial_route_stops),
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
            "pickup_loc": None, "pickup_locs": [], "delivery_loc": None,
            "pickup_lat": 0, "pickup_lng": 0,
            "delivery_lat": 0, "delivery_lng": 0,
            "pickup_loc_id": None, "delivery_loc_id": None,
            "pickup_loc_ids": [], "delivery_loc_ids": [],
            "pickup_stop_keys": [], "delivery_stop_keys": [],
            "initial_route_stops_json": "[]",
            "saved_locations_json": json.dumps(
                _saved_locations_builder_payload(partner)),
        })

    @http.route("/my/booking/quote", type="http", auth="user", website=True, sitemap=False, methods=["POST"])
    def booking_quote(self, **kwargs):
        require_visible()

        Fsa = request.env["logistics.fsa"].sudo()
        partner = request.env.user.partner_id.commercial_partner_id

        # Resolve pickup FSA: prefer the access row (_resolve_loc enforces
        # ownership), fall back to FSA code.
        pickup_fsa = None
        pickup_loc_id = kwargs.get("pickup_loc_id")
        if pickup_loc_id:
            pickup_loc = _resolve_loc(request.env, partner, pickup_loc_id)
            if pickup_loc:
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
            delivery_loc = _resolve_loc(request.env, partner, delivery_loc_id)
            if delivery_loc:
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
            if route_stops_json or movements_json:
                if not (route_stops_json and movements_json):
                    raise UserError(_("The route stops and pallet movements must be submitted together."))
                try:
                    route_stops = json.loads(route_stops_json)
                    pallet_movements = json.loads(movements_json)
                except (json.JSONDecodeError, TypeError):
                    raise UserError(_("The route stops or pallet movements are invalid."))
                _validate_movement_payload(
                    route_stops, pallet_movements, physical_pallets)
                if str(kwargs.get("pallet_weight_mode") or "auto").strip().lower() == "manual":
                    movement_weight_total = sum(
                        float(movement.get("weight_lbs") or 0.0)
                        for movement in pallet_movements
                    )
                    if abs(movement_weight_total - weight_lbs) > 0.05:
                        raise UserError(_(
                            "Manual pallet weights must equal the total shipment weight."))
                posted_pickup_ids = set(_indexed_ints(
                    kwargs, "pickup_loc_id_"))
                posted_delivery_ids = set(_indexed_ints(
                    kwargs, "delivery_loc_id_"))
                route_pickup_ids = {
                    int(stop.get("saved_location_id"))
                    for stop in route_stops
                    if stop.get("stop_type") == "pickup"
                    and str(stop.get("saved_location_id") or "").lstrip("-").isdigit()
                }
                route_delivery_ids = {
                    int(stop.get("saved_location_id"))
                    for stop in route_stops
                    if stop.get("stop_type") == "delivery"
                    and str(stop.get("saved_location_id") or "").lstrip("-").isdigit()
                }
                if ((posted_pickup_ids and posted_pickup_ids != route_pickup_ids)
                        or (posted_delivery_ids and posted_delivery_ids != route_delivery_ids)):
                    raise UserError(_(
                        "The submitted movement stops do not match the selected locations."))
                for stop in route_stops:
                    stop_type = stop.get("stop_type")
                    location = _resolve_loc(
                        request.env, partner, stop.get("saved_location_id"))
                    if not location or (
                            stop_type == "pickup" and not location.can_pickup) or (
                            stop_type == "delivery" and not location.can_delivery):
                        raise UserError(_("Every route stop must use a valid customer saved location."))
            elif len(_indexed_ints(kwargs, "pickup_loc_id_")) > 1:
                raise UserError(_(
                    "Multiple pickup stops require pallet movement assignments."))
        except (TypeError, ValueError, UserError) as exc:
            error = str(exc) if isinstance(exc, UserError) else _(
                "Please enter a valid pallet count and weight.")
            return request.render("prema_logistics_booking.portal_step2_shipment",
                                  _quote_error_context(
                                      kwargs, partner, pickup_fsa, delivery_fsa, error))

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
                    return request.render(
                        "prema_logistics_booking.portal_step2_shipment",
                        _quote_error_context(
                            kwargs, partner, pickup_fsa, delivery_fsa, _(
                                "This pallet quantity is not available on the "
                                "selected departure. Reduce the quantity or "
                                "choose another pickup date.",
                            )),
                    )
            except (ValueError, TypeError):
                pass

        shipment_type = kwargs.get("shipment_type") or "ltl"
        temperature_mode = kwargs.get("temperature_mode") or "dry"
        from ..services.temperature_compat import parse_required_temperature_c
        if temperature_mode != "reefer":
            # A Dry booking never carries a temperature requirement — drop any
            # stale value (the UI clears it too, this is the server authority).
            required_temperature_c = None
        else:
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
            pu_loc = _resolve_loc(request.env, partner, pickup_loc_id)
            if pu_loc:
                pu_eff = _portal_coord_pair(pu_loc)
                if pu_eff[0] is not None:
                    pickup_stops[0]["latitude"] = pu_eff[0]
                    pickup_stops[0]["longitude"] = pu_eff[1]
                pickup_stops[0]["address"] = pu_loc.street or ""
                pickup_stops[0]["city"] = pu_loc.city or ""
                pickup_stops[0].update(_stop_loc_refs(pu_loc))
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

        # Build delivery stops with per-stop data (access rows canonical,
        # legacy rows during the transition window — _resolve_loc
        # enforces ownership on both).
        delivery_stops = []
        for i, dl_id in enumerate(delivery_loc_ids):
            dl = _resolve_loc(request.env, partner, dl_id)
            if not dl:
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
            de_eff = _portal_coord_pair(dl)
            stop = {
                "postal_code": dl.postal_code or "",
                "latitude": de_eff[0],
                "longitude": de_eff[1],
                "address": dl.street or "",
                "city": dl.city or "",
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
                **_stop_loc_refs(dl),
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
            resolved_route_stops = []
            for rs in route_stops:
                loc = None
                loc_id = rs.get("saved_location_id")
                loc = _resolve_loc(request.env, partner, loc_id)
                if not loc:
                    return request.redirect("/my/booking/new")
                loc_eff = _portal_coord_pair(loc) if loc else (None, None)
                entry = {
                    "stop_key": rs.get("stop_key") or "",
                    "location_name": rs.get("location_name")
                        or (loc.business_name or loc.name if loc else ""),
                    "postal_code": (loc.postal_code if loc else rs.get("postal_code")) or "",
                    "latitude": (loc_eff[0] if loc else rs.get("latitude")) or 0.0,
                    "longitude": (loc_eff[1] if loc else rs.get("longitude")) or 0.0,
                    "address": (loc.street if loc else rs.get("address")) or "",
                    "city": (loc.city if loc else rs.get("city")) or "",
                    "pallets": int(rs.get("pallets") or 0),
                    "weight_lbs": float(rs.get("weight_lbs") or 0.0),
                    **(_stop_loc_refs(loc) if loc else {}),
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
                # Route planning must consume the same canonical, verified
                # stop data as the pickup/delivery lists. The browser payload
                # carries stable keys and saved-location references; passing
                # that raw list downstream drops coordinates and yields
                # NO_PICKUP_REGION for access-row locations.
                resolved_route_stops.append({
                    "stop_key": entry["stop_key"],
                    "stop_type": rs.get("stop_type"),
                    "saved_location_id": loc.id,
                    "latitude": entry["latitude"],
                    "longitude": entry["longitude"],
                    "postal_code": entry["postal_code"],
                    "address": entry["address"],
                    "city": entry["city"],
                    **_stop_loc_refs(loc),
                })
                if rs.get("stop_type") == "pickup":
                    gen_pickup_stops.append(entry)
                else:
                    gen_delivery_stops.append(entry)
            pickup_stops = gen_pickup_stops
            delivery_stops = gen_delivery_stops
            route_stops = resolved_route_stops

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
            return request.render(
                "prema_logistics_booking.portal_step2_shipment",
                _quote_error_context(kwargs, partner, pickup_fsa, delivery_fsa, str(exc)),
            )
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

        # Fetch saved locations for display — canonical access row first,
        # legacy saved location fallback (SAVED LOCATION CONSOLIDATION).
        pickup_loc = None
        if session.pickup_customer_access_id:
            acc = request.env["logistics.location.customer.access"].sudo().browse(
                session.pickup_customer_access_id.id)
            if acc.exists():
                pickup_loc = acc
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
        """Segment-aware maximum selectable pallets for the route +
        selected departure (VehicleCapacityService.route_capacity).

        Consumed by the Total Physical Pallets stepper: the + button stops
        at max_selectable_pallets and manual entry above it is clamped
        with a generic message — the number itself is never displayed.
        FTL availability (whole-truck exclusivity) is reflected the same
        way. Route endpoints + shipment type must be sent so occupancy is
        evaluated on the exact segments the shipment overlaps."""
        require_visible()
        from ..services.region_resolver import RegionResolver
        from ..services.vehicle_capacity_service import VehicleCapacityService

        resolver = RegionResolver(request.env)
        # Generalized routes use the first ordered canonical pickup as the
        # availability origin and the last ordered canonical delivery as the
        # route end. The complete stop list is still sent by the portal and
        # is resolved here before any legacy scalar fallback is considered.
        generalized_stops = []
        raw_route_stops = str(kwargs.get("route_stops_json") or "").strip()
        if raw_route_stops:
            try:
                submitted_stops = json.loads(raw_route_stops)
            except (json.JSONDecodeError, TypeError):
                submitted_stops = []
            if isinstance(submitted_stops, list):
                partner = request.env.user.partner_id.commercial_partner_id
                for submitted_stop in submitted_stops:
                    if not isinstance(submitted_stop, dict):
                        generalized_stops = []
                        break
                    loc = _resolve_loc(
                        request.env, partner,
                        submitted_stop.get("saved_location_id"))
                    if not loc:
                        generalized_stops = []
                        break
                    eff = _portal_coord_pair(loc)
                    if eff[0] is None or eff[1] is None:
                        generalized_stops = []
                        break
                    generalized_stops.append({
                        "stop_type": submitted_stop.get("stop_type"),
                        "latitude": eff[0], "longitude": eff[1],
                        "postal_code": loc.postal_code or "",
                    })

        def canonical_stop_region(stop):
            if not stop:
                return False
            try:
                match = resolver.resolve(
                    float(stop["latitude"]), float(stop["longitude"]))
                if match.matched_region:
                    return match.matched_region
            except (KeyError, TypeError, ValueError):
                pass
            return resolver.canonical_region(stop.get("postal_code", ""))

        region = False
        if generalized_stops:
            first_pickup = next(
                (stop for stop in generalized_stops
                 if stop.get("stop_type") == "pickup"), None)
            region = canonical_stop_region(first_pickup)
        if not raw_route_stops and kwargs.get("pickup_lat") and kwargs.get("pickup_lng"):
            try:
                match = resolver.resolve(
                    float(kwargs["pickup_lat"]), float(kwargs["pickup_lng"]))
                region = match.matched_region
            except (ValueError, TypeError):
                region = False
        if not raw_route_stops and not region and kwargs.get("pickup_fsa"):
            # Coordinates outside every polygon (Mascouche J7K …): bridge
            # the FSA through the canonical mapping — never the raw
            # logistics.fsa.region_id (those rows still carry OLD region
            # ids, which no corridor stop references).
            region = resolver.canonical_region(str(kwargs["pickup_fsa"]).strip().upper())
        if not raw_route_stops and not region and kwargs.get("pickup_loc_id"):
            raw = str(kwargs["pickup_loc_id"]).strip()
            if raw.lstrip("-").isdigit():
                partner = request.env.user.partner_id.commercial_partner_id
                loc = _resolve_loc(request.env, partner, int(raw))
                if loc and loc.postal_code:
                    region = resolver.canonical_region(loc.postal_code)

        # The calendar binds the stepper to the EXACT departure the
        # customer selected — never an independently re-searched one.
        # Server-validated: active, still scheduled, vehicle assigned.
        #
        # DISCLOSURE POLICY: the response carries ONLY generic state
        # (available / capacity_state / can_fit_requested_quantity /
        # max_selectable_pallets) — never reserved_pallets, remaining
        # pallets, layouts, or any occupancy breakdown. max_selectable
        # exists ONLY to clamp the stepper (the + button stops at it and
        # manual entry above it is rejected with a generic message) — it
        # is never rendered as text anywhere in the portal. Exact fleet
        # utilization stays private; the authoritative capacity
        # enforcement stays server-side (Get Price pre-check +
        # confirmation lock).
        requested_departure_id = kwargs.get("departure_id")
        requested_pallets = None
        raw_pallets = kwargs.get("pallets")
        if raw_pallets not in (None, ""):
            try:
                requested_pallets = int(raw_pallets)
            except (ValueError, TypeError):
                requested_pallets = None
        shipment_type = kwargs.get("shipment_type") or "ltl"
        if shipment_type not in ("ltl", "ftl"):
            shipment_type = "ltl"

        # Delivery region — segment-aware occupancy needs BOTH route ends.
        # Same canonical resolution chain as the pickup side.
        delivery_region = False
        if generalized_stops:
            deliveries = [stop for stop in generalized_stops
                          if stop.get("stop_type") == "delivery"]
            delivery_region = canonical_stop_region(deliveries[-1] if deliveries else None)
        if not raw_route_stops and kwargs.get("delivery_lat") and kwargs.get("delivery_lng"):
            try:
                delivery_region = resolver.resolve(
                    float(kwargs["delivery_lat"]),
                    float(kwargs["delivery_lng"]),
                ).matched_region
            except (ValueError, TypeError):
                delivery_region = False
        if not raw_route_stops and not delivery_region and kwargs.get("delivery_fsa"):
            delivery_region = resolver.canonical_region(
                str(kwargs["delivery_fsa"]).strip().upper())
        if not raw_route_stops and not delivery_region:
            raw_dl = str(kwargs.get("delivery_loc_id") or "").strip()
            if not raw_dl and kwargs.get("delivery_loc_ids"):
                raw_dl = str(kwargs["delivery_loc_ids"]).split(",")[0].strip()
            if raw_dl.lstrip("-").isdigit():
                partner = request.env.user.partner_id.commercial_partner_id
                dl_loc = _resolve_loc(request.env, partner, int(raw_dl))
                if dl_loc and dl_loc.postal_code:
                    delivery_region = resolver.canonical_region(dl_loc.postal_code)

        Departure = request.env["logistics.corridor.departure"].sudo()
        departure = False
        if requested_departure_id and str(requested_departure_id).lstrip("-").isdigit():
            departure = Departure.browse(int(requested_departure_id)).exists()
        if not (departure and departure.active
                and departure.status not in ("cancelled", "completed")
                and departure.vehicle_id):
            # No (valid) calendar-selected departure: fall back to the
            # scheduled departure serving the pickup region on the
            # requested date — the pre-date-selection probe.
            departure = False
            found = VehicleCapacityService.for_pickup_date(
                request.env, region, kwargs.get("pickup_date"))
            if found.get("available"):
                departure = Departure.browse(found["departure_id"]).exists()

        if departure and departure.vehicle_id:
            result = VehicleCapacityService(request.env).route_capacity(
                departure, region or False, delivery_region or False,
                service_type=shipment_type, requested_pallets=requested_pallets,
            )
            return request.make_json_response({
                "available": result["available"],
                "departure_id": departure.id,
                "capacity_state": result["capacity_state"],
                "max_selectable_pallets": result["max_selectable_pallets"],
                "can_fit_requested_quantity": result["can_fit_requested_quantity"],
                "per_pallet_weight": result["per_pallet_weight"],
            })

        # No service on this date: generic unavailable state.
        return request.make_json_response({
            "available": False,
            "departure_id": False,
            "capacity_state": "unavailable",
            "max_selectable_pallets": 0,
            "can_fit_requested_quantity": False,
            "per_pallet_weight": 0.0,
        })

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

        # Pull address data from the session's frozen canonical locations
        # first, fall back to form fields (for postal-code-only quotes).
        # The access row is preferred, the facility is the physical
        # fallback (SAVED LOCATION CONSOLIDATION 18.0.13.25.0).
        Access = request.env["logistics.location.customer.access"].sudo()
        pu_loc = None
        if session.pickup_customer_access_id:
            acc = Access.browse(session.pickup_customer_access_id.id)
            if acc.exists():
                pu_loc = acc
        de_loc = None
        if session.delivery_customer_access_id:
            acc = Access.browse(session.delivery_customer_access_id.id)
            if acc.exists():
                de_loc = acc
        Facility = request.env["prema.dispatch.location"].sudo()
        pu_fac = None
        if session.pickup_facility_id:
            fac = Facility.browse(session.pickup_facility_id.id)
            if fac.exists():
                pu_fac = fac
        de_fac = None
        if session.delivery_facility_id:
            fac = Facility.browse(session.delivery_facility_id.id)
            if fac.exists():
                de_fac = fac

        # Per-stop contact/instructions (UAT-011) — access rows carry the
        # private contact data.
        delivery_stops_data = []
        for stop in session.delivery_stop_ids:
            seq = stop.sequence
            sl = stop.customer_access_id or stop.saved_location_id
            delivery_stops_data.append({
                "sequence": seq,
                "saved_location_id": sl.id if sl else None,
                "contact_name": kwargs.get(f"delivery_contact_name_{seq}") or (sl.contact_name if sl else ""),
                "phone": kwargs.get(f"delivery_phone_{seq}") or (sl.contact_phone if sl else ""),
                "dock_info": kwargs.get(f"delivery_dock_info_{seq}") or (sl.dock_info if sl else ""),
                "instructions": kwargs.get(f"delivery_instructions_{seq}") or (sl.delivery_instructions if sl else ""),
            })

        address_vals = {
            "pickup_company": (pu_loc.business_name if pu_loc and pu_loc.business_name
                               else (pu_fac.business_name if pu_fac else "")) or kwargs.get("pickup_company"),
            "pickup_postal_code": pu_loc.postal_code if pu_loc else (pu_fac.postal_code if pu_fac else kwargs.get("pickup_postal_code")),
            "pickup_address": pu_loc.street if pu_loc else (pu_fac.street if pu_fac else kwargs.get("pickup_address")),
            "pickup_contact_name": kwargs.get("pickup_contact_name") or (pu_loc.contact_name if pu_loc else ""),
            "pickup_phone": kwargs.get("pickup_phone") or (pu_loc.contact_phone if pu_loc else ""),
            "pickup_instructions": kwargs.get("pickup_instructions") or (pu_loc.pickup_instructions if pu_loc else ""),
            "pickup_dock_info": kwargs.get("pickup_dock_info") or (pu_loc.dock_info if pu_loc else ""),
            "delivery_company": (de_loc.business_name if de_loc and de_loc.business_name
                                 else (de_fac.business_name if de_fac else "")) or kwargs.get("delivery_company"),
            "delivery_postal_code": de_loc.postal_code if de_loc else (de_fac.postal_code if de_fac else kwargs.get("delivery_postal_code")),
            "delivery_address": de_loc.street if de_loc else (de_fac.street if de_fac else kwargs.get("delivery_address")),
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

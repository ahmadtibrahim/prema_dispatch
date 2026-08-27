"""Portal-only bridge for generalized multi-stop booking orchestration.

The portal controller validates customer-owned ``logistics.location.customer.access``
rows before building a movement_v1 request. Downstream orchestration is an
internal service, however, and some canonical-facility reads historically ran
under the portal user's ACLs. That caused a valid customer quote to fail with
an access error on ``prema.dispatch.location``.

This adapter keeps the security boundary explicit:

* customer ownership/capability is re-validated here with sudo;
* portal route-stop references are normalized to the canonical
  customer-access + physical-facility pair;
* only the already-validated internal orchestration portion is executed with
  a sudo environment;
* non-portal channels are untouched;
* route stops are never duplicated. Two pallets from different pickup stops
  may therefore share one delivery stop (for example PU1 -> DL1 and PU2 -> DL1).

It is deliberately additive so production portal/template changes can evolve
independently without replacing the large controller/orchestration files.
"""

from odoo import _
from odoo.exceptions import AccessError, UserError

from .booking_orchestration_service import BookingOrchestrationService


_ORIGINAL_NORMALIZE_REQUEST = BookingOrchestrationService.normalize_request
_ORIGINAL_PREPARE_QUOTE = BookingOrchestrationService.prepare_quote
_ORIGINAL_CONFIRM_FROM_INTERNAL = BookingOrchestrationService.confirm_from_internal


def _sudo_env(env):
    """Return the same Odoo environment in superuser mode."""
    try:
        return env(su=True)
    except TypeError:
        # Compatibility with test/dummy environments that do not implement
        # Environment.__call__. Real Odoo environments take the first path.
        return env


def _int_id(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _commercial_partner(env, partner_id):
    partner = env["res.partner"].sudo().browse(_int_id(partner_id)).exists()
    if not partner:
        raise UserError(_("Customer not found."))
    return partner.commercial_partner_id


def _resolve_customer_access(env, commercial_partner, stop):
    """Resolve one portal stop to this customer's access row + facility.

    ``saved_location_id`` was used by early movement_v1 portal payloads for
    the CUSTOMER ACCESS id, while internal routing uses that legacy key as a
    PHYSICAL FACILITY id. Resolve that ambiguity only at this trusted portal
    boundary, then emit both explicit canonical ids so downstream code never
    has to guess.
    """
    Access = env["logistics.location.customer.access"].sudo().with_context(
        active_test=False,
    )
    Facility = env["prema.dispatch.location"].sudo().with_context(
        active_test=False,
    )

    access = Access.browse()
    facility = Facility.browse()

    access_id = _int_id(stop.get("customer_access_id"))
    facility_id = _int_id(stop.get("facility_id"))
    legacy_id = _int_id(stop.get("saved_location_id"))

    # Explicit access id is authoritative when present.
    if access_id:
        candidate = Access.browse(access_id).exists()
        if (candidate and candidate.active
                and candidate.commercial_partner_id.id == commercial_partner.id):
            access = candidate
        else:
            raise AccessError(_("This saved location does not belong to your account."))

    # Early quote-time portal payloads stored the CUSTOMER ACCESS id in
    # saved_location_id. If an explicit facility_id is also present and is
    # different, validate that both references identify the same facility.
    # At confirm time the compatibility pair is facility_id ==
    # saved_location_id; in that case saved_location_id is intentionally a
    # physical facility alias and must not be reinterpreted as an access id.
    if not access and legacy_id and (not facility_id or legacy_id != facility_id):
        candidate = Access.browse(legacy_id).exists()
        if (candidate and candidate.active
                and candidate.commercial_partner_id.id == commercial_partner.id):
            access = candidate

    if access and access.facility_id:
        facility = access.facility_id.sudo().exists()

    # Explicit facility id (or canonical legacy facility id at confirm time)
    # is accepted only when THIS customer has an active access row for it.
    wanted_facility_id = facility_id or (legacy_id if not access else 0)
    if wanted_facility_id:
        candidate_facility = Facility.browse(wanted_facility_id).exists()
        if not candidate_facility:
            raise UserError(_("A selected saved location is no longer available."))
        if facility and facility.id != candidate_facility.id:
            raise UserError(_("A route stop contains conflicting saved-location references."))
        facility = candidate_facility
        if not access:
            access = Access.search([
                ("facility_id", "=", facility.id),
                ("commercial_partner_id", "=", commercial_partner.id),
                ("active", "=", True),
            ], limit=1)

    if not access or not access.active or not facility:
        raise AccessError(_("Every route stop must use one of your saved locations."))

    stop_type = stop.get("stop_type")
    if stop_type == "pickup" and not access.can_pickup:
        raise UserError(_("A selected location is not enabled for pickup."))
    if stop_type == "delivery" and not access.can_delivery:
        raise UserError(_("A selected location is not enabled for delivery."))

    return access, facility


def _movement_counts(movements):
    """Return physical-pallet counts and weights per stable stop key.

    A movement contributes once to its pickup and once to each delivery it
    serves. The delivery key itself is never duplicated, so two origins can
    feed one Healthy Planet/Belleville stop while it remains a single stop.
    """
    pickup_counts = {}
    pickup_weights = {}
    delivery_counts = {}
    delivery_weights = {}

    for movement in movements or []:
        if not isinstance(movement, dict):
            continue
        pickup_key = str(movement.get("pickup_stop_key") or "").strip()
        try:
            pallet_weight = float(movement.get("weight_lbs") or 0.0)
        except (TypeError, ValueError):
            pallet_weight = 0.0
        if pickup_key:
            pickup_counts[pickup_key] = pickup_counts.get(pickup_key, 0) + 1
            pickup_weights[pickup_key] = (
                pickup_weights.get(pickup_key, 0.0) + pallet_weight
            )

        destinations = movement.get("delivery_stop_keys") or []
        portions = movement.get("delivery_weights") or []
        for index, delivery_key in enumerate(destinations):
            delivery_key = str(delivery_key or "").strip()
            if not delivery_key:
                continue
            delivery_counts[delivery_key] = delivery_counts.get(delivery_key, 0) + 1
            # Dedicated pallets carry their whole physical weight to the one
            # destination. Shared pallets use an explicit portion when the
            # UI supplied one; otherwise leave the explanatory weight at 0
            # rather than inventing a split here.
            if len(destinations) == 1:
                portion = pallet_weight
            elif index < len(portions) and portions[index] is not None:
                try:
                    portion = float(portions[index] or 0.0)
                except (TypeError, ValueError):
                    portion = 0.0
            else:
                # The portal movement snapshot may omit explicit portions
                # for a shared physical pallet. Keep the canonical stop
                # aggregate reconciled with the visible allocation by using
                # an equal explanatory split in that case.
                portion = pallet_weight / float(len(destinations) or 1)
            delivery_weights[delivery_key] = (
                delivery_weights.get(delivery_key, 0.0) + portion
            )

    return pickup_counts, pickup_weights, delivery_counts, delivery_weights


def _canonicalize_portal_route_stops(env, normalized_request):
    """Normalize movement_v1 route refs and physical snapshots in-place."""
    if (getattr(normalized_request, "source_channel", None) != "portal"
            or getattr(normalized_request, "route_model_version", None) != "movement_v1"):
        return normalized_request

    route_stops = list(getattr(normalized_request, "route_stops", None) or [])
    if not route_stops:
        return normalized_request

    commercial = _commercial_partner(env, normalized_request.partner_id)
    (pickup_counts, pickup_weights,
     delivery_counts, delivery_weights) = _movement_counts(
        getattr(normalized_request, "pallet_movements", None) or [],
    )

    seen_keys = set()
    canonical = []

    for original in route_stops:
        if not isinstance(original, dict):
            raise UserError(_("The route stops are invalid."))
        stop = dict(original)
        stop_key = str(stop.get("stop_key") or "").strip()
        if not stop_key or stop_key in seen_keys:
            raise UserError(_("Each route stop must have a unique stable key."))
        seen_keys.add(stop_key)

        stop_type = stop.get("stop_type")
        if stop_type not in ("pickup", "delivery"):
            raise UserError(_("Route stops must be pickup or delivery stops."))

        access, facility = _resolve_customer_access(
            env, commercial, stop,
        )

        # Emit the unambiguous canonical pair. saved_location_id remains as
        # a compatibility alias for internal consumers that define it as the
        # physical prema.dispatch.location id.
        stop["customer_access_id"] = access.id
        stop["facility_id"] = facility.id
        stop["saved_location_id"] = facility.id

        latitude = stop.get("latitude") or access.latitude or facility.pin_lat or 0.0
        longitude = stop.get("longitude") or access.longitude or facility.pin_lng or 0.0
        stop["latitude"] = float(latitude or 0.0)
        stop["longitude"] = float(longitude or 0.0)
        stop["postal_code"] = (
            stop.get("postal_code") or access.postal_code or facility.postal_code or ""
        )
        stop["address"] = (
            stop.get("address") or access.street or facility.street or ""
        )
        stop["street"] = (
            stop.get("street") or access.street or facility.street or ""
        )
        stop["city"] = stop.get("city") or access.city or facility.city or ""
        stop["timezone"] = stop.get("timezone") or access.timezone or "America/Toronto"
        stop["location_name"] = (
            stop.get("location_name")
            or access.customer_alias
            or facility.business_name
            or facility.name
            or facility.chain_name
            or stop["city"]
            or stop_key
        )

        if stop_type == "pickup":
            stop["pallets"] = pickup_counts.get(stop_key, 0)
            stop["weight_lbs"] = round(pickup_weights.get(stop_key, 0.0), 1)
        else:
            stop["pallets"] = delivery_counts.get(stop_key, 0)
            if delivery_weights.get(stop_key, 0.0):
                stop["weight_lbs"] = round(delivery_weights[stop_key], 1)
        canonical.append(stop)

    # The normalized pickup/delivery lists are used by pricing, FSA anchors,
    # session persistence and confirmation. Rebuild them from the SAME stop
    # objects so there is one route authority and no one-to-one collapse.
    normalized_request.route_stops = canonical
    normalized_request.pickup_stops = [
        stop for stop in canonical if stop["stop_type"] == "pickup"
    ]
    normalized_request.delivery_stops = [
        stop for stop in canonical if stop["stop_type"] == "delivery"
    ]
    return normalized_request


def _portal_normalize_request(self, values, source_channel):
    """Backfill route_stops on movement confirmation before validation.

    Quote-time movement_v1 already sends route_stops. Confirm-time rebuilds
    pickup/delivery stops from the frozen pricing-session stop list; older
    code did not copy that ordered list into ``route_stops`` before creating
    NormalizedBookingRequest, which made a valid movement quote fail its own
    movement_v1 invariant.
    """
    if source_channel == "portal" and isinstance(values, dict):
        values = dict(values)
        if (values.get("route_model_version") == "movement_v1"
                and values.get("pallet_movements")
                and not values.get("route_stops")):
            route_stops = []
            for stop_type, rows in (
                    ("pickup", values.get("pickup_stops") or []),
                    ("delivery", values.get("delivery_stops") or [])):
                for stop in rows:
                    row = dict(stop)
                    row["stop_type"] = stop_type
                    # confirm_from_session's stop snapshots intentionally use
                    # saved_location_id as the PHYSICAL facility id. Mark it
                    # explicitly so an equal-numbered customer-access row can
                    # never be mistaken for the facility during canonicalization.
                    if (row.get("saved_location_id")
                            and not row.get("customer_access_id")
                            and not row.get("facility_id")):
                        row["facility_id"] = row["saved_location_id"]
                    route_stops.append(row)
            values["route_stops"] = route_stops
    return _ORIGINAL_NORMALIZE_REQUEST(self, values, source_channel)


def _portal_prepare_quote(self, normalized_request, *args, **kwargs):
    if getattr(normalized_request, "source_channel", None) != "portal":
        return _ORIGINAL_PREPARE_QUOTE(self, normalized_request, *args, **kwargs)

    sudo_env = _sudo_env(self.env)
    _canonicalize_portal_route_stops(sudo_env, normalized_request)
    # The controller/bridge already established customer ownership. From
    # this point the service performs internal facility-hours, corridor,
    # departure, pricing-session and snapshot work; run that trusted segment
    # with the permissions it requires instead of granting portal users read
    # ACLs on internal dispatch facilities.
    internal_service = BookingOrchestrationService(sudo_env)
    return _ORIGINAL_PREPARE_QUOTE(
        internal_service, normalized_request, *args, **kwargs,
    )


def _portal_confirm_from_internal(self, normalized_request, *args, **kwargs):
    if getattr(normalized_request, "source_channel", None) != "portal":
        return _ORIGINAL_CONFIRM_FROM_INTERNAL(
            self, normalized_request, *args, **kwargs,
        )

    sudo_env = _sudo_env(self.env)
    _canonicalize_portal_route_stops(sudo_env, normalized_request)
    internal_service = BookingOrchestrationService(sudo_env)
    return _ORIGINAL_CONFIRM_FROM_INTERNAL(
        internal_service, normalized_request, *args, **kwargs,
    )


# Portal-only adapter: all other channels continue through the original
# methods unchanged.
BookingOrchestrationService.normalize_request = _portal_normalize_request
BookingOrchestrationService.prepare_quote = _portal_prepare_quote
BookingOrchestrationService.confirm_from_internal = _portal_confirm_from_internal

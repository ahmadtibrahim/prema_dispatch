"""18.0.11.0.0 post-migration — backfill COMPATIBILITY pallet rows.

For non-completed bookings that predate the canonical pallet model:
create one pickup stop (from the booking's pickup fields) when missing,
one logistics.booking.pallet per physical pallet, and delivery-allocation
rows from the legacy `_pallet_allocs` JSON in price_snapshot.

COMPATIBILITY-ONLY: these rows are a derived view for UI/reporting. They
NEVER change the booking's architecture — every historical booking stays
route_model_version='legacy' (schema default) and keeps using the legacy
dispatch bridge. Pallet rows alone are not a bridge selector; only the
explicit route_model_version discriminator is.

Idempotent + guarded: never touches completed/cancelled bookings and
never duplicates existing pallet rows or dispatch items.
"""
import logging

_logger = logging.getLogger(__name__)


def backfill_booking_pallets(env):
    Booking = env["logistics.booking"]
    # Schema-only safety: the discriminator column defaults to legacy for
    # every row the ORM ever created before this field existed. Re-assert
    # it so no stray NULL can ever slip a historical booking into the
    # movement bridge.
    env.cr.execute(
        "UPDATE logistics_booking SET route_model_version = 'legacy' "
        "WHERE route_model_version IS NULL"
    )
    bookings = Booking.search([("state", "not in", ("completed", "cancelled"))])
    for booking in bookings:
        if env["logistics.booking.pallet"].search_count(
                [("booking_id", "=", booking.id)]):
            continue
        physical = booking.physical_pallets or booking.pallets or 0
        if physical <= 0:
            continue
        pickup = booking.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")
        if not pickup:
            pickup = env["logistics.booking.stop"].create({
                "booking_id": booking.id,
                "sequence": 5,
                "stop_type": "pickup",
                "location_name": booking.pickup_address or "Pickup",
                "postal_zip": booking.pickup_fsa_id.fsa if booking.pickup_fsa_id else "",
            })
        # Legacy allocation JSON lives in price_snapshot.
        allocs = []
        snap = booking.price_snapshot or []
        for entry in snap:
            if isinstance(entry, dict) and "_pallet_allocs" in entry:
                allocs = entry["_pallet_allocs"] or []
                break
        deliveries = booking.stop_ids.filtered(
            lambda s: s.stop_type == "delivery").sorted("sequence")
        for index in range(1, physical + 1):
            record = next(
                (a for a in allocs if isinstance(a, dict)
                 and a.get("pallet") == index), None)
            stops = (record or {}).get("stops") or []
            pallet = env["logistics.booking.pallet"].create({
                "booking_id": booking.id,
                "sequence": index * 10,
                "label": "P-%d" % index,
                "weight_lbs": (booking.weight_lbs or 0.0) / physical,
                "shared": bool(stops and len(stops) > 1),
                "pickup_stop_id": pickup.id,
            })
            for stop_index in stops:
                delivery = deliveries[stop_index - 1] if (
                    1 <= stop_index <= len(deliveries)) else deliveries[:1]
                if delivery:
                    env["logistics.booking.pallet.stop.allocation"].create({
                        "pallet_id": pallet.id,
                        "delivery_stop_id": delivery.id,
                        "unload_sequence": 10,
                    })


def migrate(cr, version):
    _logger.info("18.0.11.0.0 post-migration: backfilling booking pallet movements")
    env = cr.env if hasattr(cr, "env") else None
    if env is not None:
        backfill_booking_pallets(env)
    _logger.info("18.0.11.0.0 post-migration complete")

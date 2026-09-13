"""Corridor 13 — GTA → Southeastern Ontario → Ottawa Region as a LOOP.

Corridor 13 is the Friday service on the shared truck (Mon corridor 15,
Tue 9, Wed 11, Thu 12, Fri 13). It was left in a state that could not
express the Friday operation at all:

  • direction 'eastbound' with same_day_return on — the quote/calendar
    gates read the direction, so the Friday return lanes were refused;
  • destination_hub_id empty, so the truck's arrival back at the
    Mississauga hub had no configured position or time (0 km / 00:00,
    source 'suggested'), and the Friday-evening return delivery had
    nowhere to land;
  • a DUPLICATE sequence 10 (the GTA hub region AND Kawarthas), and
    Haliburton sequenced BEFORE Southeastern/Ottawa while carrying the
    largest distance — stop order and the driving order disagreed.

This script rebuilds the corridor as the documented Friday loop:

    GTA hub 0 km → Northumberland → Southeastern Ontario → Ottawa Region
             → Haliburton Highlands/Ottawa Valley → Kawarthas → GTA hub

The return-path regions (Haliburton, Kawarthas) are ordinary ordered
stops of ONE pass, so a loop needs no mirror visits: the hub is both the
start (0 km) and the finish (destination_hub_distance_km), and the model
detects it by origin_hub_id == destination_hub_id.

Timing is never invented here. Every stop is reset to 'suggested' and the
canonical action_recalculate_route_distance() then writes the road
distance and the suggested clocks from Google Routes (the corridor form's
own Calculate Route Distance button) — including the hub return time that
every custody calculation downstream measures from.

Idempotent: re-running rewrites the same stop set and recalculates the
same distances; no stop is ever duplicated, no departure and no existing
booking is touched. Run with an Odoo shell and COMMIT explicitly:

    odoo shell -d <db> < scripts/fix_corridor_13_loop.py

"""
import logging

_logger = logging.getLogger(__name__)

CORRIDOR_ID = 13

# (sequence, region code, label) in ACTUAL driving order around the loop.
# The hub region is the anchor stop at 0 km — the same pattern corridors
# 9/12/15 already use.
ROUTE = [
    (10, "ON-GTA", "GTA hub — loop start and finish"),
    (20, "ON-NORTHUMBERLAND", "outbound"),
    (30, "ON-SOUTHEASTERN", "outbound"),
    (40, "ON-OTTAWA", "outbound"),
    (50, "ON-HALIBURTON-OTTAWA-VALLEY", "return path"),
    (60, "ON-KAWARTHAS", "return path"),
]


def fix_corridor_13_loop(env, commit=False):
    """Rebuild corridor 13 as the Friday loop. Returns a summary dict."""
    corridor = env["logistics.corridor"].browse(CORRIDOR_ID)
    if not corridor.exists():
        raise ValueError("Corridor %s does not exist." % CORRIDOR_ID)

    Region = env["logistics.region"]
    summary = {"stops_created": 0, "stops_updated": 0, "stops_archived": 0}

    origin_hub = corridor.origin_hub_id
    if not origin_hub:
        origin_hub = env["logistics.hub"].search(
            [("is_default", "=", True)], limit=1)
    if not origin_hub:
        raise ValueError("Corridor %s has no origin hub." % CORRIDOR_ID)

    # The loop needs BOTH hub references on the same hub — that pair is
    # what _is_loop_corridor() keys on (single pass, no mirror visits).
    # Writing destination_hub_id also zeroes destination_hub_distance_km
    # by design; the recalculation below rewrites the real loop length.
    corridor.with_context(skip_departure_reconcile=True).write({
        "direction": "round_trip",
        "same_day_return": True,
        "origin_hub_id": origin_hub.id,
        "destination_hub_id": origin_hub.id,
        "destination_hub_distance_km": 0.0,
        "hub_arrival_time_source": "suggested",
        "destination_hub_arrival_time": 0.0,
    })

    wanted_sequences = {sequence for sequence, _code, _label in ROUTE}
    wanted_codes = {code for _sequence, code, _label in ROUTE}

    for sequence, code, label in ROUTE:
        region = Region.search([("code", "=", code)], limit=1)
        if not region:
            raise ValueError("Region %s not found." % code)
        stop = corridor.stop_ids.filtered(
            lambda item: item.region_id == region
        ).sorted("sequence")[:1]
        values = {
            "corridor_id": corridor.id,
            "sequence": sequence,
            "region_id": region.id,
            "pickup_allowed": True,
            "delivery_allowed": True,
            "active": True,
            "name": label,
        }
        if stop:
            # Distances and clocks are rewritten by the recalculation, so
            # the write must not leave a stale 'manual' override behind.
            values["timing_source"] = "suggested"
            stop.write(values)
            summary["stops_updated"] += 1
        else:
            env["logistics.corridor.stop"].create(values)
            summary["stops_created"] += 1

    # Retire anything not on the documented route (the duplicate GTA row,
    # any leftover experimental stop). Never deleted — existing rows may
    # be referenced by history.
    for stop in corridor.stop_ids:
        if stop.region_id.code in wanted_codes \
                and stop.sequence in wanted_sequences:
            continue
        if not stop.active:
            continue
        stop.write({"active": False})
        summary["stops_archived"] += 1

    # Canonical distance + suggested timing writer (the corridor form's own
    # "Calculate Route Distance"): Google Routes when available, the
    # documented straight-line estimate otherwise. This is what fills
    # destination_hub_distance_km (the full loop length) and the hub
    # return clock every custody calculation measures from.
    corridor.action_recalculate_route_distance()

    summary.update({
        "corridor_id": corridor.id,
        "direction": corridor.direction,
        "loop": corridor._is_loop_corridor(),
        "full_distance_km": corridor.full_distance_km,
        "hub_arrival_time": corridor.destination_hub_arrival_time,
        "departures_touched": False,
    })
    if commit:
        env.cr.commit()
    _logger.info("Corridor 13 loop fix: %s", summary)
    return summary

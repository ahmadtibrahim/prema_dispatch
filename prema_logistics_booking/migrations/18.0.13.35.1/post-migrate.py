"""18.0.13.35.1 post-migration — official SWON "Southwestern Ontario" LTL region
+ Monday GTA → Southwestern Ontario → Windsor corridor (Task SWON).

NOTE: 18.0.13.35.0 shipped with `cr.env` in migrate() — that attribute does
NOT exist on this Odoo build's migration cursor, so the body silently
no-oped (the same bug 18.0.13.15.0 shipped; see 18.0.13.15.1). This
migration re-runs the full work with the canonical explicit environment.
All steps below are idempotent, so re-running on any DB state is safe.

Creates the official Southwestern Ontario LTL region from the SAME legitimate
boundary source as every other official region — Statistics Canada 2021 Census
/ Esri Canada Content FeatureServer (Census Divisions Essex 3537,
Chatham-Kent 3536, Lambton 3538, Middlesex 3539, Oxford 3532, Elgin 3534),
dissolved + simplified (same method as all sibling regions), then:

1. Remaps 60 SW-Ontario FSAs — verified by representative-point AND centroid
   point-in-polygon against the authoritative 2021 FSA boundaries (StatsCan
   lfsa000b21a_e) vs both the raw and the simplified SWON polygon (zero
   disagreements) — from their current owners (ON-HHB 29 / ON-WATERLOO-
   WELLINGTON 26 / ON-SOUTHEASTERN 5) to SWON. Every FSA keeps exactly one
   region_id; nothing is ever stolen from a region outside the reported list.
   Full conflict list reported and approved BEFORE this migration was written
   (see /tmp/prema_swon_report.md, section 4).

2. Runs the equivalent of the Apply FSA Coverage UI action on SWON with the
   same 60-code list — must pass conflict-free after the remap (proves the
   mechanism the UI uses accepts the coverage).

3. Creates the NEW Monday-only round-trip corridor
   "GTA -> Southwestern Ontario -> Windsor" (reefer, 06:00, 3.00/km,
   8 pallets, $100 corridor minimum — mirroring the existing Monday/Thursday
   GTA–HHB–Niagara pattern). The existing Monday corridor
   "GTA -> Hamilton, Halton and Brant -> Niagara Region" is NOT overwritten;
   it is only repaired (see 4).

4. Repairs the existing Monday corridor: removes its phantom 4th stop
   (seq 13, region_id NULL — invisible to route resolution, pollutes
   planning) and recalculates both corridors' road distances via the
   canonical action_recalculate_route_distance (Google Routes).

5. Materializes the 8-week departure horizon immediately (the daily cron
   would do it anyway; running here makes Monday 06:00 departures available
   right away).

Idempotent: safe to run once (version-scoped); every step re-checks its
prerequisite before acting.
"""
import json
import logging
import os

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

SWON_FSAS = [
    # Woodstock (Oxford) — from ON-HHB
    "N4S", "N4T", "N4V",
    # Tillsonburg (Oxford) — from ON-HHB
    "N4G",
    # Ingersoll / Dorchester / Thamesford (Oxford-Middlesex) — from ON-HHB
    "N5C",
    # Aylmer (Elgin) — from ON-HHB
    "N5H",
    # Port Stanley (Elgin) — from ON-HHB
    "N5L",
    # St. Thomas (Elgin) — from ON-HHB
    "N5P", "N5R",
    # London (Middlesex) — from ON-HHB
    "N5V", "N5W", "N5X", "N5Y", "N5Z",
    "N6A", "N6B", "N6C", "N6E", "N6G", "N6H", "N6J", "N6K", "N6L", "N6M",
    "N6N", "N6P",
    # Delaware / Lambeth (Middlesex) — from ON-HHB
    "N7G",
    # Chatham-Kent — from ON-HHB
    "N7L", "N7M",
    # Sarnia (Lambton) — from ON-WATERLOO-WELLINGTON
    "N7S", "N7T", "N7V", "N7W", "N7X",
    # Tecumseh / St. Clair Beach (Essex) — from ON-WATERLOO-WELLINGTON
    "N8A",
    # LaSalle (Essex) — from ON-WATERLOO-WELLINGTON
    "N8H",
    # Essex (Essex) — from ON-WATERLOO-WELLINGTON
    "N8M",
    # Oldcastle / Tecumseh (Essex) — from ON-WATERLOO-WELLINGTON
    "N8N",
    # Windsor (Essex) — from ON-WATERLOO-WELLINGTON
    "N8P", "N8R", "N8S", "N8T", "N8W", "N8X", "N8Y",
    "N9A", "N9B", "N9C", "N9E", "N9G", "N9H", "N9J", "N9K", "N9V", "N9Y",
    # Rural SW Ontario (FSA polygons verified to extend into SWON territory)
    # — from ON-SOUTHEASTERN
    "N0J",  # South Norwich / Delhi — point verified inside Oxford CD
    "N0L",  # Norwich / Sweaburg / Shedden — point verified inside Elgin CD
    "N0N",  # Dutton / West Lorne / Wallacetown (Elgin)
    "N0P",  # Blenheim / Wheatley / Tilbury (Chatham-Kent / Essex)
    "N0R",  # Tilbury / Comber — point verified inside Chatham-Kent CD
]

# Regions owning SWON-area FSAs before this migration (the reported conflict
# list). The migration NEVER moves an FSA out of any other region.
EXPECTED_SOURCE_REGIONS = ("ON-HHB", "ON-WATERLOO-WELLINGTON", "ON-SOUTHEASTERN")

BOUNDARY_SOURCE = "Statistics Canada 2021 Census — Esri Canada Content FeatureServer"
BOUNDARY_SOURCE_URL = (
    "https://services.arcgis.com/wjcPoefzjpzCgffS/arcgis/rest/services/"
    "Census_Division/FeatureServer"
)
BOUNDARY_VERSION_DATE = "2021-01-01"

MONDAY_CORRIDOR_NAME = "GTA -> Southwestern Ontario -> Windsor"
EXISTING_MONDAY_NAME = "GTA -> Hamilton, Halton and Brant -> Niagara Region"


def _load_polygon():
    module_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(module_dir, "data", "boundaries", "sw_ontario_2021.geojson")
    with open(path) as fh:
        return json.load(fh)


def _region(env, code):
    return env["logistics.region"].sudo().with_context(active_test=False).search(
        [("code", "=", code)], limit=1)


def _create_swon_region(env):
    """Create (or reuse) the official SWON region. Returns the record."""
    region = _region(env, "SWON")
    if region:
        _logger.info("SWON region already exists: id=%s", region.id)
        return region
    from odoo import fields as odoo_fields
    canada = env["res.country"].sudo().search([("code", "=", "CA")], limit=1)
    ontario = env["res.country.state"].sudo().search(
        [("code", "=", "ON"), ("country_id", "=", canada.id)], limit=1)
    reviewer = env.ref("base.user_admin", raise_if_not_found=False) or env.user
    region = env["logistics.region"].sudo().create({
        "code": "SWON",
        "name": "Southwestern Ontario",
        "main_city": "London",
        "country_id": canada.id,
        "state_id": ontario.id,
        "customer_visible": True,
        "is_official_ltl_region": True,
        "active": True,
        "display_sequence": 20,
        "boundary_status": "approved",
        "boundary_source": BOUNDARY_SOURCE,
        "boundary_source_url": BOUNDARY_SOURCE_URL,
        "boundary_version_date": BOUNDARY_VERSION_DATE,
        "boundary_reviewed_by": reviewer.id,
        "boundary_reviewed_at": odoo_fields.Datetime.now(),
        "match_priority": 10,
        "marker_latitude": 42.9849,
        "marker_longitude": -81.2453,
        "map_color": "#00838f",
    })
    region.write({"polygon_geojson": json.dumps(_load_polygon())})
    # Canonical boundary validation + area + checksum (same as the UI action).
    from odoo.addons.prema_logistics_booking.services.region_resolver import RegionResolver
    resolver = RegionResolver(env)
    is_valid, message, _repaired = resolver.validate_geometry(region.polygon_geojson)
    if not is_valid:
        raise RuntimeError("SWON polygon failed validation: %s" % message)
    region.write({
        "boundary_area_km2": resolver.compute_area_km2(region.polygon_geojson),
        "boundary_checksum": resolver.compute_checksum(region.polygon_geojson),
    })
    resolver.invalidate_cache(region)
    _logger.info("SWON region created: id=%s", region.id)
    return region


def _remap_swon_fsas(env, region):
    """Reassign the 60 verified SW-Ontario FSAs to SWON.

    Guard: only FSAs currently owned by the three reported source regions
    (or unassigned) are moved — never stolen from any other region; those
    are logged and skipped."""
    Fsa = env["logistics.fsa"].sudo()
    moved, skipped_other, already = [], [], 0
    for fsa_code in SWON_FSAS:
        fsa = Fsa.search([("fsa", "=", fsa_code)], limit=1)
        if not fsa:
            _logger.warning("FSA %s not found in DB — creating", fsa_code)
            fsa = Fsa.create({
                "fsa": fsa_code,
                "province": region.state_id.code,
                "display_city": region.main_city,
                "region_id": region.id,
                "pickup_supported": True,
                "delivery_supported": True,
                "active": True,
            })
            moved.append((fsa_code, "MISSING"))
            continue
        if fsa.region_id == region:
            already += 1
            continue
        if fsa.region_id and fsa.region_id.code not in EXPECTED_SOURCE_REGIONS:
            skipped_other.append(f"{fsa_code} ({fsa.region_id.code})")
            continue
        moved.append((fsa_code, fsa.region_id.code if fsa.region_id else "UNASSIGNED"))
        fsa.region_id = region.id
    if skipped_other:
        _logger.warning("FSAs skipped (owned by unreported region): %s", skipped_other)
    _logger.info("SWON FSA remap: moved=%d already=%d skipped_other=%s",
                 len(moved), already, skipped_other)
    return moved


def _apply_fsa_coverage_equivalent(env, region):
    """Run the UI action's own validation against the SWON coverage list.

    After the remap every code in SWON_FSAS belongs to SWON, so the action's
    conflict guard must pass and it records the coverage list on the region —
    the same end state a Dispatch Manager's Apply FSA Coverage click produces."""
    region.sudo().coverage_fsa_prefixes = "\n".join(SWON_FSAS)
    try:
        region.sudo().action_apply_fsa_coverage()
    except Exception as exc:  # noqa: BLE001 — surface so the migration fails loudly
        raise RuntimeError("Apply FSA Coverage equivalent FAILED for SWON: %s" % exc)
    _logger.info("Apply FSA Coverage equivalent passed for SWON (%d codes)",
                 len(SWON_FSAS))


def _create_monday_corridor(env, region):
    """Create the new Monday-only round-trip corridor GTA -> SWON -> Windsor."""
    Corridor = env["logistics.corridor"].sudo()
    existing = Corridor.search([("name", "=", MONDAY_CORRIDOR_NAME)], limit=1)
    if existing:
        _logger.info("Monday SWON corridor already exists: id=%s", existing.id)
        return existing
    hub = env["logistics.hub"].sudo().search([("is_default", "=", True)], limit=1)
    if not hub:
        hub = env["logistics.hub"].sudo().search([("name", "=", "Mississauga Hub")], limit=1)
    vehicle = env["fleet.vehicle"].sudo().browse(15)
    if not vehicle.exists():
        vehicle = env["fleet.vehicle"].sudo().search(
            [("name", "ilike", "PB38446")], limit=1)
    gta = _region(env, "ON-GTA")
    corridor = Corridor.create({
        "name": MONDAY_CORRIDOR_NAME,
        "direction": "round_trip",
        "equipment_type": "reefer",
        "same_day_return": True,
        "operate_monday": True,
        "start_time": 6.0,
        "rate_per_km": 3.0,
        "planned_pallets": 8,
        "minimum_booking_charge": 100.0,
        "included_weight_per_pallet": 500.0,
        "origin_hub_id": hub.id if hub else False,
        "default_vehicle_id": vehicle.id if vehicle.exists() else False,
        "departure_horizon_weeks": 8,
        "active": True,
    })
    stop_model = env["logistics.corridor.stop"].sudo()
    stop_model.create([
        {"corridor_id": corridor.id, "region_id": gta.id, "sequence": 10,
         "pickup_allowed": True, "delivery_allowed": True, "active": True},
        {"corridor_id": corridor.id, "region_id": region.id, "sequence": 20,
         "pickup_allowed": True, "delivery_allowed": True, "active": True},
    ])
    _logger.info("Monday SWON corridor created: id=%s", corridor.id)
    return corridor


def _repair_existing_monday(env):
    """Remove the phantom 4th stop (region_id NULL) from the existing Monday
    corridor and recalculate both corridors' Google route distances."""
    Corridor = env["logistics.corridor"].sudo()
    existing = Corridor.search(
        [("name", "=", EXISTING_MONDAY_NAME), ("operate_monday", "=", True)],
        limit=1)
    repaired = False
    if existing:
        phantom = existing.stop_ids.filtered(
            lambda s: not s.region_id and s.active)
        if phantom:
            _logger.info("Removing phantom stop(s) from corridor %s: ids=%s",
                         existing.id, phantom.ids)
            phantom.unlink()
            repaired = True
        else:
            _logger.info("Corridor %s has no phantom stop to repair", existing.id)
    results = {}
    for corridor in (existing, Corridor.search(
            [("name", "=", MONDAY_CORRIDOR_NAME)], limit=1)):
        if not corridor:
            continue
        try:
            corridor.action_recalculate_route_distance()
            results[corridor.id] = "ok"
        except Exception as exc:  # noqa: BLE001 — migration must not die on API hiccups
            _logger.warning("Corridor %s distance recalculation failed: %s",
                            corridor.id, exc)
            results[corridor.id] = "failed: %s" % exc
    _logger.info("Corridor distance recalc results: %s", results)
    return repaired, results


def _materialize_departures(env):
    from odoo.addons.prema_logistics_booking.scripts.generate_phase1_departures import (
        generate_phase1_departures,
    )
    result = generate_phase1_departures(env, weeks=8)
    _logger.info("Departure horizon after migration: %s", result)
    return result


def migrate(cr, version):
    # NOTE: `cr.env` does NOT exist on this Odoo build's migration cursor
    # (silently no-ops — 18.0.13.35.0 shipped that bug and was re-run as
    # 18.0.13.35.1, exactly like 18.0.13.15.0 -> 18.0.13.15.1). The
    # canonical pattern is an explicit environment.
    _logger.info("18.0.13.35.1 post-migration: SWON region + Monday corridor")
    env = api.Environment(cr, SUPERUSER_ID, {})
    region = _create_swon_region(env)
    moved = _remap_swon_fsas(env, region)
    _apply_fsa_coverage_equivalent(env, region)
    corridor = _create_monday_corridor(env, region)
    repaired, recalc = _repair_existing_monday(env)
    departures = _materialize_departures(env)
    _logger.info(
        "18.0.13.35.1 post-migration complete: region=%s fsas_moved=%s "
        "corridor=%s repaired_phantom=%s recalc=%s departures=%s",
        region.id, moved, corridor.id, repaired, recalc, departures)
    _logger.info("18.0.13.35.1 post-migration done")

"""18.0.13.15.0 post-migration — official QC-MONTREAL region + corridor wiring (Task N).

Creates the official Greater-Montreal (island) LTL region from the SAME
legitimate boundary source as every other official QC region — Statistics
Canada 2021 Census / Esri Canada Content FeatureServer (Census Division CD
2466, Montréal) — then:

1. Remaps the Montreal-island FSAs (physically inside the CD 2466 polygon,
   verified by centroid point-in-polygon against the authoritative 2021 FSA
   boundaries; the 98-code list below is that verified set) from the
   historical inactive region 18 (R13 Greater Montreal) to QC-MONTREAL.
   Laval (H7), the south shore (J*), and every other FSA whose centroid is
   outside the island polygon stay untouched on region 18 (historical).

2. Inserts QC-MONTREAL into corridor 9 (eastbound, GTA → Québec City) right
   after QC-OUTAOUAIS — the truck physically crosses Montreal Island on the
   417/40 corridor before reaching Laurentides/Montérégie/Lanaudière — and
   its exact reverse counterpart into corridor 11 (westbound) so the
   is_two_way pairing stays exact.

3. Recalculates the cumulative road distances via the canonical
   action_recalculate_route_distance mechanism (Google Routes).

Idempotent: safe to run once (version-scoped). Historical region 18 is left
inactive, never deleted. No FSA is ever owned by two official regions —
each logistics.fsa row keeps exactly one region_id.
"""
import json
import logging
import os

_logger = logging.getLogger(__name__)

# FSAs with authoritative 2021-FSA centroids inside the Montréal CD (2466)
# boundary — extracted from the Esri Canada Content FeatureServer with the
# same source used for every official QC region polygon.
MONTREAL_ISLAND_FSAS = [
    "H1A", "H1B", "H1C", "H1E", "H1G", "H1H", "H1J", "H1K", "H1L", "H1M",
    "H1N", "H1P", "H1R", "H1S", "H1T", "H1V", "H1W", "H1X", "H1Y", "H1Z",
    "H2A", "H2B", "H2C", "H2E", "H2G", "H2H", "H2J", "H2K", "H2L", "H2M",
    "H2N", "H2P", "H2R", "H2S", "H2T", "H2V", "H2W", "H2X", "H2Y", "H2Z",
    "H3A", "H3B", "H3C", "H3E", "H3G", "H3H", "H3J", "H3K", "H3L", "H3M",
    "H3N", "H3P", "H3R", "H3S", "H3T", "H3V", "H3W", "H3X", "H3Y", "H3Z",
    "H4A", "H4B", "H4C", "H4E", "H4G", "H4H", "H4J", "H4K", "H4L", "H4M",
    "H4N", "H4P", "H4R", "H4S", "H4T", "H4V", "H4W", "H4X",
    "H8N", "H8P", "H8R", "H8S", "H8T", "H8Y", "H8Z",
    "H9A", "H9B", "H9C", "H9E", "H9G", "H9H", "H9J", "H9K", "H9P", "H9R",
    "H9S", "H9W", "H9X",
]

BOUNDARY_SOURCE = "Statistics Canada 2021 Census — Esri Canada Content FeatureServer"
BOUNDARY_SOURCE_URL = (
    "https://services.arcgis.com/wjcPoefzjpzCgffS/arcgis/rest/services/"
    "Census_Division/FeatureServer"
)
BOUNDARY_VERSION_DATE = "2021-01-01"


def _load_polygon():
    module_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(module_dir, "data", "boundaries", "qc_montreal_2021.geojson")
    with open(path) as fh:
        return json.load(fh)


def _region(env, code):
    # active_test=False: historical region 18 (R13) is intentionally
    # INACTIVE — the remap must still find it to compare/transfer FSAs.
    return env["logistics.region"].sudo().with_context(active_test=False).search(
        [("code", "=", code)], limit=1)


def _create_montreal_region(env):
    """Create (or reuse) the official QC-MONTREAL region. Returns the record."""
    region = _region(env, "QC-MONTREAL")
    if region:
        return region
    from odoo import fields as odoo_fields
    canada = env["res.country"].sudo().search([("code", "=", "CA")], limit=1)
    quebec = env["res.country.state"].sudo().search(
        [("code", "=", "QC"), ("country_id", "=", canada.id)], limit=1)
    reviewer = env.ref("base.user_admin", raise_if_not_found=False) or env.user
    region = env["logistics.region"].sudo().create({
        "code": "QC-MONTREAL",
        "name": "Montréal",
        "main_city": "Montréal",
        "country_id": canada.id,
        "state_id": quebec.id,
        "customer_visible": True,
        "is_official_ltl_region": True,
        "active": True,
        "boundary_status": "approved",
        "boundary_source": BOUNDARY_SOURCE,
        "boundary_source_url": BOUNDARY_SOURCE_URL,
        "boundary_version_date": BOUNDARY_VERSION_DATE,
        "boundary_reviewed_by": reviewer.id,
        "boundary_reviewed_at": odoo_fields.Datetime.now(),
        "match_priority": 10,
        "marker_latitude": 45.5017,
        "marker_longitude": -73.5673,
        "map_color": "#7b1fa2",
    })
    region.write({"polygon_geojson": json.dumps(_load_polygon())})
    # Canonical boundary validation + area + checksum (same as the UI action).
    from odoo.addons.prema_logistics_booking.services.region_resolver import RegionResolver
    resolver = RegionResolver(env)
    is_valid, message, _repaired = resolver.validate_geometry(region.polygon_geojson)
    if not is_valid:
        raise RuntimeError("QC-MONTREAL polygon failed validation: %s" % message)
    region.write({
        "boundary_area_km2": resolver.compute_area_km2(region.polygon_geojson),
        "boundary_checksum": resolver.compute_checksum(region.polygon_geojson),
    })
    resolver.invalidate_cache(region)
    _logger.info("QC-MONTREAL region created: id=%s", region.id)
    return region


def _remap_island_fsas(env, region):
    """Reassign the verified island FSAs from historical region 18 to QC-MONTREAL."""
    Fsa = env["logistics.fsa"].sudo()
    legacy = _region(env, "R13")
    moved = 0
    skipped_other = []
    for fsa in Fsa.search([("fsa", "in", MONTREAL_ISLAND_FSAS)]):
        if fsa.region_id == region:
            continue  # already assigned
        if fsa.region_id and fsa.region_id != legacy:
            skipped_other.append(fsa.fsa)  # never steal from another region
            continue
        fsa.region_id = region.id
        moved += 1
    if skipped_other:
        _logger.warning("FSAs skipped (owned by other region): %s", skipped_other)
    _logger.info("Montreal-island FSAs remapped: %d (region %s -> QC-MONTREAL)",
                 moved, legacy.code if legacy else "?")
    return moved


def _insert_corridor_stops(env, region):
    """Insert QC-MONTREAL into corridors 9 and 11 as mirror counterparts."""
    c9 = env["logistics.corridor"].sudo().browse(9)
    c11 = env["logistics.corridor"].sudo().browse(11)
    if not c9.exists() or not c11.exists():
        raise RuntimeError("Corridor 9/11 not found")
    stop_model = env["logistics.corridor.stop"].sudo()
    for corridor in (c9, c11):
        ordered = corridor.stop_ids.filtered("active").sorted("sequence")
        if not ordered:
            continue
        # Anchor on the OUTAOUAIS stop. Eastbound (9) the truck crosses the
        # island on the 417/40 right after Gatineau → insert AFTER
        # QC-OUTAOUAIS. Westbound (11) is the exact reverse counterpart →
        # insert immediately BEFORE QC-OUTAOUAIS (after whatever precedes
        # it), keeping the two corridors perfect mirror images so the
        # is_two_way pairing check stays satisfied.
        idx = None
        for i, stop in enumerate(ordered):
            if stop.region_id and stop.region_id.code == "QC-OUTAOUAIS":
                idx = i
                break
        if idx is None:
            raise RuntimeError("Corridor %s has no QC-OUTAOUAIS stop" % corridor.id)
        already = ordered.filtered(lambda s: s.region_id == region)
        if already:
            continue  # idempotent
        insert_at = idx + 1 if corridor.id == 9 else idx
        new_order = list(ordered)
        new_order.insert(insert_at, None)  # placeholder for the new stop
        # Renumber everything cleanly in 10s, then create the new stop.
        seq = 10
        created = False
        for stop in new_order:
            if stop is None:
                stop_model.create({
                    "corridor_id": corridor.id,
                    "region_id": region.id,
                    "sequence": seq,
                    "pickup_allowed": True,
                    "delivery_allowed": True,
                    "active": True,
                })
                created = True
            else:
                stop.sequence = seq
            seq += 10
        _logger.info("Corridor %s (%s): inserted QC-MONTREAL at sequence %s",
                     corridor.id, corridor.name, seq - 20 if created else "-")
    return True


def _recalc_distances(env):
    """Canonical route-distance recalculation (Google Routes) for 9 and 11."""
    results = {}
    for cid in (9, 11):
        corridor = env["logistics.corridor"].sudo().browse(cid)
        try:
            corridor.action_recalculate_route_distance()
            results[cid] = "ok"
        except Exception as exc:  # noqa: BLE001 — migration must not die on API hiccups
            _logger.warning("Corridor %s distance recalculation failed: %s", cid, exc)
            results[cid] = "failed: %s" % exc
    return results


def migrate(cr, version):
    _logger.info("18.0.13.15.0 post-migration: QC-MONTREAL region + corridors")
    env = cr.env if hasattr(cr, "env") else None
    if env is not None:
        region = _create_montreal_region(env)
        moved = _remap_island_fsas(env, region)
        _insert_corridor_stops(env, region)
        recalc = _recalc_distances(env)
        _logger.info("18.0.13.15.0 post-migration complete: region=%s fsas_moved=%s recalc=%s",
                     region.id, moved, recalc)
    _logger.info("18.0.13.15.0 post-migration done")

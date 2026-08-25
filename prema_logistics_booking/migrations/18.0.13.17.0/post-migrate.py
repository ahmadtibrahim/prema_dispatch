"""18.0.13.17.0 post-migration — corridor timing authority (Phase 2).

Every existing non-zero planned stop time and Hub Arrival Time was entered by
hand; flag them Manual Override so the new Calculate Route Times / Calculate
Route Distance actions never silently overwrite them. Zero/empty rows stay
Suggested and are filled on the next recalculation.

Idempotent: version-scoped, runs once.
"""
from odoo.tools.sql import column_exists


def migrate(cr, version):
    if column_exists(cr, "logistics_corridor_stop", "timing_source"):
        cr.execute(
            "UPDATE logistics_corridor_stop SET timing_source = 'manual' "
            "WHERE planned_arrival_time > 0 OR planned_departure_time > 0"
        )
    if column_exists(cr, "logistics_corridor", "hub_arrival_time_source"):
        cr.execute(
            "UPDATE logistics_corridor SET hub_arrival_time_source = 'manual' "
            "WHERE destination_hub_arrival_time > 0"
        )

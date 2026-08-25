"""18.0.13.18.0 post-migration — Phase 3: departure truck/driver override.

vehicle_assignment_source value renamed manual_override → departure_override;
remap historical rows so the restore-to-corridor-default reconcile logic
keeps working. Idempotent: version-scoped, runs once.
"""
from odoo.tools.sql import column_exists


def migrate(cr, version):
    if column_exists(cr, "logistics_corridor_departure", "vehicle_assignment_source"):
        cr.execute(
            "UPDATE logistics_corridor_departure "
            "SET vehicle_assignment_source = 'departure_override' "
            "WHERE vehicle_assignment_source = 'manual_override'"
        )

"""MANUAL UAT FOLLOW-UP (18.0.13.34.0) — departure driver consistency.

DEPARTURE DRIVER (ISSUE 3): departures created by the corridor horizon
generator carried a truck but no driver_id (the Phase-3 driver resolution
only ran on truck REassignment). Backfill driver_id on every departure
that still has no driver but whose assigned truck has a configured/default
driver — the same resolution used at create() time now and by the
truck-reassignment write hook: vehicle.driver_id or
vehicle.x_current_driver_contact_id.

Only fills blanks; never overrides an existing driver assignment.
Idempotent by construction.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Departure = env["logistics.corridor.departure"]
    Vehicle = env["fleet.vehicle"]

    driverless = Departure.search(
        [("driver_id", "=", False), ("vehicle_id", "!=", False)]
    )
    vehicles = Vehicle.browse(driverless.mapped("vehicle_id").ids)
    vehicle_driver = {}
    for vehicle in vehicles:
        vehicle_driver[vehicle.id] = (
            vehicle.driver_id or vehicle.x_current_driver_contact_id
        )

    fixed = []
    for departure in driverless:
        driver = vehicle_driver.get(departure.vehicle_id.id)
        if driver:
            departure.write({"driver_id": driver.id})
            fixed.append((departure.id, departure.vehicle_id.name, driver.name))

    _logger.info(
        "18.0.13.34.0: departure driver backfill — %d departure(s) "
        "received their truck's default driver: %s",
        len(fixed),
        ", ".join("dep %s / %s → %s" % f for f in fixed[:20]) or "none",
    )

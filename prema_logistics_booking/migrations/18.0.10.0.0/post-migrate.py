"""18.0.10.0.0 post-migration — seed per-vehicle pallet layout rows.

DATA CONFIGURATION ONLY: every existing vehicle gets STANDARD and PINWHEEL
(and TURNED when configured) layout rows derived from its legacy capacity
fields, so VehicleCapacityService has explicit per-vehicle layout data.
No values are hardcoded anywhere else in the application.
"""
import logging

_logger = logging.getLogger(__name__)


def seed_vehicle_pallet_layouts(cr):
    cr.execute("""
        INSERT INTO fleet_vehicle_pallet_layout
            (vehicle_id, name, code, layout_type, max_pallets, sequence,
             is_default, active, create_uid, write_uid, create_date, write_date)
        SELECT v.id, 'Standard', 'standard', 'standard',
               v.straight_pallet_capacity, 10, TRUE, TRUE, 1, 1, now(), now()
        FROM fleet_vehicle v
        WHERE v.straight_pallet_capacity > 0
          AND NOT EXISTS (
              SELECT 1 FROM fleet_vehicle_pallet_layout l
              WHERE l.vehicle_id = v.id AND l.code = 'standard')
    """)
    cr.execute("""
        INSERT INTO fleet_vehicle_pallet_layout
            (vehicle_id, name, code, layout_type, max_pallets, sequence,
             is_default, active, create_uid, write_uid, create_date, write_date)
        SELECT v.id, 'Pinwheel', 'pinwheel', 'pinwheel',
               v.pin_wheel_pallet_capacity, 20, FALSE, TRUE, 1, 1, now(), now()
        FROM fleet_vehicle v
        WHERE v.pin_wheel_pallet_capacity > 0
          AND NOT EXISTS (
              SELECT 1 FROM fleet_vehicle_pallet_layout l
              WHERE l.vehicle_id = v.id AND l.code = 'pinwheel')
    """)



def migrate(cr, version):
    _logger.info("18.0.10.0.0 post-migration: seeding vehicle pallet layouts")
    seed_vehicle_pallet_layouts(cr)
    _logger.info("18.0.10.0.0 post-migration complete")

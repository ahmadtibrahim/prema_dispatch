-- Prema AI V4 — Performance Indexes (idempotent, run-once)
-- Applies indexes on Prod-db-test1a or Prod-db after module upgrade.
-- Each statement uses IF NOT EXISTS to prevent duplicate-creation errors.

-- prema.dispatch.job
CREATE INDEX IF NOT EXISTS idx_dispatch_job_scheduled_pickup ON prema_dispatch_job (scheduled_pickup);
CREATE INDEX IF NOT EXISTS idx_dispatch_job_vehicle_pickup ON prema_dispatch_job (vehicle_id, scheduled_pickup);
CREATE INDEX IF NOT EXISTS idx_dispatch_job_driver_pickup ON prema_dispatch_job (driver_id, scheduled_pickup);
CREATE INDEX IF NOT EXISTS idx_dispatch_job_source ON prema_dispatch_job (source_model, source_res_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_job_invoice ON prema_dispatch_job (invoice_id);

-- logistics.corridor.departure
CREATE INDEX IF NOT EXISTS idx_corridor_dep_date ON logistics_corridor_departure (departure_date);
CREATE INDEX IF NOT EXISTS idx_corridor_dep_vehicle_date ON logistics_corridor_departure (vehicle_id, departure_date);
CREATE INDEX IF NOT EXISTS idx_corridor_dep_corridor_date ON logistics_corridor_departure (corridor_id, departure_date);
CREATE INDEX IF NOT EXISTS idx_corridor_dep_status_date ON logistics_corridor_departure (status, departure_date);

-- logistics.booking
CREATE INDEX IF NOT EXISTS idx_booking_partner_state ON logistics_booking (partner_id, state);
CREATE INDEX IF NOT EXISTS idx_booking_source_idempotency ON logistics_booking (source_channel, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_booking_departure ON logistics_booking (departure_id);
CREATE INDEX IF NOT EXISTS idx_booking_corridor ON logistics_booking (corridor_id);
CREATE INDEX IF NOT EXISTS idx_booking_invoice ON logistics_booking (invoice_id);
CREATE INDEX IF NOT EXISTS idx_booking_dispatch ON logistics_booking (dispatch_job_id);
CREATE INDEX IF NOT EXISTS idx_booking_tracking_token ON logistics_booking (tracking_token);
CREATE INDEX IF NOT EXISTS idx_booking_pickup_fsa ON logistics_booking (pickup_fsa_id);
CREATE INDEX IF NOT EXISTS idx_booking_delivery_fsa ON logistics_booking (delivery_fsa_id);

-- logistics.booking.leg
CREATE INDEX IF NOT EXISTS idx_booking_leg_booking_seq ON logistics_booking_leg (booking_id, sequence);
CREATE INDEX IF NOT EXISTS idx_booking_leg_departure_state ON logistics_booking_leg (departure_id, reservation_state);
CREATE INDEX IF NOT EXISTS idx_booking_leg_regions ON logistics_booking_leg (origin_region_id, destination_region_id);

-- logistics.booking.stop
CREATE INDEX IF NOT EXISTS idx_booking_stop_booking_seq ON logistics_booking_stop (booking_id, sequence);
CREATE INDEX IF NOT EXISTS idx_booking_stop_location ON logistics_booking_stop (saved_location_id);

-- account.move
CREATE INDEX IF NOT EXISTS idx_account_move_booking ON account_move (logistics_booking_id);

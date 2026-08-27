"""Multi-stop booking stop — one record per pickup or delivery on a logistics.booking."""
import math

from odoo import api, fields, models


class LogisticsBookingStop(models.Model):
    _name = "logistics.booking.stop"
    _description = "Booking Stop (Pickup / Delivery)"
    _order = "booking_id, sequence"

    booking_id = fields.Many2one("logistics.booking", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, default=10)
    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")], required=True)
    stop_key = fields.Char(
        string="Stable Stop Key", index=True,
        help="Client/session stable identifier mapped to this persistent "
             "stop at confirmation (never a transient array index).")
    hub_transfer_stop = fields.Boolean(
        string="Hub Transfer Placeholder",
        default=False,
        help="True for corridor-hub placeholder stops created purely for "
             "multi-leg transfer topology. NEVER an operational stop, "
             "never customer-facing: excluded from dispatch bridges, "
             "tracking, route display and invoice descriptions.")
    liftgate_required = fields.Boolean(string="Liftgate Required")
    dock_available = fields.Boolean(string="Dock Available")
    appointment_required = fields.Boolean(string="Appointment Required")
    timing_type = fields.Selection([
        ("flexible", "Flexible"),
        ("time_window", "Time Window"),
        ("exact_appointment", "Exact Appointment"),
        ("deadline", "Hard Deadline"),
    ], default="flexible")
    service_date = fields.Date()
    window_start = fields.Float(string="Window Start (24h float)")
    window_end = fields.Float(string="Window End (24h float)")
    appointment_time = fields.Float(string="Appointment Time (24h float)")
    hard_deadline = fields.Datetime(string="Hard Deadline")
    service_time_minutes = fields.Integer(default=15)
    operating_hours_snapshot = fields.Json(
        string="Operating Hours Snapshot",
        help="Facility operating hours frozen at confirmation; planned "
             "against, never silently re-read from the master location.")
    timezone = fields.Char(default="America/Toronto")

    # Canonical facility (SAVED LOCATION CONSOLIDATION: one building =
    # one prema.dispatch.location row). The legacy
    # logistics.saved.location customer-profile M2O was retired in
    # 18.0.13.25.0 (zero live references proven before the drop).
    saved_location_id = fields.Many2one("prema.dispatch.location", string="Saved Location", index=True, ondelete="set null")

    # Identity
    company_name = fields.Char()
    location_name = fields.Char()
    branch_number = fields.Char()
    unit = fields.Char()

    # Address
    formatted_address = fields.Char()
    street = fields.Char()
    city = fields.Char()
    province_state = fields.Char(string="Province / State")
    postal_zip = fields.Char(string="Postal / ZIP")
    country_id = fields.Many2one("res.country")
    google_place_id = fields.Char()
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))

    # Contact
    contact_name = fields.Char()
    phone = fields.Char()
    email = fields.Char()

    # Time window
    requested_date = fields.Date()
    requested_time_from = fields.Float(string="Time From (hrs)", help="e.g. 8.0 = 8:00 AM")
    requested_time_to = fields.Float(string="Time To (hrs)", help="e.g. 12.0 = 12:00 PM")

    # Load
    pallet_count = fields.Integer(default=0)
    weight_lb = fields.Float(default=0.0)

    # Access
    dock_available = fields.Boolean()
    liftgate_required = fields.Boolean()
    appointment_required = fields.Boolean()

    # Reference
    reference = fields.Char()
    movement_pallet_labels = fields.Char(
        string="Movement Pallets", compute="_compute_movement_totals",
    )
    movement_pallet_count = fields.Integer(
        string="Movement Pallets", compute="_compute_movement_totals",
    )
    movement_weight_lbs = fields.Float(
        string="Movement Weight (lbs)", compute="_compute_movement_totals",
        digits=(10, 1),
    )
    # Timing
    timing_type = fields.Selection([
        ("flexible", "Flexible"), ("time_window", "Time Window"),
        ("exact_appointment", "Exact Appointment"),
    ], default="flexible", string="Timing")
    requested_service_date = fields.Date(string="Requested Date")
    window_start = fields.Float(string="Window Start")
    window_end = fields.Float(string="Window End")
    appointment_time = fields.Float(string="Appointment Time")
    timezone = fields.Char(string="Timezone")

    instructions = fields.Text()

    @api.depends(
        "stop_type", "booking_id.route_model_version",
        "booking_id.pallet_ids.active", "booking_id.pallet_ids.sequence",
        "booking_id.pallet_ids.weight_lbs", "booking_id.pallet_ids.pickup_stop_id",
        "booking_id.pallet_ids.delivery_allocation_ids.active",
        "booking_id.pallet_ids.delivery_allocation_ids.delivery_stop_id",
        "booking_id.pallet_ids.delivery_allocation_ids.weight_lbs",
    )
    def _compute_movement_totals(self):
        for stop in self:
            pallets = stop.booking_id.pallet_ids.filtered(
                lambda pallet: pallet.active and (
                    pallet.pickup_stop_id == stop
                    or stop in pallet.delivery_allocation_ids.filtered("active").mapped("delivery_stop_id")
                )
            )
            if stop.stop_type == "pickup":
                total_weight = sum(pallets.mapped("weight_lbs"))
            else:
                total_weight = sum(
                    allocation.weight_lbs or 0.0
                    for pallet in pallets
                    for allocation in pallet.delivery_allocation_ids.filtered(
                        lambda item: item.active and item.delivery_stop_id == stop
                    )
                )
            labels = ", ".join(
                pallet.label or "P%d" % (pallet.sequence // 10 or index)
                for index, pallet in enumerate(pallets.sorted("sequence"), 1)
            )
            stop.movement_pallet_labels = labels or "—"
            stop.movement_pallet_count = len(pallets)
            stop.movement_weight_lbs = round(total_weight, 1)

    # ── Facility/coordinate integrity ──────────────────────────────────
    # This snapshot (company, street, city, postal, lat/lng) is the
    # historical authority. The linked master location may only supplement
    # it (dock, entrance pin, metadata) — never silently replace it. This
    # computed flag surfaces a master link that points at a different
    # facility so the portal/session can flag it before confirmation.
    location_mismatch_warning = fields.Char(
        string="Location Mismatch", compute="_compute_location_mismatch_warning",
        help="Set when the linked master saved location's pin is a "
             "materially different place from this stop's confirmed "
             "coordinates (Booking 185: United Dairy's pickup was linked "
             "to 'Demo Logistics Customer'). Empty = consistent.")

    @api.depends("saved_location_id.pin_lat", "saved_location_id.pin_lng",
                 "saved_location_id.address", "latitude", "longitude")
    def _compute_location_mismatch_warning(self):
        for stop in self:
            loc = stop.saved_location_id
            warning = False
            if loc and stop.latitude and stop.longitude:
                if loc.pin_lat and loc.pin_lng:
                    lat1, lng1 = stop.latitude, stop.longitude
                    lat2, lng2 = loc.pin_lat, loc.pin_lng
                    radius = 6371.0
                    dlat = math.radians(lat2 - lat1)
                    dlng = math.radians(lng2 - lng1)
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(math.radians(lat1))
                         * math.cos(math.radians(lat2))
                         * math.sin(dlng / 2) ** 2)
                    dist = 2 * radius * math.asin(min(1.0, math.sqrt(a)))
                    if dist > 2.0:
                        warning = (
                            "Saved location '%s' is %.0f km from this stop's "
                            "confirmed address — verify the company link "
                            "before confirming." % (loc.name, dist))
                elif loc.address and stop.city:
                    # Location without a pin — compare address text (the
                    # real Demo Logistics record had no pin either).
                    if stop.city.lower() not in (loc.address or "").lower():
                        warning = (
                            "Saved location '%s' ('%s') does not match this "
                            "stop's confirmed city '%s' — verify the company "
                            "link before confirming."
                            % (loc.name, loc.address, stop.city))
            stop.location_mismatch_warning = warning

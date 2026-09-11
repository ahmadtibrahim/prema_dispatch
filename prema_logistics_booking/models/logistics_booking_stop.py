"""Multi-stop booking stop — one record per pickup or delivery on a logistics.booking."""
import math
from datetime import datetime, time, timedelta

import pytz

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
        ("facility_hours", "Facility Hours"),
        ("earliest_time", "Earliest Time"),
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
    # TIER 2 (§4/§5): facility hours and the shipment's own timing are TWO
    # separate facts and must coexist. These carry the facility's own
    # open/close for the shipment's declared operating day, so an
    # appointment window never has to be abused to express "the dock is
    # open 06:00–16:00". Explicit fields (not a reuse of window_start/
    # window_end, which belong to the CUSTOMER's window).
    facility_open_time = fields.Float(
        string="Facility Open (24h float)",
        help="Facility's own opening time on the shipment's declared day "
             "(e.g. 6.0 = 06:00). Set from the shipment's tender document "
             "or the master location — never overwritten by the shipment's "
             "appointment window.")
    facility_close_time = fields.Float(
        string="Facility Close (24h float)",
        help="Facility's own closing time on the shipment's declared day "
             "(e.g. 16.0 = 16:00). Kept separately from the appointment "
             "window (Tier 2 §4).")
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
    # Legacy duplicate declarations removed (18.0.13.x): timing_type is
    # declared ONCE above (with the deadline option), requested_service_date
    # kept here as a historical alias.
    requested_service_date = fields.Date(string="Requested Date")

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

    def _local_hour_to_utc(self, day, hours_float):
        """A declared LOCAL clock hour on `day` → Odoo's naive UTC.

        The shipment's times are quoted in the FACILITY's own timezone
        (`self.timezone`), never in UTC. Writing "11:00" straight into a
        Datetime column makes the Planner and the driver read it back as
        07:00 EDT — the appointment silently moves four hours earlier
        (§6/§7/§11). The old inline combination did exactly that; it was
        dormant only because nothing populated these fields until the
        Book Load wizard began carrying real appointments.
        """
        hours = float(hours_float or 0.0) % 24.0
        whole = int(hours)
        minutes = int(round((hours - whole) * 60))
        local_naive = datetime.combine(day, time(0)) + timedelta(
            hours=whole, minutes=minutes)
        try:
            tzinfo = pytz.timezone(self.timezone or "America/Toronto")
        except Exception:
            tzinfo = pytz.timezone("America/Toronto")
        return tzinfo.localize(local_naive).astimezone(pytz.UTC).replace(tzinfo=None)

    def _dispatch_timing_vals(self, day):
        """Map this booking stop's timing to prema.dispatch.stop timing
        fields — the single authority used by BOTH the movement_v1 bridge
        and the legacy _create_dispatch_operation extra-stop copies.

        day: operation date (date) used to combine the 24h-float times.
        Returns only the fields that apply (time_window_type always).

        TIER 2: facility hours travel on their OWN fields
        (facility_open_time / facility_close_time) alongside whichever
        customer window applies — an exact appointment never erases the
        dock's opening hours and vice versa (§4).
        """
        vals = {"time_window_type": "flexible"}
        day = day or fields.Date.context_today(self)

        def _combine(hours_float):
            return self._local_hour_to_utc(day, hours_float)

        # ── Facility hours: a separate channel, never a window type ────
        if self.facility_open_time is not None and self.facility_close_time:
            vals.update({
                "facility_open_time": self.facility_open_time,
                "facility_close_time": self.facility_close_time,
            })

        if self.timing_type == "facility_hours":
            # The dock's own hours ARE the constraint — no customer window.
            # The ETA engine evaluates them from the frozen
            # operating_hours_snapshot; the explicit fields above are what
            # the planner displays.
            pass
        elif self.timing_type == "earliest_time":
            if self.window_start is not None:
                vals.update({
                    "time_window_type": "window",
                    "earliest_time": _combine(self.window_start),
                })
        elif self.timing_type == "time_window" and self.window_start is not None:
            vals.update({
                "time_window_type": "window",
                "earliest_time": _combine(self.window_start),
                "latest_time": _combine(self.window_end if self.window_end is not None
                                        else self.window_start),
            })
        elif self.timing_type == "exact_appointment" and (
                self.appointment_time is not None or self.window_start is not None):
            start = (self.appointment_time if self.appointment_time is not None
                     else self.window_start)
            vals.update({
                "time_window_type": "exact",
                "exact_time": _combine(start),
            })
            # An appointment is quoted as a RANGE ("11:00 AM to 12:00 PM").
            # Keep the quoted end so the driver/planner never shows a
            # narrower commitment than the facility actually gave.
            # (0.0 means "no end quoted", not midnight.)
            if self.window_end and self.window_end != start:
                vals["latest_time"] = _combine(self.window_end)
        elif self.timing_type == "deadline" and self.hard_deadline:
            vals.update({
                "time_window_type": "deadline",
                "deadline_time": self.hard_deadline,
                "hard_deadline": True,
            })
        return vals

    def _job_timing_vals(self, day, prefix):
        """Dispatch JOB header window fields for this side (§6), derived
        from THIS stop's timing — replaces the hardcoded
        ``*_window_type = "flexible"``.

        Facility hours deliberately do NOT become a job window type: the
        job header states the CUSTOMER's commitment, and the facility's
        own hours ride on the stop's explicit fields plus the frozen
        snapshot (§4). A facility-hours-only stop is genuinely flexible
        from the customer's side.
        """
        vals = {}
        day = day or fields.Date.context_today(self)

        def _combine(hours_float):
            return self._local_hour_to_utc(day, hours_float)

        if self.timing_type == "earliest_time" and self.window_start is not None:
            vals.update({
                "%s_window_type" % prefix: "window",
                "%s_earliest" % prefix: _combine(self.window_start),
            })
        elif self.timing_type == "time_window" and self.window_start is not None:
            vals.update({
                "%s_window_type" % prefix: "window",
                "%s_earliest" % prefix: _combine(self.window_start),
                "%s_latest" % prefix: _combine(
                    self.window_end if self.window_end is not None
                    else self.window_start),
            })
        elif self.timing_type == "exact_appointment" and (
                self.appointment_time is not None or self.window_start is not None):
            start = (self.appointment_time if self.appointment_time is not None
                     else self.window_start)
            vals.update({
                "%s_window_type" % prefix: "exact",
                "%s_exact_time" % prefix: _combine(start),
                "%s_earliest" % prefix: _combine(start),
            })
            if self.window_end and self.window_end != start:
                vals["%s_latest" % prefix] = _combine(self.window_end)
        elif self.timing_type == "deadline" and self.hard_deadline:
            vals["%s_window_type" % prefix] = "deadline"
            if prefix == "delivery":
                vals["delivery_deadline"] = self.hard_deadline
        return vals

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

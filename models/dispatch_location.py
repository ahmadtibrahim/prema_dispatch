import logging

from odoo import api, fields, models
from odoo.osv import expression

_logger = logging.getLogger(__name__)


class PremaDispatchLocation(models.Model):
    """Saved delivery/pickup locations — remembers exact parking pin and entrance photo.

    When a stop address is matched against a saved location, the precise
    pin position (pin_lat/pin_lng) is pre-loaded so drivers always park in
    the right spot without re-pinning every time.
    """
    _name = "prema.dispatch.location"
    _description = "Saved Dispatch Location"
    _order = "use_count desc, name"
    _rec_name = "name"

    name = fields.Char(string="Location Name", required=True)
    business_name = fields.Char(
        string="Business Name",
        help="The customer/warehouse's actual business name — shown on the driver app "
             "so repeat stops (e.g. two pickups from the same shipper) are recognizable "
             "at a glance instead of just an address.",
    )
    address = fields.Char(string="Address", required=True)
    google_place_id = fields.Char(
        string="Google Place ID",
        help="Google Places identifier for precise address matching.",
    )

    # Address Validation API (same pattern as prema.dispatch.stop)
    address_validated = fields.Boolean(string="Address Validated", readonly=True)
    address_validation_warning = fields.Char(string="Address Warning", readonly=True)
    address_formatted = fields.Char(string="Validated Address", readonly=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer / Warehouse",
        ondelete="set null",
        help="Link to a contact if this location belongs to a specific customer.",
    )

    # Precise parking pin (manually set by dispatcher or driver)
    pin_lat = fields.Float(string="Pin Latitude",  digits=(10, 6))
    pin_lng = fields.Float(string="Pin Longitude", digits=(10, 6))
    pin_set = fields.Boolean(
        string="Pin Manually Set",
        help="True when a dispatcher or driver has manually positioned the parking pin.",
    )

    active = fields.Boolean(default=True)

    location_type = fields.Selection([
        ("warehouse",  "Warehouse"),
        ("customer",   "Customer"),
        ("relay",      "Relay / Transfer Point"),
        ("rest_area",  "Rest Area"),
        ("fuel_station", "Fuel Station"),
        ("gym", "Gym"),
        ("hotel_motel", "Motels / Hotels"),
        ("other",      "Other"),
    ], string="Location Type", default="customer")
    allow_cross_dock = fields.Boolean(
        string="Allow Cross-Dock",
        help="This location can be used to temporarily transfer freight between loads "
             "(e.g. drop one job's skid here, pick up another job's freight, come back "
             "for it later) — a property of a location, not a separate Location Type. "
             "A Warehouse most commonly allows this, but any location type can.",
    )

    # Entrance photo
    entrance_photo = fields.Binary(string="Entrance Photo", attachment=True)
    entrance_photo_fname = fields.Char(string="Photo Filename")

    # Additional site photos (same pattern as entrance_photo/entrance_photo_fname)
    dock_photo = fields.Binary(string="Dock Photo", attachment=True)
    dock_photo_fname = fields.Char(string="Dock Photo Filename")
    parking_photo = fields.Binary(string="Parking Photo", attachment=True)
    parking_photo_fname = fields.Char(string="Parking Photo Filename")
    gate_photo = fields.Binary(string="Gate Photo", attachment=True)
    gate_photo_fname = fields.Char(string="Gate Photo Filename")
    receiving_office_photo = fields.Binary(string="Receiving Office Photo", attachment=True)
    receiving_office_photo_fname = fields.Char(string="Receiving Office Photo Filename")
    scale_photo = fields.Binary(string="Scale Photo", attachment=True)
    scale_photo_fname = fields.Char(string="Scale Photo Filename")
    loading_area_photo = fields.Binary(string="Loading Area Photo", attachment=True)
    loading_area_photo_fname = fields.Char(string="Loading Area Photo Filename")

    # Instructions
    parking_notes = fields.Text(
        string="Parking / Entrance Notes",
        help="Instructions for drivers: dock door, entrance side, gate code, etc.",
    )
    driver_instructions = fields.Text(
        string="Driver Instructions",
        help="Structured turn-by-turn / arrival guidance for drivers, separate from the "
             "general dispatcher notes in Parking / Entrance Notes.",
    )
    security_notes = fields.Text(
        string="Security Notes",
        help="Security procedures, ID checks, sign-in requirements, etc.",
    )
    dock_door = fields.Char(string="Default Dock / Door #")
    receiving_entrance = fields.Char(string="Receiving Entrance")
    truck_entrance = fields.Char(string="Truck Entrance")
    gate_code = fields.Char(string="Gate Code")

    # Equipment / access
    liftgate_required = fields.Boolean(string="Liftgate Required")
    dock_height_available = fields.Boolean(string="Dock Height Available")
    pump_truck_required = fields.Boolean(string="Pump Truck Required")
    straight_truck_accessible = fields.Boolean(string="Straight Truck Accessible", default=True)
    trailer_53ft_accessible = fields.Boolean(string="53' Trailer Accessible", default=True)

    geofence_radius = fields.Integer(
        string="Geofence Radius (m)", default=150,
        help="Arrival geofence radius in meters — how close the driver's GPS must be "
             "before an arrival is auto-detected.",
    )

    # Stats
    use_count = fields.Integer(string="Times Visited", default=0, readonly=True)
    last_visited = fields.Datetime(string="Last Visited", readonly=True)

    # Auto-tracked visit stats (updated by record_visit_stats(), not user-entered)
    average_wait_minutes = fields.Float(string="Avg Wait (min)", readonly=True)
    average_unload_minutes = fields.Float(string="Avg Unload (min)", readonly=True)
    average_total_stop_minutes = fields.Float(string="Avg Total Stop (min)", readonly=True)
    fastest_stop_minutes = fields.Float(string="Fastest Stop (min)", readonly=True)
    longest_stop_minutes = fields.Float(string="Longest Stop (min)", readonly=True)
    average_delay_minutes = fields.Float(string="Avg Delay (min)", readonly=True)
    appointment_compliance_pct = fields.Float(string="Appointment Compliance %", readonly=True)
    arrival_accuracy_pct = fields.Float(string="Arrival Accuracy %", readonly=True)

    # AI/heuristic scores (readonly, computed — simple heuristics, not real ML)
    recommended_service_time_minutes = fields.Integer(string="Recommended Service Time (min)", readonly=True)
    best_arrival_window_start = fields.Char(string="Best Arrival Window Start", readonly=True)
    best_arrival_window_end = fields.Char(string="Best Arrival Window End", readonly=True)
    avoid_arrival_window_start = fields.Char(string="Avoid Arrival Window Start", readonly=True)
    avoid_arrival_window_end = fields.Char(string="Avoid Arrival Window End", readonly=True)
    difficulty_score = fields.Float(string="Difficulty Score", readonly=True)
    dock_efficiency_score = fields.Float(string="Dock Efficiency Score", readonly=True)
    traffic_impact_score = fields.Float(string="Traffic Impact Score", readonly=True)
    ai_confidence_score = fields.Float(
        string="AI Confidence Score", readonly=True,
        help="Reflects how many visits the stats above are based on — low with few "
             "visits, higher after many. Not a measure of accuracy, just sample size.",
    )

    # Linked stops (computed count)
    stop_ids = fields.One2many(
        "prema.dispatch.stop", "saved_location_id", string="Stops"
    )
    stop_count = fields.Integer(compute="_compute_stop_count", string="# Stops")

    @api.depends("stop_ids")
    def _compute_stop_count(self):
        for loc in self:
            loc.stop_count = len(loc.stop_ids)

    def name_get(self):
        """Show the real saved-location business name in stop/company pickers,
        falling back to the internal location name or address when needed."""
        return [
            (
                rec.id,
                rec.business_name or rec.name or rec.address or "",
            )
            for rec in self
        ]

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        args = list(args or [])
        if name:
            args = expression.AND([
                args,
                expression.OR([
                    [("business_name", operator, name)],
                    [("name", operator, name)],
                    [("address", operator, name)],
                ]),
            ])
        return self.search(args, limit=limit).name_get()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.address and not rec.address_validated:
                rec._validate_address()
        return records

    def _validate_address(self):
        """Check address accuracy via Google's Address Validation API — same
        pattern as prema.dispatch.stop._validate_address(). Does not rewrite
        the address, only flags/stores the standardized form for reference."""
        self.ensure_one()
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        if not api_key or not self.address:
            return False
        try:
            import requests
            r = requests.post(
                "https://addressvalidation.googleapis.com/v1:validateAddress",
                params={"key": api_key},
                json={"address": {"regionCode": "CA", "addressLines": [self.address]}},
                timeout=5,
            )
            data = r.json()
            result = data.get("result")
            if result is None:
                return False
            verdict = result.get("verdict", {})
            warning = ""
            if not verdict.get("addressComplete", True):
                warning = "Address looks incomplete"
            elif verdict.get("hasUnconfirmedComponents"):
                warning = "Some address details could not be confirmed"
            elif verdict.get("hasReplacedComponents"):
                warning = "Address auto-corrected — please verify"
            self.write({
                "address_validated": True,
                "address_validation_warning": warning,
                "address_formatted": result.get("address", {}).get("formattedAddress", ""),
            })
            return True
        except Exception:
            _logger.exception("Address validation failed for location %s (%r)", self.id, self.address)
            return False

    @api.model
    def find_or_create_by_address(self, address, place_id=None, business_name=None, partner_id=None):
        """Return an existing saved location matching address/place_id, or create one.

        Matching order: Google place_id (most precise) → exact address text
        → same business_name + partner (catches the same shipper re-entered
        with slightly different address text, e.g. two pickups from the same
        warehouse). This is what lets a repeat stop reuse the saved pin,
        notes, and photo instead of starting from scratch every time.

        search() applies Odoo's default active_test, so only active=True
        locations are matched/reused here — an archived location will not
        be silently reattached to new stops.
        """
        if not address:
            return self.browse()

        existing = self.browse()
        if place_id:
            existing = self.search([("google_place_id", "=", place_id)], limit=1)
        if not existing:
            existing = self.search([("address", "=ilike", address.strip())], limit=1)
        if not existing and business_name and partner_id:
            existing = self.search([
                ("business_name", "=ilike", business_name.strip()),
                ("partner_id", "=", partner_id),
            ], limit=1)
        if existing:
            return existing

        return self.create({
            "name":           business_name or address[:80],
            "business_name":  business_name or "",
            "address":        address,
            "google_place_id": place_id or "",
            "partner_id":     partner_id or False,
        })

    def update_pin(self, lat, lng, source="dispatcher"):
        """Update the parking pin position and mark as manually set."""
        self.ensure_one()
        self.write({
            "pin_lat": lat,
            "pin_lng": lng,
            "pin_set": True,
        })
        return True

    def record_visit(self):
        """Increment visit counter and update last-visited timestamp."""
        self.ensure_one()
        self.write({
            "use_count":    self.use_count + 1,
            "last_visited": fields.Datetime.now(),
        })

    def record_visit_stats(self, stop):
        """Roll a just-completed stop's timing into this location's running
        averages and heuristic scores. Called from the stop-completion path
        (prema.dispatch.stop.action_mark_completed / dispatch_job.driver_update_stop)
        when the stop is linked to a saved location.

        NOTE: these are simple running-average heuristics, not real ML —
        good enough to nudge dispatchers, not to be treated as guaranteed.
        """
        self.ensure_one()
        if not stop:
            return False

        arrival = stop.actual_arrival_time
        departure = stop.actual_departure_time
        total_minutes = None
        if arrival and departure:
            total_minutes = (departure - arrival).total_seconds() / 60.0

        unload_minutes = stop.service_time_minutes or None

        # Compliance: was the arrival within the requested window/appointment?
        target_time = stop.exact_time or stop.earliest_time or stop.latest_time or stop.scheduled_time
        delay_minutes = None
        on_time = None
        if arrival and target_time:
            delay_minutes = (arrival - target_time).total_seconds() / 60.0
            if stop.exact_time:
                on_time = abs(delay_minutes) <= 15
            elif stop.latest_time:
                on_time = arrival <= stop.latest_time
            elif stop.earliest_time:
                on_time = arrival >= stop.earliest_time

        # Running averages — weight the new sample against use_count (already
        # incremented by record_visit() before this is normally called, so
        # guard against divide-by-zero on the very first visit).
        n = max(self.use_count, 1)
        prev_n = max(n - 1, 0)

        def rolling_avg(prev_avg, new_value):
            if new_value is None:
                return prev_avg
            if prev_n <= 0:
                return new_value
            return ((prev_avg * prev_n) + new_value) / n

        vals = {}

        if total_minutes is not None:
            vals["average_total_stop_minutes"] = rolling_avg(self.average_total_stop_minutes, total_minutes)
            vals["fastest_stop_minutes"] = (
                total_minutes if not self.fastest_stop_minutes
                else min(self.fastest_stop_minutes, total_minutes)
            )
            vals["longest_stop_minutes"] = max(self.longest_stop_minutes or 0.0, total_minutes)

        if unload_minutes is not None:
            vals["average_unload_minutes"] = rolling_avg(self.average_unload_minutes, unload_minutes)

        if total_minutes is not None and unload_minutes is not None:
            wait_minutes = max(total_minutes - unload_minutes, 0.0)
            vals["average_wait_minutes"] = rolling_avg(self.average_wait_minutes, wait_minutes)

        if delay_minutes is not None:
            vals["average_delay_minutes"] = rolling_avg(self.average_delay_minutes, delay_minutes)

        if on_time is not None:
            prev_compliance = self.appointment_compliance_pct or 0.0
            prev_hits = round(prev_compliance / 100.0 * prev_n)
            new_hits = prev_hits + (1 if on_time else 0)
            vals["appointment_compliance_pct"] = (new_hits / n) * 100.0

        if delay_minutes is not None:
            # Arrival accuracy: how close to the target time, as a 0-100 score
            # that decays with growing lateness/earliness (0 at 60+ min off).
            accuracy_sample = max(0.0, 100.0 - (abs(delay_minutes) / 60.0) * 100.0)
            vals["arrival_accuracy_pct"] = rolling_avg(self.arrival_accuracy_pct, accuracy_sample)

        # Recommended service time: rounded average unload time, falling back
        # to whatever we already had.
        if "average_unload_minutes" in vals and vals["average_unload_minutes"]:
            vals["recommended_service_time_minutes"] = int(round(vals["average_unload_minutes"]))

        # Best/avoid arrival windows: bucket this arrival's hour as "good" if
        # it was on-time/low-delay, "avoid" if it was late — a running signal,
        # not a full statistical model.
        if arrival:
            hour = arrival.hour
            window_start = f"{hour:02d}:00"
            window_end = f"{(hour + 1) % 24:02d}:00"
            if on_time is False or (delay_minutes is not None and delay_minutes > 30):
                vals["avoid_arrival_window_start"] = window_start
                vals["avoid_arrival_window_end"] = window_end
            elif on_time or (delay_minutes is not None and delay_minutes <= 0):
                vals["best_arrival_window_start"] = window_start
                vals["best_arrival_window_end"] = window_end

        # Heuristic scores (0-100, higher delay/non-compliance = higher difficulty).
        avg_delay = vals.get("average_delay_minutes", self.average_delay_minutes) or 0.0
        compliance = vals.get("appointment_compliance_pct", self.appointment_compliance_pct) or 0.0
        vals["difficulty_score"] = max(0.0, min(100.0, (max(avg_delay, 0.0) * 1.5) + (100.0 - compliance) * 0.5))

        avg_unload = vals.get("average_unload_minutes", self.average_unload_minutes) or 0.0
        if avg_unload:
            # Faster than a 30-min baseline unload = more efficient dock.
            vals["dock_efficiency_score"] = max(0.0, min(100.0, 100.0 - ((avg_unload - 30.0) / 30.0) * 100.0))

        avg_wait = vals.get("average_wait_minutes", self.average_wait_minutes) or 0.0
        vals["traffic_impact_score"] = max(0.0, min(100.0, (avg_wait / 60.0) * 100.0))

        # Confidence scales with sample size, not accuracy — heuristic only.
        vals["ai_confidence_score"] = min(100.0, self.use_count * 10)

        if vals:
            self.write(vals)
        return True

import logging

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


def tz_from_longitude_band(lat, lng):
    """Rough North-American timezone estimate from longitude alone, used only
    when the Time Zone API call fails/has no key. Real timezone boundaries
    follow provincial/state lines, not longitude, so this is deliberately a
    fallback, not the primary path — see _lookup_timezone()."""
    if lng is None:
        return "America/Toronto"
    if lng <= -122:
        return "America/Vancouver"
    if lng <= -113:
        return "America/Edmonton"
    if lng <= -95:
        return "America/Winnipeg"
    if lng <= -68:
        return "America/Toronto"
    return "America/Halifax"


class PremaDispatchStop(models.Model):
    _name = "prema.dispatch.stop"
    _description = "Dispatch Stop"
    _order = "job_id, sequence asc"

    job_id = fields.Many2one(
        "prema.dispatch.job", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(compute="_compute_name", store=True)

    stop_type = fields.Selection([
        ("pickup",   "Pickup"),
        ("dropoff",  "Drop-Off"),
        ("return",   "Return"),
        ("transfer", "Driver Transfer"),
        ("cross_dock_drop",    "Cross-Dock Drop / Transfer-In"),
        ("cross_dock_pickup",  "Cross-Dock Pickup / Transfer-Out"),
        ("other",    "Other"),
    ], default="dropoff", required=True)

    # Driver-to-driver relay handoff (stop_type="transfer"): the receiving
    # driver/truck take over the job from this point on. Reuses the job's
    # existing driver_id/vehicle_id write hook, which already logs to
    # prema.dispatch.assignment.log automatically.
    transfer_to_driver_id = fields.Many2one(
        "res.partner", string="Transfer To Driver",
        domain="[('x_is_driver','=',True)]",
        help="Driver taking over the remaining stops from this handoff point.",
    )
    transfer_to_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Transfer To Truck",
        help="Truck taking over the load, if it's also changing (leave blank if only the driver changes).",
    )
    transfer_from_driver_id = fields.Many2one(
        "res.partner", string="Transferred From Driver",
        readonly=True, copy=False,
        help="Captured when a driver-transfer stop is executed so the stop can be restored safely.",
    )
    transfer_from_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Transferred From Truck",
        readonly=True, copy=False,
        help="Captured when a driver-transfer stop is executed so the stop can be restored safely.",
    )

    status = fields.Selection([
        ("pending",   "Pending"),
        ("en_route",  "En Route"),
        ("arrived",   "Arrived"),
        ("completed", "Completed"),
        ("skipped",   "Skipped"),
        ("issue",     "Issue"),
        ("cancelled", "Cancelled"),
    ], default="pending", required=True)

    # Location
    partner_id = fields.Many2one("res.partner", string="Company / Contact")
    contact_name = fields.Char()
    contact_phone = fields.Char()
    address = fields.Char()
    dock_door = fields.Char(
        string="Dock / Door #",
        help="Warehouse dock door, bay, or unit number (e.g. Door 7, Bay 3, Unit 12). "
             "Critical for overnight and early-morning pickups.",
    )
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))
    tz_name = fields.Char(
        string="Local Timezone", readonly=True,
        help="IANA timezone at this stop's coordinates (e.g. America/Toronto), "
             "used to display times in the stop's own local time instead of "
             "always the dispatcher's timezone. Looked up via Google's Time "
             "Zone API on geocode; falls back to a longitude-band estimate "
             "for Canada if the API call fails.",
    )

    # Address Validation API (accuracy check — does not auto-rewrite the
    # dispatcher/AI-entered address, just flags it for review)
    address_validated = fields.Boolean(
        string="Address Validated", readonly=True,
        help="True once the Address Validation API has checked this address.",
    )
    address_validation_warning = fields.Char(
        string="Address Warning", readonly=True,
        help="Set when the Address Validation API flagged this address as "
             "incomplete, unconfirmed, or auto-corrected. Empty = clean.",
    )
    address_formatted = fields.Char(
        string="Validated Address", readonly=True,
        help="The standardized address returned by Google's Address Validation API, for reference.",
    )

    # Precise parking pin (separate from geocoded address lat/lng)
    saved_location_id = fields.Many2one(
        "prema.dispatch.location", string="Saved Location",
        ondelete="set null",
        help="Links to a saved location record that stores the precise parking pin and entrance photo.",
    )
    allow_cross_dock = fields.Boolean(
        related="saved_location_id.allow_cross_dock", string="Cross-Dock", readonly=True,
        help="From the Saved Location — this stop's address can be used to temporarily "
             "transfer freight between loads.",
    )
    cross_dock_origin_stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Cross-Dock Origin Stop",
        ondelete="set null",
        domain="[('job_id', '=', job_id), ('stop_type', '=', 'pickup')]",
        help="Original pickup stop whose freight is being temporarily held "
             "or reloaded at this cross-dock location.",
    )
    freight_item_ids = fields.Many2many(
        "prema.dispatch.item",
        "dispatch_stop_item_rel",
        "stop_id",
        "item_id",
        string="Freight Items / Pallets",
        domain="[('job_id', '=', job_id)]",
        help="Use this on transfer and cross-dock stops to specify the exact pallets/skids "
             "being unloaded, staged, reloaded, or handed to another truck.",
    )
    freight_item_summary = fields.Char(
        string="Freight Summary",
        compute="_compute_freight_item_summary",
    )
    pin_lat = fields.Float(
        string="Pin Latitude", digits=(10, 6),
        help="Manually set parking/entrance pin — where the driver should park.",
    )
    pin_lng = fields.Float(
        string="Pin Longitude", digits=(10, 6),
        help="Manually set parking/entrance pin — where the driver should park.",
    )
    pin_set = fields.Boolean(
        string="Pin Manually Set",
        help="True when a dispatcher or driver has dragged the pin to an exact position.",
    )

    # Multi-round grouping
    linked_load_group = fields.Integer(
        string="Load Group", default=0,
        help="Groups a pickup and its related drop-offs in a multi-round route. "
             "Round 1=1, Round 2=2, 0=ungrouped.",
    )
    shared_pallet_number = fields.Integer(
        string="Shared Pallet Number",
        default=0,
        help="Driver-entered shared pallet number for stops that ride on the same physical pallet. "
             "Example: stops marked Shared Pallet #3 will be linked to pallet U-03 / TF-03 when available.",
    )
    route_locked = fields.Boolean(
        string="Route Locked",
        help="Prevent auto-optimization from changing this stop's position.",
    )
    planning_only = fields.Boolean(
        string="Planning Anchor Placeholder",
        default=False,
        help="True when this record only preserves planning intent and must not behave as an operational stop.",
    )
    delete_request_state = fields.Selection([
        ("none", "None"),
        ("pending", "Pending Dispatcher Approval"),
        ("approved", "Approved"),
        ("denied", "Denied"),
    ], default="none", string="Driver Delete Request", copy=False)
    delete_requested_by = fields.Many2one("res.users", copy=False)
    delete_requested_at = fields.Datetime(copy=False)
    delete_request_reason = fields.Text(copy=False)
    delete_reviewed_by = fields.Many2one("res.users", copy=False)
    delete_reviewed_at = fields.Datetime(copy=False)
    delete_review_notes = fields.Text(copy=False)

    # Time Windows
    time_window_type = fields.Selection([
        ("flexible", "Flexible — Any Time"),
        ("window",   "Time Window"),
        ("exact",    "Exact Appointment"),
        ("deadline", "By Deadline"),
    ], default="flexible", string="Time Window")
    earliest_time = fields.Datetime(string="Earliest Arrival",
        help="Do not arrive before this time.")
    latest_time = fields.Datetime(string="Latest Arrival",
        help="Must arrive by this time.")
    exact_time = fields.Datetime(string="Appointment Time",
        help="Fixed arrival time — appointment scheduled.")
    deadline_time = fields.Datetime(string="Deadline",
        help="Must be completed before this time.")
    hard_deadline = fields.Boolean(string="Hard Deadline",
        help="Missing this deadline is unacceptable. System will block or warn if ETA exceeds it.")
    appointment_confirmed = fields.Boolean(string="Appointment Confirmed")

    # Schedule
    scheduled_time = fields.Datetime()
    actual_arrival_time = fields.Datetime()
    actual_departure_time = fields.Datetime()
    completed_vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Completed On Truck",
        readonly=True, copy=False,
    )
    completed_driver_id = fields.Many2one(
        "res.partner", string="Completed By Driver",
        readonly=True, copy=False,
    )

    # Route Estimates (populated by Estimate Route action via Google Maps)
    service_time_minutes = fields.Integer(
        string="Service Time (min)", default=15,
        help="Time spent at stop for loading/unloading. Default: 20 min pickup, 15 min drop-off.",
    )
    drive_time_from_prev_minutes = fields.Integer(
        string="Drive Time (min)", readonly=True,
        help="Estimated drive time from the previous stop (Google Maps).",
    )
    estimated_arrival = fields.Datetime(
        string="Est. Arrival", readonly=True,
        help="Computed: previous stop departure + drive time.",
    )
    estimated_departure = fields.Datetime(
        string="Est. Departure", readonly=True,
        help="Computed: estimated arrival + service time.",
    )

    # Load
    pallets_in = fields.Integer(string="Pallets In", default=0,
        help="Pallets loaded onto truck at this pickup stop.")
    pallets_in_estimated = fields.Boolean(
        string="Estimated from downstream deliveries", readonly=True,
        help="True when pallets_in was auto-filled from the sum of this "
             "pickup's downstream drop-offs (because the dispatcher didn't "
             "enter a pickup count) rather than entered directly. Clears "
             "itself the moment a dispatcher edits Pallets In by hand.",
    )
    pallets_out = fields.Integer(string="Pallets Out", default=0,
        help="Pallets unloaded from truck at this drop-off stop.")
    weight_in_lbs = fields.Float(string="Weight In (lbs)", digits=(10, 1))
    weight_out_lbs = fields.Float(string="Weight Out (lbs)", digits=(10, 1))
    onboard_load_after_stop = fields.Integer(
        string="Onboard After",
        compute="_compute_onboard_load", store=True, readonly=True,
        help="Running pallet count on truck after this stop completes.",
    )

    # POD & Evidence
    invoice_id = fields.Many2one(
        "account.move", string="Invoice for This Stop",
        domain="[('move_type', '=', 'out_invoice')]",
        help="Set this when a job has multiple delivery stops billed on "
             "separate invoices (e.g. LTL consolidation). POD/POP captured "
             "for this stop will attach here instead of the job's main "
             "invoice. Leave blank to use the job's invoice.",
    )
    pod_required = fields.Boolean(string="POD Required", default=False)
    pod_uploaded = fields.Boolean(
        string="POD Uploaded", compute="_compute_pod_uploaded", store=True
    )
    pod_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_stop_pod_att_rel",
        "stop_id", "attachment_id",
        string="POD Documents",
    )
    pop_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_stop_pop_att_rel",
        "stop_id", "attachment_id",
        string="POP Documents (Proof of Pickup)",
    )
    photo_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_stop_photo_att_rel",
        "stop_id", "attachment_id",
        string="Delivery Photos",
    )
    document_attachment_ids = fields.Many2many(
        "ir.attachment",
        "dispatch_stop_doc_att_rel",
        "stop_id", "attachment_id",
        string="Other Documents",
    )

    # GPS stamp at arrival
    gps_stamp_lat = fields.Float(digits=(10, 6))
    gps_stamp_lng = fields.Float(digits=(10, 6))
    gps_stamp_time = fields.Datetime()

    # Notes
    driver_notes = fields.Text()
    dispatcher_notes = fields.Text()
    issue_reason = fields.Text()

    # ── Computed ──────────────────────────────────────────────────

    @api.depends(
        "job_id.stop_ids.pallets_in",
        "job_id.stop_ids.pallets_out",
        "job_id.stop_ids.sequence",
        "job_id.stop_ids.stop_type",
    )
    def _compute_onboard_load(self):
        for stop in self:
            all_stops = list(stop.job_id.stop_ids.sorted("sequence"))
            if not all_stops:
                stop.onboard_load_after_stop = 0
                continue

            def _effective_pickup(idx):
                """pallets_in for pickup; infer from following drop-offs if zero.
                A cross-dock reload always has pallets_in set explicitly (the
                amount being taken back onboard), so it skips inference."""
                s = all_stops[idx]
                if s.stop_type == "cross_dock_pickup":
                    return s.pallets_in
                if s.stop_type != "pickup":
                    return 0
                if s.pallets_in > 0:
                    return s.pallets_in
                next_pickup = next(
                    (j for j in range(idx + 1, len(all_stops))
                     if all_stops[j].stop_type == "pickup"),
                    len(all_stops),
                )
                return sum(all_stops[j].pallets_out for j in range(idx + 1, next_pickup))

            running = 0
            for i, s in enumerate(all_stops):
                if s.stop_type in ("pickup", "cross_dock_pickup"):
                    running += _effective_pickup(i)
                elif s.stop_type in ("dropoff", "return", "cross_dock_drop", "transfer"):
                    running = max(0, running - s.pallets_out)
                if s.id == stop.id:
                    break
            stop.onboard_load_after_stop = running

    @api.depends("freight_item_ids", "freight_item_ids.name", "freight_item_ids.item_ref", "freight_item_ids.pallet_count")
    def _compute_freight_item_summary(self):
        for stop in self:
            labels = []
            for item in stop.freight_item_ids[:3]:
                labels.append(item.display_label())
            if len(stop.freight_item_ids) > 3:
                labels.append(f"+{len(stop.freight_item_ids) - 3} more")
            stop.freight_item_summary = ", ".join(labels)

    @api.depends("sequence", "stop_type", "address")
    def _compute_name(self):
        type_labels = {
            "pickup":  "Pickup",
            "dropoff": "Drop-Off",
            "return":  "Return",
            "transfer": "Driver Transfer",
            "cross_dock_drop":   "Cross-Dock Drop / Transfer-In",
            "cross_dock_pickup": "Cross-Dock Pickup / Transfer-Out",
            "other":   "Stop",
        }
        for stop in self:
            label = type_labels.get(stop.stop_type, "Stop")
            city = ""
            if stop.address:
                parts = [p.strip() for p in stop.address.split(",")]
                city = f" — {parts[-2]}" if len(parts) >= 2 else f" — {parts[0]}"
            stop.name = f"{stop.sequence // 10} {label}{city}"

    @api.depends("pod_attachment_ids")
    def _compute_pod_uploaded(self):
        for stop in self:
            stop.pod_uploaded = bool(stop.pod_attachment_ids)


    def _apply_saved_location(self, location):
        self.ensure_one()
        if not location:
            return False
        vals = self._saved_location_values(location)
        self.write(vals)
        return True

    @api.model
    def _saved_location_values(self, location):
        return {
            "saved_location_id": location.id,
            "address": location.address or "",
            "partner_id": location.partner_id.id if location.partner_id else False,
            "contact_name": location.business_name or location.name or "",
            "latitude": location.pin_lat or 0.0,
            "longitude": location.pin_lng or 0.0,
            "dock_door": location.dock_door or "",
            "pin_lat": location.pin_lat or 0.0,
            "pin_lng": location.pin_lng or 0.0,
            "pin_set": bool(location.pin_set),
        }

    # ── Onchange ──────────────────────────────────────────────────

    @api.onchange("saved_location_id")
    def _onchange_saved_location_id(self):
        """Selecting a Saved Location auto-fills the stop's address (and pin/
        contact if the location has them) — the Stops table's "Company"
        column picks from Saved Locations rather than raw Contacts so this
        fill-in actually happens."""
        for stop in self:
            loc = stop.saved_location_id
            if not loc:
                continue
            vals = stop._saved_location_values(loc)
            for key, value in vals.items():
                if key in ("partner_id", "saved_location_id"):
                    setattr(stop, key, value or False)
                elif value not in (False, None, ""):
                    setattr(stop, key, value)

    @api.onchange("transfer_to_vehicle_id")
    def _onchange_transfer_to_vehicle_id(self):
        for stop in self:
            if stop.stop_type not in ("transfer", "cross_dock_drop") or not stop.transfer_to_vehicle_id:
                continue
            driver = stop.transfer_to_vehicle_id.driver_id or stop.transfer_to_vehicle_id.x_current_driver_contact_id
            if driver and not stop.transfer_to_driver_id:
                stop.transfer_to_driver_id = driver

    @api.onchange("freight_item_ids", "stop_type")
    def _onchange_freight_item_ids(self):
        for stop in self:
            stop._sync_selected_item_pallet_counts()

    @api.onchange("stop_type")
    def _onchange_stop_type_service_time(self):
        """Default service time based on stop type."""
        defaults = {
            "pickup": 20, "dropoff": 15, "return": 20, "transfer": 10, "other": 15,
            "cross_dock_drop": 10, "cross_dock_pickup": 10,
        }
        if self.stop_type and not self.service_time_minutes:
            self.service_time_minutes = defaults.get(self.stop_type, 15)
        if self.stop_type in ("cross_dock_drop", "cross_dock_pickup", "transfer") and not self.pod_required:
            self.pod_required = True

    @api.onchange("time_window_type")
    def _onchange_time_window_type(self):
        """Clear irrelevant time fields when window type changes."""
        if self.time_window_type == "flexible":
            self.earliest_time = False
            self.latest_time = False
            self.exact_time = False
            self.deadline_time = False
        elif self.time_window_type == "exact":
            self.earliest_time = False
            self.latest_time = False
            self.deadline_time = False
        elif self.time_window_type == "deadline":
            self.earliest_time = False
            self.latest_time = False
            self.exact_time = False
        elif self.time_window_type == "window":
            self.exact_time = False
            self.deadline_time = False

    # ── Actions ───────────────────────────────────────────────────

    def action_mark_en_route(self):
        self.write({"status": "en_route"})

    def action_mark_arrived(self):
        self.write({
            "status": "arrived",
            "actual_arrival_time": fields.Datetime.now(),
        })
        self.job_id._post_timeline(
            self.job_id, "arrived_stop",
            notes=self.name or self.address,
            stop=self,
        )

    def action_mark_completed(self):
        self.ensure_one()
        self._check_completion_requirements()
        vals = {
            "status": "completed",
            "actual_departure_time": fields.Datetime.now(),
        }
        if not self.actual_arrival_time:
            vals["actual_arrival_time"] = fields.Datetime.now()
        if not self.completed_vehicle_id and self.job_id.vehicle_id:
            vals["completed_vehicle_id"] = self.job_id.vehicle_id.id
        if not self.completed_driver_id and self.job_id.driver_id:
            vals["completed_driver_id"] = self.job_id.driver_id.id
        self.write(vals)
        if self.stop_type == "cross_dock_drop" and self.transfer_to_vehicle_id:
            self._apply_receiving_truck_assignment()
        if self.saved_location_id:
            self.saved_location_id.record_visit()
            self.saved_location_id.record_visit_stats(self)
        # Timeline event based on stop type
        if self.stop_type == "pickup":
            self.job_id._post_timeline(
                self.job_id, "picked_up",
                notes=self.name or self.address,
                stop=self,
            )
        elif self.stop_type in ("dropoff", "return"):
            self.job_id._post_timeline(
                self.job_id, "delivered",
                notes=self.name or self.address,
                stop=self,
            )
        else:
            self.job_id._post_timeline(
                self.job_id, "stop_completed",
                notes=self.name or self.address,
                stop=self,
            )
        if self.stop_type in ("dropoff", "return"):
            self._mark_pallet_allocations_delivered()
        self.job_id._check_all_stops_done()

    def _mark_pallet_allocations_delivered(self):
        self.ensure_one()
        allocations = self.env["prema.dispatch.pallet.stop.allocation"].search([
            ("stop_id", "=", self.id),
            ("active", "=", True),
            ("delivered", "=", False),
        ])
        now = fields.Datetime.now()
        for alloc in allocations:
            alloc.write({
                "delivered": True,
                "delivered_at": now,
                "delivered_by": self.env.user.id,
            })
            item = alloc.dispatch_item_id
            remaining = item.stop_allocation_ids.filtered(lambda a: a.active and not a.delivered)
            item_vals = {
                "unloaded_at": now if not remaining else False,
                "unloaded_by": self.env.user.id if not remaining else False,
            }
            if remaining:
                item_vals["status"] = "partially_unloaded"
            else:
                item_vals["status"] = "delivered"
                item_vals["current_custody_type"] = "delivered"
            item.write(item_vals)

    def _check_completion_requirements(self):
        """Validate only the parts that must block completion.

        Driver proof/photo capture remains supported, but it should not
        block the stop from finishing when the dispatcher/driver needs to
        move the route forward without a POD/custody upload yet.
        """
        self.ensure_one()
        self._check_transfer_configuration()
        self._check_explicit_freight_selection()

    def _selected_item_pallet_total(self):
        self.ensure_one()
        return sum(self.freight_item_ids.mapped("pallet_count"))

    def _sync_selected_item_pallet_counts(self):
        for stop in self:
            total = stop._selected_item_pallet_total()
            if not total:
                continue
            if stop.stop_type in ("pickup", "cross_dock_pickup"):
                stop.pallets_in = total
            elif stop.stop_type in ("dropoff", "return", "cross_dock_drop", "transfer"):
                stop.pallets_out = total

    def _check_explicit_freight_selection(self):
        self.ensure_one()
        if self.stop_type not in ("transfer", "cross_dock_drop", "cross_dock_pickup"):
            return
        if self.freight_item_ids:
            return
        candidates = self._items_for_custody_transition()
        if len(candidates) > 1:
            raise exceptions.UserError(
                "Select the exact Freight Items / pallets for this transfer or cross-dock stop."
            )

    def _check_transfer_configuration(self):
        self.ensure_one()
        if self.stop_type not in ("transfer", "cross_dock_drop"):
            return
        if self.stop_type == "transfer" and not (self.saved_location_id or self.address):
            raise exceptions.UserError(
                "Driver Transfer stops need a meet-point address or Saved Location."
            )
        if self.transfer_to_driver_id and not self.transfer_to_vehicle_id:
            raise exceptions.UserError(
                "Select 'Transfer To Truck' or leave the transfer target blank to unassign the load."
            )
        if self.transfer_to_vehicle_id and not self.transfer_to_driver_id:
            driver = self.transfer_to_vehicle_id.driver_id or self.transfer_to_vehicle_id.x_current_driver_contact_id
            if not driver:
                raise exceptions.UserError(
                    "Select a receiving driver, or choose a truck that already has a driver assigned."
                )
            self.transfer_to_driver_id = driver

    def _capture_transfer_origin(self):
        self.ensure_one()
        if self.transfer_from_vehicle_id or self.transfer_from_driver_id:
            return
        prior_vehicle = self.completed_vehicle_id or self.job_id.vehicle_id
        prior_driver = self.completed_driver_id or self.job_id.driver_id
        self.write({
            "transfer_from_driver_id": prior_driver.id if prior_driver else False,
            "transfer_from_vehicle_id": prior_vehicle.id if prior_vehicle else False,
        })

    def _apply_receiving_truck_assignment(self, stage_unassigned=False):
        self.ensure_one()
        if self.stop_type not in ("transfer", "cross_dock_drop"):
            raise exceptions.UserError(
                "Only Driver Transfer and Cross-Dock Drop stops can hand freight to another truck."
            )

        self._check_transfer_configuration()
        self._capture_transfer_origin()

        location_label = (
            self.saved_location_id.business_name
            or self.saved_location_id.name
            or self.saved_location_id.address
            or self.address
            or "handoff point"
        )
        current_vehicle = self.job_id.vehicle_id
        current_driver = self.job_id.driver_id

        if stage_unassigned:
            self._transfer_saved_location()
            if current_vehicle or current_driver:
                self.job_id.write({
                    "driver_id": False,
                    "vehicle_id": False,
                })
                self.job_id.message_post(
                    body=f"Remaining route staged at {location_label} and unassigned for a later truck pickup."
                )
            return {
                "success": True,
                "applied": True,
                "unassigned": True,
                "reassigned_vehicle_id": False,
                "message": f"Remaining route staged at {location_label} and unassigned.",
            }

        target_vehicle = self.transfer_to_vehicle_id
        if not target_vehicle:
            return {
                "success": True,
                "applied": False,
                "unassigned": False,
                "reassigned_vehicle_id": current_vehicle.id if current_vehicle else False,
                "message": "No receiving truck selected.",
            }

        target_driver = (
            self.transfer_to_driver_id
            or target_vehicle.driver_id
            or target_vehicle.x_current_driver_contact_id
        )
        if not target_driver:
            raise exceptions.UserError(
                "Select a receiving driver, or choose a truck that already has a driver assigned."
            )

        current_vehicle_id = current_vehicle.id if current_vehicle else False
        current_driver_id = current_driver.id if current_driver else False
        if current_vehicle_id == target_vehicle.id and current_driver_id == target_driver.id:
            return {
                "success": True,
                "applied": False,
                "unassigned": False,
                "reassigned_vehicle_id": target_vehicle.id,
                "message": f"Remaining route stays on {target_vehicle.display_name}.",
            }

        self.job_id.write({
            "driver_id": target_driver.id,
            "vehicle_id": target_vehicle.id,
        })
        self.job_id.message_post(
            body=(
                f"Remaining route reassigned from {location_label} to "
                f"{target_driver.name} / {target_vehicle.display_name}."
            )
        )
        return {
            "success": True,
            "applied": True,
            "unassigned": False,
            "reassigned_vehicle_id": target_vehicle.id,
            "message": (
                f"Remaining route reassigned to {target_driver.name} / {target_vehicle.display_name}."
            ),
        }

    def action_assign_receiving_truck(self, vehicle_id=False, stage_unassigned=False):
        self.ensure_one()
        if self.stop_type not in ("transfer", "cross_dock_drop"):
            raise exceptions.UserError(
                "Only Driver Transfer and Cross-Dock Drop stops can use receiving-truck assignment."
            )

        vehicle = self.env["fleet.vehicle"]
        driver = self.env["res.partner"]
        if vehicle_id:
            vehicle = self.env["fleet.vehicle"].browse(int(vehicle_id))
            if not vehicle.exists():
                raise exceptions.UserError("The selected receiving truck no longer exists.")
            driver = vehicle.driver_id or vehicle.x_current_driver_contact_id
            if not driver:
                raise exceptions.UserError(
                    "Select a truck that already has a driver assigned."
                )

        self.write({
            "transfer_to_vehicle_id": vehicle.id if vehicle else False,
            "transfer_to_driver_id": driver.id if driver else False,
        })

        if self.status == "completed":
            return self._apply_receiving_truck_assignment(stage_unassigned=stage_unassigned)

        if stage_unassigned:
            return {
                "success": True,
                "applied": False,
                "unassigned": False,
                "reassigned_vehicle_id": False,
                "message": "The load will stay staged here until you finish the stop.",
            }

        return {
            "success": True,
            "applied": False,
            "unassigned": False,
            "reassigned_vehicle_id": vehicle.id if vehicle else False,
            "message": (
                f"Receiving truck saved: {vehicle.display_name}."
                if vehicle else
                "Receiving truck cleared."
            ),
        }

    def _transfer_saved_location(self):
        self.ensure_one()
        if self.saved_location_id:
            return self.saved_location_id
        if not self.address:
            return self.env["prema.dispatch.location"]
        location = self.env["prema.dispatch.location"].find_or_create_by_address(
            self.address,
            business_name=self.contact_name or (self.partner_id.name if self.partner_id else None),
            partner_id=self.partner_id.id if self.partner_id else None,
        )
        if location and not self.saved_location_id:
            self.saved_location_id = location.id
        return location

    def _cross_dock_origin_stop(self):
        self.ensure_one()
        if self.cross_dock_origin_stop_id:
            return self.cross_dock_origin_stop_id
        pickups = self.job_id.stop_ids.filtered(
            lambda s: s.stop_type == "pickup"
        ).sorted("sequence")
        return pickups[:1] if len(pickups) == 1 else self.env["prema.dispatch.stop"]

    def _items_for_custody_transition(self):
        self.ensure_one()
        if self.freight_item_ids:
            return self.freight_item_ids.filtered(
                lambda i: i.job_id.id == self.job_id.id and i.status not in ("delivered", "cancelled")
            )
        items = self.job_id.item_ids.filtered(
            lambda i: i.status not in ("delivered", "cancelled")
        )
        if self.stop_type == "pickup":
            return items.filtered(lambda i: i.pickup_stop_id.id == self.id)
        if self.stop_type in ("dropoff", "return"):
            # Include items linked via delivery_stop_id OR stop_allocation_ids (shared pallets)
            alloc_items = items.filtered(
                lambda i: i.stop_allocation_ids.filtered(
                    lambda a: a.active and a.stop_id.id == self.id
                )
            )
            direct = items.filtered(lambda i: i.delivery_stop_id.id == self.id)
            return (direct | alloc_items)
        if self.stop_type == "transfer":
            return items
        if self.stop_type in ("cross_dock_drop", "cross_dock_pickup"):
            origin = self._cross_dock_origin_stop()
            if not origin:
                return self.env["prema.dispatch.item"]
            items = items.filtered(lambda i: i.pickup_stop_id.id == origin.id)
            if self.stop_type == "cross_dock_drop":
                if self.job_id.vehicle_id:
                    matched = items.filtered(
                        lambda i: i.current_vehicle_id.id == self.job_id.vehicle_id.id
                    )
                    if matched:
                        items = matched
                return items
            if self.saved_location_id:
                located = items.filtered(
                    lambda i: i.current_location_id.id == self.saved_location_id.id
                )
                if located:
                    items = located
            staged = items.filtered(lambda i: i.current_custody_type == "cross_dock")
            return staged or items
        return self.env["prema.dispatch.item"]

    def _apply_item_custody_transition(self, items=None, log_events=True):
        """Move freight items through the existing item lifecycle while also
        keeping chain-of-custody fields current for cross-dock and transfer
        workflows."""
        self.ensure_one()
        items = items or self._items_for_custody_transition()
        if not items:
            return items

        vehicle = self.completed_vehicle_id or self.job_id.vehicle_id
        driver = self.completed_driver_id or self.job_id.driver_id
        location = self.saved_location_id
        location_name = (
            (location.business_name or location.name or location.address)
            if location else
            (self.address or "location")
        )

        if self.stop_type == "pickup":
            items.write({
                "status": "in_transit",
                "current_vehicle_id": vehicle.id if vehicle else False,
                "current_driver_id": driver.id if driver else False,
                "current_location_id": False,
                "current_custody_type": "truck",
            })
            if log_events:
                items._log_custody_event(
                    "loaded", stop=self, vehicle=vehicle, driver=driver,
                    notes=f"Picked up at {self.address or 'origin stop'}.",
                )
        elif self.stop_type == "cross_dock_drop":
            items.write({
                "status": "cross_docked",
                "current_vehicle_id": False,
                "current_driver_id": False,
                "current_location_id": location.id if location else False,
                "current_custody_type": "cross_dock",
            })
            if log_events:
                items._log_custody_event(
                    "cross_docked", stop=self, saved_location=location,
                    notes=f"Held at cross-dock {location_name}.",
                )
        elif self.stop_type == "cross_dock_pickup":
            items.write({
                "status": "in_transit",
                "current_vehicle_id": vehicle.id if vehicle else False,
                "current_driver_id": driver.id if driver else False,
                "current_location_id": False,
                "current_custody_type": "truck",
            })
            if log_events:
                items._log_custody_event(
                    "reloaded", stop=self, saved_location=location,
                    vehicle=vehicle, driver=driver,
                    notes=f"Reloaded from {location_name}.",
                )
        elif self.stop_type == "transfer":
            target_vehicle = self.transfer_to_vehicle_id
            target_driver = self.transfer_to_driver_id or (
                target_vehicle.driver_id or target_vehicle.x_current_driver_contact_id
                if target_vehicle else self.env["res.partner"]
            )
            if target_vehicle or target_driver:
                items.write({
                    "status": "in_transit",
                    "current_vehicle_id": target_vehicle.id if target_vehicle else False,
                    "current_driver_id": target_driver.id if target_driver else False,
                    "current_location_id": False,
                    "current_custody_type": "truck",
                })
                if log_events:
                    items._log_custody_event(
                        "transferred", stop=self, saved_location=location,
                        vehicle=target_vehicle, driver=target_driver,
                        notes=f"Transferred to another truck at {self.address or 'handoff point'}.",
                    )
            else:
                location = self._transfer_saved_location()
                location_name = (
                    (location.business_name or location.name or location.address)
                    if location else (self.address or "meet point")
                )
                items.write({
                    "status": "staged",
                    "current_vehicle_id": False,
                    "current_driver_id": False,
                    "current_location_id": location.id if location else False,
                    "current_custody_type": "location" if location else "pending",
                })
                if log_events:
                    items._log_custody_event(
                        "transferred", stop=self, saved_location=location,
                        notes=f"Transferred off truck and staged at {location_name}.",
                    )
        elif self.stop_type in ("dropoff", "return"):
            items.write({
                "status": "delivered",
                "current_vehicle_id": False,
                "current_driver_id": False,
                "current_location_id": False,
                "current_custody_type": "delivered",
            })
            if log_events:
                items._log_custody_event(
                    "delivered", stop=self, saved_location=location,
                    vehicle=vehicle, driver=driver,
                    notes=f"Delivered at {self.address or 'destination stop'}.",
                )
        return items

    def _advance_item_status(self):
        self.ensure_one()
        self._apply_item_custody_transition(log_events=True)

    def action_execute_transfer(self):
        """Complete a driver-to-driver relay handoff at this stop: reassigns
        the job's driver/truck for all remaining stops, and marks this
        transfer stop completed. Requires the receiving driver's custody
        photo (pod_attachment_ids) to already be uploaded — same evidence
        flow as a normal POD, called before this."""
        self.ensure_one()
        if self.stop_type != "transfer":
            raise exceptions.UserError("This is not a transfer stop.")
        self._check_completion_requirements()

        if self.transfer_to_vehicle_id and not self.transfer_to_driver_id:
            driver = self.transfer_to_vehicle_id.driver_id or self.transfer_to_vehicle_id.x_current_driver_contact_id
            if not driver:
                raise exceptions.UserError("Select a receiving driver before completing this transfer.")
            self.transfer_to_driver_id = driver

        prior_driver = self.job_id.driver_id
        prior_vehicle = self.job_id.vehicle_id
        self.write({
            "transfer_from_driver_id": prior_driver.id if prior_driver else False,
            "transfer_from_vehicle_id": prior_vehicle.id if prior_vehicle else False,
            "completed_driver_id": prior_driver.id if prior_driver else False,
            "completed_vehicle_id": prior_vehicle.id if prior_vehicle else False,
        })

        if not self.transfer_to_driver_id and not self.transfer_to_vehicle_id:
            self._transfer_saved_location()
            vals = {"driver_id": False, "vehicle_id": False}
        else:
            vals = {"driver_id": self.transfer_to_driver_id.id}
            if self.transfer_to_vehicle_id:
                vals["vehicle_id"] = self.transfer_to_vehicle_id.id

        self.job_id.write(vals)  # auto-logged to prema.dispatch.assignment.log / unassignment log
        self.action_mark_completed()
        self.job_id.message_post(
            body=(
                f"Load transferred to {self.transfer_to_driver_id.name} / "
                f"{self.transfer_to_vehicle_id.display_name} at {self.address or 'handoff point'}."
                if self.transfer_to_driver_id and self.transfer_to_vehicle_id else
                f"Load staged and unassigned at {self.address or 'handoff point'}."
            )
        )
        return {
            "success": True,
            "unassigned": not (self.transfer_to_driver_id or self.transfer_to_vehicle_id),
        }

    def action_restore_stop(self):
        """Undo an accidental driver/dispatcher status change while keeping
        freight custody consistent with the remaining completed stops."""
        self.ensure_one()
        if self.status == "pending":
            return {"success": True}
        later_done = self.job_id.stop_ids.filtered(
            lambda s: s.sequence > self.sequence and s.status == "completed"
        )
        if later_done:
            raise exceptions.UserError(
                "Restore later completed stops first so freight custody stays in order."
            )

        if self.stop_type in ("transfer", "cross_dock_drop") and (self.transfer_from_driver_id or self.transfer_from_vehicle_id):
            self.job_id.write({
                "driver_id": self.transfer_from_driver_id.id if self.transfer_from_driver_id else False,
                "vehicle_id": self.transfer_from_vehicle_id.id if self.transfer_from_vehicle_id else False,
            })

        self.write({
            "status": "pending",
            "actual_arrival_time": False,
            "actual_departure_time": False,
            "gps_stamp_lat": 0,
            "gps_stamp_lng": 0,
            "gps_stamp_time": False,
            "issue_reason": False,
            "completed_driver_id": False,
            "completed_vehicle_id": False,
        })
        self.job_id._rebuild_item_custody()
        self.job_id.message_post(
            body=f"Stop restored to Pending: {self.name or self.address or 'stop'}"
        )
        return {"success": True}

    def action_cancel_stop(self):
        """Mark stop as cancelled — never deletes, preserves audit trail."""
        self.write({"status": "cancelled"})
        self.job_id.message_post(
            body=f"Stop cancelled: {self.name} ({self.address or 'no address'})"
        )
        self.job_id._check_all_stops_cancelled()

    def action_report_issue(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Report Issue",
            "res_model": "prema.dispatch.stop",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ── Geocoding & real-time sync ─────────────────────────────────

    @api.model
    def _validate_cross_dock_stop_values(self, stop_type, saved_location_id):
        if stop_type not in ("cross_dock_drop", "cross_dock_pickup"):
            return
        if not saved_location_id:
            raise exceptions.UserError(
                "Cross-dock stops require a Saved Location with Cross-Dock enabled."
            )
        location = self.env["prema.dispatch.location"].browse(saved_location_id)
        if not location.exists() or not location.allow_cross_dock:
            raise exceptions.UserError(
                "Only Saved Locations with Cross-Dock enabled can use Cross-Dock stop actions."
            )

    def _validate_cross_dock_stop_transition(self, vals=None):
        vals = vals or {}
        for stop in self:
            stop_type = vals.get("stop_type", stop.stop_type)
            saved_location_id = vals.get("saved_location_id", stop.saved_location_id.id)
            self._validate_cross_dock_stop_values(stop_type, saved_location_id)

    def _validate_freight_item_links(self):
        for stop in self:
            wrong_job_items = stop.freight_item_ids.filtered(lambda i: i.job_id.id != stop.job_id.id)
            if wrong_job_items:
                raise exceptions.UserError(
                    "Stop freight items must belong to the same dispatch job."
                )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("stop_type") in ("cross_dock_drop", "cross_dock_pickup", "transfer") and "pod_required" not in vals:
                vals["pod_required"] = True
            self._validate_cross_dock_stop_values(
                vals.get("stop_type", "dropoff"),
                vals.get("saved_location_id"),
            )
        records = super().create(vals_list)
        records._validate_freight_item_links()
        records._sync_selected_item_pallet_counts()
        for rec in records:
            if rec.saved_location_id:
                rec._apply_saved_location(rec.saved_location_id)
            if rec.address and not (rec.latitude or rec.longitude):
                rec._geocode_address()
            if rec.address and not rec.address_validated:
                rec._validate_address()
        # Re-estimate every pickup on the affected job(s), not just pickups
        # in this batch — stops are often created one-by-one (AI extraction,
        # estimator promotion), so a dropoff created after its pickup must
        # still trigger that pickup's estimate to update.
        records.mapped("job_id").mapped("stop_ids").filtered(
            lambda s: s.stop_type == "pickup"
        )._estimate_pickup_pallets()
        records._notify_driver_route_changed()
        return records

    def write(self, vals):
        self._validate_cross_dock_stop_transition(vals)
        resulting_cross_dock = vals.get("stop_type") in ("cross_dock_drop", "cross_dock_pickup", "transfer") or (
            "saved_location_id" in vals and any(
                stop.stop_type in ("cross_dock_drop", "cross_dock_pickup", "transfer") for stop in self
            )
        )
        if resulting_cross_dock and "pod_required" not in vals:
            vals = dict(vals, pod_required=True)
        # A direct edit to pallets_in on a pickup stop is the dispatcher
        # overriding a downstream estimate — stop treating it as estimated,
        # unless this write IS the estimator itself (which sets both fields
        # together, see _estimate_pickup_pallets).
        if "pallets_in" in vals and "pallets_in_estimated" not in vals:
            vals["pallets_in_estimated"] = False

        result = super().write(vals)
        self._validate_freight_item_links()
        if "freight_item_ids" in vals or "stop_type" in vals:
            self._sync_selected_item_pallet_counts()
        if "address" in vals and vals["address"]:
            for rec in self:
                if not (rec.pin_set) or "address" in vals:
                    rec._geocode_address()
                rec._validate_address()

        # Re-estimate any pickup whose downstream deliveries just changed.
        if "pallets_out" in vals or "sequence" in vals or "stop_type" in vals:
            jobs = self.mapped("job_id")
            jobs.mapped("stop_ids").filtered(
                lambda s: s.stop_type == "pickup"
            )._estimate_pickup_pallets()

        # Item status: covers every path that completes/fails/cancels a stop
        # (form action, driver app RPC, etc.) in one place rather than
        # duplicating per call site.
        if vals.get("status") == "completed":
            for stop in self:
                stop._advance_item_status()
        elif vals.get("status") == "issue":
            for stop in self:
                stop.job_id.item_ids.filtered(
                    lambda i: (i.pickup_stop_id.id == stop.id or i.delivery_stop_id.id == stop.id)
                    and i.status not in ("delivered", "cancelled")
                ).write({"status": "failed"})
        elif vals.get("status") == "cancelled":
            for stop in self:
                stop.job_id.item_ids.filtered(
                    lambda i: i.pickup_stop_id.id == stop.id or i.delivery_stop_id.id == stop.id
                ).write({"status": "cancelled"})

        if any(k in vals for k in ("sequence", "status", "address", "scheduled_time",
                                    "pin_lat", "pin_lng", "pallets_in", "pallets_out", "shared_pallet_number")):
            self._notify_driver_route_changed()
        return result

    def _estimate_pickup_pallets(self):
        """If a pickup stop has no pallet count entered, estimate it from the
        downstream drop-offs that follow it (up to the next pickup or end of
        route) — e.g. Pickup Ajax (unknown) -> Drop Oshawa 12 -> Drop Whitby 1
        estimates 13 for the Ajax pickup. Dispatcher can always override by
        editing Pallets In directly (see write())."""
        for stop in self:
            if stop.stop_type != "pickup":
                continue
            if stop.pallets_in and not stop.pallets_in_estimated:
                continue  # dispatcher-entered value — never overwrite it
            siblings = stop.job_id.stop_ids.sorted("sequence")
            total = 0
            found_dropoff = False
            for sib in siblings:
                if sib.sequence <= stop.sequence:
                    continue
                if sib.stop_type == "pickup":
                    break
                if sib.stop_type in ("dropoff", "return") and sib.status != "cancelled":
                    total += sib.pallets_out or 0
                    found_dropoff = True
            if found_dropoff and total > 0 and total != stop.pallets_in:
                stop.write({"pallets_in": total, "pallets_in_estimated": True})
            elif found_dropoff and total == 0 and stop.pallets_in_estimated:
                stop.write({"pallets_in": 0, "pallets_in_estimated": True})

    def _lookup_timezone(self, lat, lng, api_key):
        """Resolve the IANA timezone name for a stop's coordinates via
        Google's Time Zone API, falling back to a longitude-band estimate
        if the call fails so the field is never left blank."""
        try:
            import time
            import requests
            r = requests.get(
                "https://maps.googleapis.com/maps/api/timezone/json",
                params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": api_key},
                timeout=5,
            )
            data = r.json()
            if data.get("status") == "OK" and data.get("timeZoneId"):
                return data["timeZoneId"]
            _logger.warning(
                "Time Zone API lookup failed for stop %s (%s,%s): %s",
                self.id, lat, lng, data.get("status"),
            )
        except Exception:
            _logger.exception("Time Zone API request failed for stop %s (%s,%s)", self.id, lat, lng)
        return tz_from_longitude_band(lat, lng)

    def _geocode_address(self, force=False):
        """Geocode address via Google and set lat/lng + default pin.

        force=True re-geocodes even if lat/lng are already set (used by the
        "Reset pin to address" action to undo a bad manual pin placement).
        Returns True on success, False otherwise (failures are logged, not
        swallowed, so a misconfigured/disabled Geocoding API is visible).
        """
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        if not api_key or not self.address:
            return False
        try:
            import requests
            r = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": self.address, "key": api_key},
                timeout=5,
            )
            data = r.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                vals = {"latitude": loc["lat"], "longitude": loc["lng"]}
                if force or not self.pin_set:
                    vals["pin_lat"] = loc["lat"]
                    vals["pin_lng"] = loc["lng"]
                    vals["pin_set"] = False
                vals["tz_name"] = self._lookup_timezone(loc["lat"], loc["lng"], api_key)
                self.sudo().write(vals)
                self._auto_link_saved_location()
                return True
            _logger.warning(
                "Geocoding failed for stop %s (%r): %s — %s",
                self.id, self.address, data.get("status"), data.get("error_message", ""),
            )
            return False
        except Exception:
            _logger.exception("Geocoding request failed for stop %s (%r)", self.id, self.address)
            return False

    def _auto_link_saved_location(self):
        """Link this stop to an existing saved location for the same address/
        business+customer if one exists, so pin/notes/photo are reused
        instead of every stop at the same warehouse starting from scratch.
        Only auto-links if not already linked — never overwrites a
        dispatcher's manual choice."""
        if self.saved_location_id or not self.address:
            return
        loc = self.env["prema.dispatch.location"].find_or_create_by_address(
            self.address,
            business_name=self.contact_name or (self.partner_id.name if self.partner_id else None),
            partner_id=self.partner_id.id if self.partner_id else None,
        )
        if loc:
            self.sudo().saved_location_id = loc.id

    def _validate_address(self):
        """Check address accuracy via Google's Address Validation API.

        Does NOT rewrite the dispatcher/AI-entered address — only flags it
        (address_validation_warning) and stores the standardized form
        (address_formatted) for reference, so dispatchers can spot typos or
        incomplete addresses before a driver is sent to the wrong place.
        """
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
                _logger.warning(
                    "Address validation failed for stop %s (%r): %s",
                    self.id, self.address, data.get("error", data),
                )
                return False
            verdict = result.get("verdict", {})
            warning = ""
            if not verdict.get("addressComplete", True):
                warning = "Address looks incomplete"
            elif verdict.get("hasUnconfirmedComponents"):
                warning = "Some address details could not be confirmed"
            elif verdict.get("hasReplacedComponents"):
                warning = "Address auto-corrected — please verify"
            self.sudo().write({
                "address_validated": True,
                "address_validation_warning": warning,
                "address_formatted": result.get("address", {}).get("formattedAddress", ""),
            })
            return True
        except Exception:
            _logger.exception("Address validation request failed for stop %s (%r)", self.id, self.address)
            return False

    def _notify_driver_route_changed(self):
        """Push a bus notification so the driver app knows to refresh."""
        try:
            for rec in self:
                if rec.job_id and rec.job_id.driver_id:
                    driver_partner_id = rec.job_id.driver_id.id
                    self.env["bus.bus"]._sendmany([[
                        f"driver_route_{driver_partner_id}",
                        "route_updated",
                        {"job_id": rec.job_id.id, "stop_id": rec.id},
                    ]])
        except Exception:
            pass

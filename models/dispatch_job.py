import logging
import re

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)

_FNAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-.]")
_PLAIN_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _safe_fname(text):
    return _FNAME_SAFE_RE.sub("_", (text or "").strip())


class PremaDispatchJob(models.Model):
    _name = "prema.dispatch.job"
    _description = "Prema Dispatch Job"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "scheduled_pickup asc, id desc"

    # ── Identity ──────────────────────────────────────────────────

    name = fields.Char(
        string="Job #", default="New", copy=False,
        readonly=True, tracking=True,
    )
    invoice_id = fields.Many2one(
        "account.move", string="Invoice",
        ondelete="set null", index=True, tracking=True,
    )
    partner_id = fields.Many2one(
        "res.partner", string="Customer", tracking=True
    )
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company
    )
    ref = fields.Char(string="Reference / BOL / PO", tracking=True)
    active = fields.Boolean(default=True)
    color = fields.Integer()

    # ── Source Document Links ─────────────────────────────────

    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order",
        ondelete="set null", index=True, tracking=True,
        help="Sales Order or Quotation this booking was created from.",
    )
    source_model = fields.Char(
        string="Source Model", readonly=True, copy=False,
        help="Internal: model of the source document (account.move or sale.order).",
    )
    source_res_id = fields.Integer(
        string="Source Record ID", readonly=True, copy=False, index=True,
    )
    source_document_name = fields.Char(
        string="Source Document", compute="_compute_source_doc",
        store=True, readonly=True,
        help="Reference number of the originating Quote, Sales Order, or Invoice.",
    )

    # ── Tracking ──────────────────────────────────────────────

    tracking_number = fields.Char(
        string="Tracking #", readonly=True, copy=False, index=True,
        help="Auto-generated when booking leaves Draft. Share with customer.",
    )
    tracking_url = fields.Char(
        string="Tracking URL", compute="_compute_tracking_url", readonly=True,
    )
    booking_confirmed_at = fields.Datetime(
        string="Booking Confirmed At", readonly=True, copy=False,
    )
    template_id = fields.Many2one(
        "prema.dispatch.booking.template", string="Booking Template",
        ondelete="set null", readonly=True, copy=False, index=True,
        help="Recurring template that auto-generated this booking.",
    )

    # ── Stage / Status ────────────────────────────────────────────

    stage_id = fields.Many2one(
        "prema.dispatch.stage", string="Stage",
        default=lambda self: self._default_stage(),
        group_expand="_read_group_stage_ids",
        tracking=True, index=True,
    )
    priority = fields.Selection([
        ("normal",    "Normal"),
        ("urgent",    "Urgent"),
        ("emergency", "Emergency"),
    ], default="normal", tracking=True)

    # ── Service Details ───────────────────────────────────────────

    service_type = fields.Selection([
        ("local",     "Local"),
        ("ltl",       "LTL"),
        ("ftl",       "FTL"),
        ("dedicated", "Dedicated"),
        ("other",     "Other"),
    ], default="ltl")
    equipment_type = fields.Selection([
        ("dry",      "Dry Van"),
        ("reefer",   "Reefer"),
        ("flatbed",  "Flatbed"),
        ("other",    "Other"),
    ], default="dry")

    # ── Booking Details ───────────────────────────────────────

    delivery_flexibility = fields.Selection([
        ("rush",      "Rush — Same Day"),
        ("next_day",  "Next Business Day"),
        ("specific",  "Specific Date Required"),
        ("this_week", "This Week"),
        ("flexible",  "Flexible — Best Price"),
        ("economy",   "Economy — No Rush"),
    ], string="Delivery Flexibility", tracking=True,
        help="Controls whether AI can find a cheaper delivery day on an existing route.",
    )
    requested_delivery_date = fields.Date(
        string="Customer Requested Date", tracking=True,
        help="Date the customer needs delivery. Used for priority planning.",
    )
    planned_delivery_date = fields.Date(
        string="Planned Delivery Date", tracking=True,
        help="Date dispatcher has committed to. Set after planning.",
    )
    requires_liftgate = fields.Boolean(
        string="Liftgate Required", tracking=True,
        help="Liftgate needed — location has no loading dock.",
    )
    requires_reefer = fields.Boolean(
        string="Reefer Required", tracking=True,
        help="Temperature-controlled truck required for this load.",
    )
    approximate_skids = fields.Integer(
        string="Approx. Skids",
        help="Skid count at booking time (estimate). Exact count comes from freight items.",
    )
    commodity = fields.Char(
        string="Commodity",
        help="Description of the freight being shipped.",
    )
    temp_requirement = fields.Char(
        string="Temp. Requirement",
        help="e.g. 2°C – 8°C. Only applies when Reefer Required is checked.",
    )
    bol_number = fields.Char(string="BOL #", tracking=True)
    po_number = fields.Char(string="PO #", tracking=True)


    # ── Stops Pending / Planned Route Intent ─────────────────────────

    route_definition_mode = fields.Selection([
        ("exact_stops", "Exact Stops Known"),
        ("stops_pending", "Stops Pending"),
    ], default="exact_stops", required=True, tracking=True)
    planned_route_name = fields.Char(string="Planned Route Name", tracking=True)
    planned_route_corridor = fields.Selection([
        ("EAST", "East"), ("WEST", "West"), ("NORTH", "North"),
        ("SOUTH", "South"), ("LOCAL", "Local / GTA"), ("CUSTOM", "Custom"),
    ], string="Planned Corridor", tracking=True)
    planned_delivery_area = fields.Char(string="Planned Delivery Area")
    stops_confirmation_state = fields.Selection([
        ("pending", "Stops Pending"), ("partial", "Partially Entered"),
        ("confirmed", "Stops Confirmed"),
    ], default="confirmed", required=True, tracking=True)
    pickup_saved_location_id = fields.Many2one("prema.dispatch.location", string="Pickup Saved Location")
    reserve_capacity = fields.Boolean(default=False)
    route_sheet_received_at = fields.Datetime(readonly=True, copy=False)
    route_sheet_received_by = fields.Many2one("res.users", readonly=True, copy=False)
    computed_route_corridor = fields.Selection([
        ("EAST", "East"), ("WEST", "West"), ("NORTH", "North"),
        ("SOUTH", "South"), ("LOCAL", "Local / GTA"), ("CUSTOM", "Custom"),
    ], compute="_compute_route_corridors", store=True)
    effective_route_corridor = fields.Selection([
        ("EAST", "East"), ("WEST", "West"), ("NORTH", "North"),
        ("SOUTH", "South"), ("LOCAL", "Local / GTA"), ("CUSTOM", "Custom"),
    ], compute="_compute_route_corridors", store=True)
    corridor_mismatch_warning = fields.Char(compute="_compute_route_corridors", store=True)

    # ── LTL / Map Display (computed, stored for fast queries) ─

    pickup_city = fields.Char(
        string="Pickup City", compute="_compute_cities", store=True,
        help="Extracted from first pickup stop. Used for LTL consolidation matching.",
    )
    delivery_cities = fields.Char(
        string="Delivery Cities", compute="_compute_cities", store=True,
        help="Comma-separated delivery cities. Used for LTL consolidation matching.",
    )

    # ── Time Windows ──────────────────────────────────────────────

    pickup_window_type = fields.Selection([
        ("flexible", "Flexible — Any Time"),
        ("window",   "Time Window"),
        ("exact",    "Exact Appointment"),
    ], default="flexible", string="Pickup Window", tracking=True)
    pickup_earliest = fields.Datetime(string="Pickup Earliest", tracking=True)
    pickup_latest = fields.Datetime(string="Pickup Latest", tracking=True)
    pickup_exact_time = fields.Datetime(string="Pickup Appointment", tracking=True)

    delivery_window_type = fields.Selection([
        ("flexible",  "Flexible — No Deadline"),
        ("deadline",  "By Deadline"),
        ("window",    "Time Window"),
        ("exact",     "Exact Appointment"),
    ], default="flexible", string="Delivery Window", tracking=True)
    delivery_earliest = fields.Datetime(string="Delivery Earliest")
    delivery_latest = fields.Datetime(string="Delivery Latest")
    delivery_deadline = fields.Datetime(string="Delivery Deadline", tracking=True,
        help="Must be delivered by this time.")
    delivery_exact_time = fields.Datetime(string="Delivery Appointment")
    hard_deadline = fields.Boolean(string="Hard Deadline", tracking=True,
        help="Missing this deadline is not acceptable — system will block assignment if ETA cannot meet it.")
    appointment_required = fields.Boolean(string="Appointment Required", tracking=True,
        help="Pickup or delivery requires a scheduled appointment.")

    # ── Route Estimates ────────────────────────────────────────────

    estimated_duration_minutes = fields.Integer(
        string="Est. Duration (min)", readonly=True,
        help="Total estimated route duration from first pickup to last drop-off.",
    )
    estimated_distance_km = fields.Float(
        string="Est. Distance (km)", readonly=True, digits=(10, 1),
        help="Total estimated route distance (Google Maps).",
    )
    estimated_cost = fields.Float(
        string="Est. Cost ($)", digits=(10, 2),
        help="Estimated cost for this job (fuel + driver time). Manual or AI-computed.",
    )
    route_estimated_at = fields.Datetime(
        string="Route Estimated At", readonly=True,
        help="When the Google Maps route estimate was last computed.",
    )

    # ── Feasibility / Risk ─────────────────────────────────────────

    feasibility_status = fields.Selection([
        ("unknown",      "Not Checked"),
        ("feasible",     "Feasible"),
        ("risky",        "Risky"),
        ("not_feasible", "Not Feasible"),
    ], default="unknown", string="Feasibility", readonly=True, tracking=True)
    feasibility_notes = fields.Text(string="Feasibility Notes", readonly=True)
    recommended_truck_id = fields.Many2one(
        "fleet.vehicle", string="Recommended Truck",
        ondelete="set null", readonly=True,
        help="Best available truck suggested by the dispatch planner.",
    )
    risk_level = fields.Selection([
        ("green",  "Green — Safe"),
        ("yellow", "Yellow — Risky"),
        ("red",    "Red — Not Feasible"),
    ], compute="_compute_risk_level", store=True, string="Risk Level")
    optimization_score = fields.Float(
        string="Optimization Score", digits=(5, 2), readonly=True,
        help="Route efficiency score (0–100). Higher = better fit for this truck/day.",
    )

    # ── Smart Operational Flags ────────────────────────────────────

    is_night_departure = fields.Boolean(
        string="Night Departure",
        compute="_compute_smart_flags", store=True,
        help="Pickup scheduled before 5:00 AM or after 10:00 PM.",
    )
    overnight_hold = fields.Boolean(
        string="Overnight Hold",
        compute="_compute_smart_flags", store=True,
        help="Pickup and final delivery span different calendar dates — freight held overnight.",
    )
    latest_safe_dispatch_time = fields.Datetime(
        string="Latest Safe Dispatch",
        compute="_compute_smart_flags", store=True,
        help="Latest time to dispatch this truck to meet the delivery deadline (deadline minus estimated route duration).",
    )
    missing_pallet_warning = fields.Boolean(
        string="Missing Pallet Data",
        compute="_compute_smart_flags", store=True,
        help="No pallet count on job or stops — capacity cannot be validated.",
    )
    corridor_tag = fields.Char(
        string="Corridor",
        compute="_compute_corridor_tag", store=True,
        help="Primary travel direction (EAST/WEST/NORTH/SOUTH/LOCAL). Used for LTL corridor consolidation matching.",
    )

    # ── Assignment Override ────────────────────────────────────

    assignment_override_reason = fields.Text(
        string="Override Reason",
        help="Required when a manager overrides a truck compatibility block.",
    )
    assignment_override_by = fields.Many2one(
        "res.users", string="Override By", readonly=True,
    )
    assignment_override_at = fields.Datetime(
        string="Override At", readonly=True,
    )
    assignment_warnings = fields.Text(
        string="Assignment Warnings", readonly=True,
        help="Soft warnings logged at time of truck assignment.",
    )

    # ── Assignment ────────────────────────────────────────────────

    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Truck",
        ondelete="set null", tracking=True,
    )
    driver_id = fields.Many2one(
        "res.partner", string="Driver",
        ondelete="set null", tracking=True,
    )
    dispatcher_id = fields.Many2one(
        "res.users", string="Dispatcher",
        default=lambda self: self.env.user,
    )
    assignment_locked = fields.Boolean(
        string="Assignment Locked",
        help="Lock truck/driver assignment to prevent accidental changes.",
    )
    sent_to_driver_at = fields.Datetime(readonly=True)
    assignment_log_ids = fields.One2many(
        "prema.dispatch.assignment.log", "job_id", string="Assignment History"
    )

    # ── Schedule ─────────────────────────────────────────────────

    scheduled_pickup = fields.Datetime(
        string="Pickup Date / Time", tracking=True,
        default=lambda self: self._default_scheduled_pickup(),
    )
    scheduled_delivery = fields.Datetime(
        string="Delivery Date / Time", compute="_compute_scheduled_delivery", store=True,
        help="Derived from the Delivery Window fields (Exact Appointment / Deadline / "
             "Time Window) so there's no separate date to keep in sync by hand.",
    )

    @api.depends("delivery_window_type", "delivery_exact_time", "delivery_deadline",
                 "delivery_latest", "delivery_earliest")
    def _compute_scheduled_delivery(self):
        for job in self:
            if job.delivery_window_type == "exact" and job.delivery_exact_time:
                job.scheduled_delivery = job.delivery_exact_time
            elif job.delivery_window_type == "deadline" and job.delivery_deadline:
                job.scheduled_delivery = job.delivery_deadline
            elif job.delivery_window_type == "window" and (job.delivery_latest or job.delivery_earliest):
                job.scheduled_delivery = job.delivery_latest or job.delivery_earliest
            else:
                job.scheduled_delivery = False

    # ── Stops & Items ─────────────────────────────────────────────

    stop_ids = fields.One2many("prema.dispatch.stop", "job_id", string="Stops")
    item_ids = fields.One2many("prema.dispatch.item", "job_id", string="Freight Items")

    stop_count = fields.Integer(compute="_compute_stop_count")
    total_skids = fields.Integer(
        string="Total Skids", compute="_compute_load_totals", store=True
    )
    total_weight_lbs = fields.Float(
        string="Total Weight (lbs)", compute="_compute_load_totals",
        store=True, digits=(10, 1),
    )
    max_onboard_pallets = fields.Integer(
        string="Max Onboard Pallets",
        compute="_compute_max_onboard", store=True, readonly=True,
        help="Peak pallet count on the truck at any single point in the route. "
             "Used for capacity validation instead of total job pallets.",
    )
    all_stops_completed = fields.Boolean(
        compute="_compute_completion", store=True
    )
    pod_complete = fields.Boolean(
        compute="_compute_completion", store=True,
        string="All POD Received",
    )
    completed_at = fields.Datetime(readonly=True)

    # ── Invoice completion ────────────────────────────────────────

    auto_posted_invoice = fields.Boolean(readonly=True)
    auto_post_error = fields.Text(readonly=True)

    # ── Timeline ──────────────────────────────────────────────────

    timeline_event_ids = fields.One2many(
        "prema.dispatch.timeline.event", "job_id", string="Timeline Events",
    )

    # ── GeoTab / Live vehicle data (read from fleet.vehicle) ──────

    vehicle_last_lat = fields.Float(
        related="vehicle_id.x_last_location_lat", readonly=True, digits=(10, 6)
    )
    vehicle_last_lng = fields.Float(
        related="vehicle_id.x_last_location_lng", readonly=True, digits=(10, 6)
    )
    vehicle_last_gps_at = fields.Datetime(
        related="vehicle_id.x_last_location_at", readonly=True
    )
    vehicle_last_address = fields.Char(
        related="vehicle_id.x_last_location_address", readonly=True
    )
    # Speed and motion are fetched on demand (not stored on fleet.vehicle)
    vehicle_speed_kmh = fields.Float(
        string="Speed (km/h)", readonly=True, digits=(6, 1)
    )
    vehicle_moving_state = fields.Selection([
        ("moving",  "Moving"),
        ("stopped", "Stopped"),
        ("unknown", "Unknown"),
    ], readonly=True, default="unknown")
    vehicle_gps_refreshed_at = fields.Datetime(
        string="GPS Refreshed At", readonly=True
    )

    current_stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Current Stop",
        compute="_compute_current_next_stop",
    )
    next_stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Next Stop",
        compute="_compute_current_next_stop",
    )

    # ── Notes ─────────────────────────────────────────────────────

    internal_notes = fields.Text()
    driver_instructions = fields.Text()

    # ── Computed ──────────────────────────────────────────────────

    def _compute_tracking_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", default=""
        )
        for job in self:
            if job.tracking_number:
                job.tracking_url = f"{base}/dispatch/track/{job.tracking_number}"
            else:
                job.tracking_url = False

    @api.depends("invoice_id", "sale_order_id", "template_id")
    def _compute_source_doc(self):
        for job in self:
            if job.invoice_id:
                job.source_document_name = job.invoice_id.name or job.invoice_id.ref
            elif job.sale_order_id:
                job.source_document_name = job.sale_order_id.name
            elif job.template_id:
                job.source_document_name = f"Template: {job.template_id.name}"
            else:
                job.source_document_name = False


    @api.depends("planned_route_corridor", "stops_confirmation_state", "delivery_cities")
    def _compute_route_corridors(self):
        for job in self:
            cities = (job.delivery_cities or "").upper()
            computed = False
            if any(x in cities for x in ("OTTAWA", "BELLEVILLE", "KINGSTON", "COBOURG", "PETERBOROUGH", "PICTON", "MANOTICK", "MONTREAL")):
                computed = "EAST"
            elif any(x in cities for x in ("LONDON", "WINDSOR", "KITCHENER", "WATERLOO", "GUELPH")):
                computed = "WEST"
            elif any(x in cities for x in ("BARRIE", "ORILLIA", "SUDBURY", "NEWMARKET")):
                computed = "NORTH"
            elif any(x in cities for x in ("NIAGARA", "HAMILTON", "ST CATHARINES")):
                computed = "SOUTH"
            elif cities:
                computed = "CUSTOM"
            job.computed_route_corridor = computed
            if job.stops_confirmation_state in ("pending", "partial"):
                job.effective_route_corridor = job.planned_route_corridor or computed
            else:
                job.effective_route_corridor = computed or job.planned_route_corridor
            if job.planned_route_corridor and computed and job.planned_route_corridor != computed:
                job.corridor_mismatch_warning = "Planned corridor %s differs from confirmed route corridor %s." % (job.planned_route_corridor, computed)
            else:
                job.corridor_mismatch_warning = False

    def _driver_job_summary(self):
        self.ensure_one()
        link = self.env["prema.dispatch.load.plan.job"].search([("job_id", "=", self.id), ("active", "=", True)], limit=1)
        pickup = self.stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        return {
            "job_id": self.id, "job_name": self.name, "customer": self.partner_id.name if self.partner_id else "",
            "planned_route_name": self.planned_route_name or "", "planned_route_corridor": self.planned_route_corridor or "",
            "computed_route_corridor": self.computed_route_corridor or "", "effective_route_corridor": self.effective_route_corridor or "",
            "route_definition_mode": self.route_definition_mode, "stops_confirmation_state": self.stops_confirmation_state,
            "expected_skids": self.approximate_skids or 0,
            "reserved_positions": link.reserved_floor_positions if link else (self.approximate_skids if self.reserve_capacity else 0),
            "confirmed_skids": len(self.item_ids.filtered(lambda i: i.status != "cancelled" and i.consumes_floor_position)) if hasattr(self, "item_ids") else 0,
            "pickup_location": {"id": pickup.saved_location_id.id if pickup and pickup.saved_location_id else False, "address": pickup.address if pickup else ""},
            "route_sheet_received_at": self._dt_iso_utc(self.route_sheet_received_at),
            "vehicle": {"id": self.vehicle_id.id, "name": self.vehicle_id.name} if self.vehicle_id else False,
            "driver": {"id": self.driver_id.id, "name": self.driver_id.name} if self.driver_id else False,
            "stage": self.stage_id.name if self.stage_id else "", "load_plan_id": link.load_plan_id.id if link else False,
        }

    @api.depends("stop_ids.stop_type", "stop_ids.address", "stop_ids.sequence")
    def _compute_cities(self):
        for job in self:
            ordered = job.stop_ids.sorted("sequence")
            pickup_stops = ordered.filtered(lambda s: s.stop_type == "pickup")
            delivery_stops = ordered.filtered(lambda s: s.stop_type == "dropoff")

            def _city(addr):
                if not addr:
                    return ""
                parts = [p.strip() for p in addr.split(",")]
                return parts[-2] if len(parts) >= 3 else (parts[0] if parts else "")

            job.pickup_city = _city(pickup_stops[0].address) if pickup_stops else ""

            seen, cities = set(), []
            for s in delivery_stops:
                c = _city(s.address)
                if c and c not in seen:
                    cities.append(c)
                    seen.add(c)
            job.delivery_cities = ", ".join(cities)

    @api.depends(
        "scheduled_pickup",
        "stop_ids.stop_type", "stop_ids.scheduled_time", "stop_ids.sequence",
        "delivery_deadline", "estimated_duration_minutes",
        "max_onboard_pallets", "approximate_skids",
        "stage_id.stage_type",
    )
    def _compute_smart_flags(self):
        from datetime import timedelta
        for job in self:
            # is_night_departure
            if job.scheduled_pickup:
                h = job.scheduled_pickup.hour
                job.is_night_departure = h < 5 or h >= 22
            else:
                job.is_night_departure = False

            # overnight_hold — pickup date differs from last delivery date
            delivery_stops = job.stop_ids.filtered(
                lambda s: s.stop_type in ("dropoff", "return") and s.scheduled_time
            ).sorted("sequence")
            if job.scheduled_pickup and delivery_stops:
                job.overnight_hold = (
                    delivery_stops[-1].scheduled_time.date() > job.scheduled_pickup.date()
                )
            else:
                job.overnight_hold = False

            # latest_safe_dispatch_time — deadline minus estimated duration with buffer
            if job.delivery_deadline and job.estimated_duration_minutes:
                buffer = job.estimated_duration_minutes + 30
                job.latest_safe_dispatch_time = (
                    job.delivery_deadline - timedelta(minutes=buffer)
                )
            else:
                job.latest_safe_dispatch_time = False

            # missing_pallet_warning
            is_draft = (job.stage_id.stage_type == "draft") if job.stage_id else True
            job.missing_pallet_warning = (
                not is_draft
                and job.max_onboard_pallets == 0
                and job.approximate_skids == 0
            )

    @api.depends(
        "stop_ids.stop_type", "stop_ids.latitude", "stop_ids.longitude", "stop_ids.sequence"
    )
    def _compute_corridor_tag(self):
        for job in self:
            pickups = job.stop_ids.filtered(
                lambda s: s.stop_type == "pickup" and s.latitude and s.longitude
            ).sorted("sequence")
            deliveries = job.stop_ids.filtered(
                lambda s: s.stop_type in ("dropoff", "return") and s.latitude and s.longitude
            ).sorted("sequence")
            if not pickups or not deliveries:
                job.corridor_tag = ""
                continue
            p = pickups[0]
            d = deliveries[-1]
            lat_diff = d.latitude - p.latitude
            lng_diff = d.longitude - p.longitude
            if abs(lng_diff) < 0.4 and abs(lat_diff) < 0.4:
                job.corridor_tag = "LOCAL"
            elif abs(lng_diff) >= abs(lat_diff):
                job.corridor_tag = "EAST" if lng_diff > 0 else "WEST"
            else:
                job.corridor_tag = "NORTH" if lat_diff > 0 else "SOUTH"

    # ── Sequence ──────────────────────────────────────────────────

    @staticmethod
    def _normalize_temp(val):
        """Append ' °C' when user types a bare number like '10' or '-18'."""
        if val and _PLAIN_NUMBER_RE.match(val.strip()):
            return val.strip() + " °C"
        return val

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "prema.dispatch.job"
                ) or "New"
            if vals.get("vehicle_id") and not vals.get("driver_id"):
                vehicle = self.env["fleet.vehicle"].browse(vals["vehicle_id"])
                driver = (
                    vehicle.driver_id
                    or vehicle.x_current_driver_contact_id
                )
                if driver:
                    vals["driver_id"] = driver.id

            # Default requested_delivery_date from source document or today
            if "requested_delivery_date" not in vals or not vals.get("requested_delivery_date"):
                if vals.get("invoice_id"):
                    inv = self.env["account.move"].browse(vals["invoice_id"])
                    vals["requested_delivery_date"] = inv.invoice_date or fields.Date.today()
                elif vals.get("sale_order_id"):
                    so = self.env["sale.order"].browse(vals["sale_order_id"])
                    vals["requested_delivery_date"] = (
                        so.date_order.date() if so.date_order else fields.Date.today()
                    )
                else:
                    vals["requested_delivery_date"] = fields.Date.today()

            # Normalize bare-number temperature to include °C
            if vals.get("temp_requirement"):
                vals["temp_requirement"] = self._normalize_temp(vals["temp_requirement"])

        records = super().create(vals_list)
        for job in records:
            self._post_timeline(job, "booking_created")
            if job.source_model and job.source_model != "prema.dispatch.booking.template":
                self._post_timeline(
                    job, "imported_from",
                    notes=f"Imported from {job.source_document_name or job.source_model}",
                )
            elif job.template_id:
                self._post_timeline(
                    job, "imported_from",
                    notes=f"Auto-generated from template: {job.template_id.name}",
                )
        return records

    # ── Defaults ─────────────────────────────────────────────────

    def _default_stage(self):
        return self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )

    def _default_scheduled_pickup(self):
        """Pickup Date defaults to today (dispatcher's local date) — the field
        is shown with widget="date" (no time picker); a specific time is only
        entered via Pickup Window / Exact Appointment when actually needed."""
        import pytz
        from datetime import time as dtime
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        return self._local_date_time_to_utc(self._user_today(user_tz), dtime.min, user_tz)

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        return stages.search([], order="sequence asc")

    # ── Onchange ─────────────────────────────────────────────────

    @api.onchange("vehicle_id")
    def _onchange_vehicle_id(self):
        if self.vehicle_id and not self.assignment_locked:
            driver = (
                self.vehicle_id.driver_id
                or self.vehicle_id.x_current_driver_contact_id
            )
            self.driver_id = driver or False

    @api.onchange("equipment_type")
    def _onchange_equipment_type(self):
        # Equipment Type and "Reefer Required" used to be two separate
        # controls on the same form both representing "this load needs a
        # reefer" — a dispatcher could pick Reefer here but leave the
        # checkbox off (or vice versa), and every matching/feasibility
        # check reads only requires_reefer (see feasibility_service.py,
        # dispatch_consolidation.py, optimization_service.py). Deriving it
        # here keeps a single user-facing control (this dropdown) while
        # leaving requires_reefer as the one field the backend logic uses.
        self.requires_reefer = self.equipment_type == "reefer"

    @api.onchange("pickup_window_type")
    def _onchange_pickup_window_type(self):
        # Time Window / Exact Appointment only exist to add a TIME on top of
        # the Pickup Date already chosen above — the dispatcher shouldn't
        # have to re-pick the date a second time, so both default onto
        # Pickup Date's calendar day and only the time is left for them to set.
        import pytz
        from datetime import time as dtime
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        base_date = (
            self._local_date_of(self.scheduled_pickup, user_tz)
            or self._user_today(user_tz)
        )
        if self.pickup_window_type == "window":
            if not self.pickup_earliest:
                self.pickup_earliest = self._local_date_time_to_utc(base_date, dtime.min, user_tz)
            if not self.pickup_latest:
                latest_date = self._local_date_of(self.pickup_earliest, user_tz) or base_date
                self.pickup_latest = self._local_date_time_to_utc(latest_date, dtime.min, user_tz)
        elif self.pickup_window_type == "exact" and not self.pickup_exact_time:
            self.pickup_exact_time = self._local_date_time_to_utc(base_date, dtime.min, user_tz)

    @api.onchange("pickup_earliest")
    def _onchange_pickup_earliest(self):
        # Pickup Latest brackets the same same-day window as Pickup Earliest
        # (e.g. 11:30-13:00) — keep it pinned to Earliest's date by default,
        # only its time is left for the dispatcher to change.
        if self.pickup_window_type != "window" or not self.pickup_earliest:
            return
        import pytz
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        earliest_date = self._local_date_of(self.pickup_earliest, user_tz)
        if not self.pickup_latest:
            from datetime import time as dtime
            self.pickup_latest = self._local_date_time_to_utc(earliest_date, dtime.min, user_tz)
            return
        latest_date = self._local_date_of(self.pickup_latest, user_tz)
        if latest_date != earliest_date:
            latest_local_time = pytz.utc.localize(self.pickup_latest).astimezone(user_tz).time()
            self.pickup_latest = self._local_date_time_to_utc(earliest_date, latest_local_time, user_tz)

    # ── Computed ─────────────────────────────────────────────────

    @api.depends("stop_ids")
    def _compute_stop_count(self):
        for job in self:
            job.stop_count = len(job.stop_ids)

    @api.depends("item_ids.pallet_count", "item_ids.weight_lbs")
    def _compute_load_totals(self):
        for job in self:
            job.total_skids = sum(i.pallet_count for i in job.item_ids)
            job.total_weight_lbs = sum(i.weight_lbs for i in job.item_ids)

    def _rebuild_item_custody(self):
        """Recompute every freight item's current custody from the job's
        completed stops. Used when a dispatcher/driver restores a stop after
        an accidental completion so pallet custody and transferred evidence
        stay aligned with the remaining completed route."""
        for job in self:
            items = job.item_ids
            if not items:
                continue
            items.mapped("custody_event_ids").sudo().unlink()
            items.write({
                "status": "pending",
                "current_vehicle_id": False,
                "current_driver_id": False,
                "current_location_id": False,
                "current_custody_type": "pending",
            })
            for stop in job.stop_ids.filtered(lambda s: s.status == "completed").sorted("sequence"):
                stop._apply_item_custody_transition(log_events=True)

    @api.depends(
        "stop_ids.pallets_in", "stop_ids.pallets_out",
        "stop_ids.sequence", "stop_ids.stop_type",
        "approximate_skids",
    )
    def _compute_max_onboard(self):
        for job in self:
            stops = list(job.stop_ids.sorted("sequence"))
            if not stops or not any(
                s.pallets_in > 0 or s.pallets_out > 0 for s in stops
            ):
                # No pallet data on stops — fall back to booking estimate
                job.max_onboard_pallets = job.approximate_skids or 0
                continue

            def _effective_pickup(idx):
                s = stops[idx]
                if s.stop_type == "cross_dock_pickup":
                    return s.pallets_in
                if s.stop_type != "pickup":
                    return 0
                if s.pallets_in > 0:
                    return s.pallets_in
                # Infer from drop-offs before next pickup
                next_pickup = next(
                    (j for j in range(idx + 1, len(stops))
                     if stops[j].stop_type == "pickup"),
                    len(stops),
                )
                return sum(stops[j].pallets_out for j in range(idx + 1, next_pickup))

            def _effective_delivery(idx, current_running):
                """How many pallets were unloaded at this stop.

                When pallets_out is explicitly set, use it.
                When pallets_out = 0 (not filled in) on a dropoff:
                  - If there is a future pickup stop, the driver cleared the
                    truck to make room — assume full delivery (reset to 0).
                  - If this is the last stop or no future pickup, also treat
                    as full delivery.
                This prevents the scenario where an unfilled delivery stop
                causes the next pickup to stack on top of an unchanged running
                count, inflating the computed peak (e.g. DISP 00012 pattern:
                pickup 12 → delivery → return-to-warehouse pickup 5 → wrongly
                computed as peak 17 instead of 12).
                """
                s = stops[idx]
                if s.stop_type not in ("dropoff", "return", "cross_dock_drop", "transfer"):
                    return 0
                if s.pallets_out > 0:
                    return s.pallets_out
                # pallets_out not recorded — infer full delivery
                return current_running

            running, peak = 0, 0
            for i, s in enumerate(stops):
                if s.stop_type in ("pickup", "cross_dock_pickup"):
                    running += _effective_pickup(i)
                elif s.stop_type in ("dropoff", "return", "cross_dock_drop", "transfer"):
                    running = max(0, running - _effective_delivery(i, running))
                if running > peak:
                    peak = running
            job.max_onboard_pallets = peak

    @api.depends(
        "requires_reefer", "requires_liftgate",
        "vehicle_id", "vehicle_id.x_reefer", "vehicle_id.x_liftgate",
        "hard_deadline", "delivery_deadline",
        "max_onboard_pallets", "assignment_warnings",
        "stop_ids.hard_deadline", "stop_ids.deadline_time", "stop_ids.estimated_arrival",
        "stage_id", "stage_id.stage_type",
    )
    def _compute_risk_level(self):
        for job in self:
            blocks = []
            warnings = []

            # Equipment mismatch — hard block (RED)
            if job.vehicle_id:
                if job.requires_reefer and not job.vehicle_id.x_reefer:
                    blocks.append("Reefer required but truck has no reefer.")
                if job.requires_liftgate and not job.vehicle_id.x_liftgate:
                    blocks.append("Liftgate required but truck has no liftgate.")

            # Capacity warning → YELLOW
            if job.assignment_warnings:
                warnings.append("Capacity warning on truck assignment.")

            # Hard deadline with no ETA computed → YELLOW
            hard_stops = job.stop_ids.filtered(
                lambda s: s.hard_deadline and s.deadline_time and not s.estimated_arrival
            )
            if hard_stops:
                warnings.append(
                    f"{len(hard_stops)} stop(s) have hard deadlines with no ETA computed yet."
                )

            # Hard deadline + ETA exists → check if deadline missed → RED
            overdue_stops = job.stop_ids.filtered(
                lambda s: s.hard_deadline
                and s.deadline_time
                and s.estimated_arrival
                and s.estimated_arrival > s.deadline_time
            )
            if overdue_stops:
                blocks.append(
                    f"{len(overdue_stops)} stop(s) ETA exceeds hard deadline."
                )

            # Job-level hard deadline check
            if job.hard_deadline and job.delivery_deadline:
                last_stop = job.stop_ids.sorted("sequence")
                last_stop = last_stop[-1:] if last_stop else None
                if last_stop and last_stop.estimated_departure:
                    if last_stop.estimated_departure > job.delivery_deadline:
                        blocks.append(
                            "Estimated delivery time exceeds job hard deadline."
                        )

            # No truck assigned on active job → YELLOW
            if (job.stage_id
                    and job.stage_id.stage_type in ("booking", "dispatched")
                    and not job.vehicle_id):
                warnings.append("No truck assigned.")

            if blocks:
                job.risk_level = "red"
            elif warnings:
                job.risk_level = "yellow"
            else:
                job.risk_level = "green"

    @api.depends(
        "stop_ids.status",
        "stop_ids.pod_required",
        "stop_ids.pod_uploaded",
    )
    def _compute_completion(self):
        for job in self:
            stops = job.stop_ids
            if not stops:
                job.all_stops_completed = False
                job.pod_complete = False
                continue
            job.all_stops_completed = all(
                s.status in ("completed", "skipped") for s in stops
            )
            required_pod = stops.filtered("pod_required")
            job.pod_complete = (
                all(s.pod_uploaded for s in required_pod)
                if required_pod
                else True
            )

    @api.depends("stop_ids.status")
    def _compute_current_next_stop(self):
        for job in self:
            ordered = job.stop_ids.sorted("sequence")
            current = next(
                (s for s in ordered if s.status == "arrived"), None
            )
            next_stop = next(
                (s for s in ordered if s.status == "pending"), None
            )
            job.current_stop_id = current
            job.next_stop_id = next_stop

    # ── Write hook — tracking number, compatibility check, timeline, assignment log

    def write(self, vals):
        # Normalize bare-number temperature
        if vals.get("temp_requirement"):
            vals["temp_requirement"] = self._normalize_temp(vals["temp_requirement"])

        # Snapshot pre-write state for timeline events
        pre_stage = {job.id: job.stage_id for job in self}
        pre_vehicle = {job.id: job.vehicle_id for job in self}
        pre_driver = {job.id: job.driver_id for job in self}

        new_vehicle_id = vals.get("vehicle_id")
        if new_vehicle_id and new_vehicle_id is not False:
            vehicle = self.env["fleet.vehicle"].browse(new_vehicle_id)
            # Auto-assign the truck's driver if none explicitly provided.
            # Prefer Fleet's own driver_id (the officially assigned driver —
            # "assigned to the truck from the fleet module") over
            # x_current_driver_contact_id (a GeoTab live "who's currently
            # driving" signal): the latter can be stale/unset in practice
            # and previously took priority, which caused jobs to get
            # assigned to whatever contact that field happened to hold
            # instead of the truck's actual driver — the driver never saw
            # the job in their app because it was never assigned to them.
            if "driver_id" not in vals:
                auto_driver = (
                    vehicle.driver_id
                    or vehicle.x_current_driver_contact_id
                )
                if auto_driver:
                    vals["driver_id"] = auto_driver.id
            for job in self:
                if job.assignment_locked:
                    continue
                hard_blocks, soft_warnings = job._check_vehicle_compatibility(vehicle)
                if hard_blocks:
                    override_reason = (
                        vals.get("assignment_override_reason")
                        or job.assignment_override_reason
                    )
                    is_manager = self.env.user.has_group(
                        "prema_dispatch.group_dispatch_manager"
                    )
                    if not override_reason:
                        issue_list = "\n".join(f"• {b}" for b in hard_blocks)
                        if is_manager:
                            raise exceptions.UserError(
                                f"Truck compatibility issues:\n{issue_list}\n\n"
                                "Fill in an Override Reason on this job to proceed."
                            )
                        raise exceptions.UserError(
                            f"Cannot assign this truck — requirements not met:\n{issue_list}\n\n"
                            "Contact a Dispatch Manager to override."
                        )
                    vals["assignment_override_by"] = self.env.user.id
                    vals["assignment_override_at"] = fields.Datetime.now()
                if soft_warnings:
                    vals["assignment_warnings"] = "\n".join(soft_warnings)
                elif "assignment_warnings" not in vals:
                    vals["assignment_warnings"] = ""

        if "vehicle_id" in vals or "driver_id" in vals:
            for job in self:
                if job.assignment_locked:
                    continue
                old_v = job.vehicle_id
                old_d = job.driver_id
                new_v_id = vals.get("vehicle_id", old_v.id)
                new_d_id = vals.get("driver_id", old_d.id)
                if new_v_id != old_v.id or new_d_id != old_d.id:
                    self.env["prema.dispatch.assignment.log"].create({
                        "job_id": job.id,
                        "old_vehicle_id": old_v.id,
                        "new_vehicle_id": new_v_id,
                        "old_driver_id": old_d.id,
                        "new_driver_id": new_d_id,
                        "gps_lat_at_assignment": job.vehicle_last_lat,
                        "gps_lng_at_assignment": job.vehicle_last_lng,
                    })
                    # Freeze "who had it before" on the next open handoff
                    # boundary regardless of HOW the reassignment happened —
                    # previously this was only captured by the dedicated
                    # Transfer/Save-Receiving-Truck flow, so a plain Planner
                    # drag left no record at all of the original truck, and
                    # the Planner/reports had nothing to fall back on for
                    # "who actually did the pickup" once the header flipped.
                    if old_v or old_d:
                        boundary = job.stop_ids.filtered(
                            lambda s: s.stop_type in ("transfer", "cross_dock_drop")
                            and s.status != "completed"
                            and not s.transfer_from_vehicle_id
                            and not s.transfer_from_driver_id
                        ).sorted("sequence")[:1]
                        if boundary:
                            boundary.write({
                                "transfer_from_vehicle_id": old_v.id if old_v else False,
                                "transfer_from_driver_id": old_d.id if old_d else False,
                            })
        result = super().write(vals)

        # Post-write timeline events
        new_stage_id = vals.get("stage_id")
        new_veh_id = vals.get("vehicle_id")
        for job in self:
            # Generate tracking number when leaving draft for the first time
            if not job.tracking_number and job.stage_id.stage_type not in ("draft", "cancelled"):
                import secrets
                trk = f"TRK-{fields.Date.today().year}-{secrets.token_hex(3).upper()}"
                job.sudo().write({
                    "tracking_number": trk,
                    "booking_confirmed_at": fields.Datetime.now(),
                })
            # Stage change timeline event
            if new_stage_id and pre_stage.get(job.id, job.stage_id).id != job.stage_id.id:
                self._post_timeline(
                    job, "stage_changed",
                    notes=f"→ {job.stage_id.name}",
                )
            # Truck assigned timeline event
            if new_veh_id and not pre_vehicle.get(job.id):
                truck = job.vehicle_id
                self._post_timeline(
                    job, "truck_assigned",
                    notes=f"{truck.name}" + (f" / {job.driver_id.name}" if job.driver_id else ""),
                )
            # Push a bus notification on the same "driver_route_{partner_id}"
            # channel/type used by dispatch_stop.py's
            # _notify_driver_route_changed, so any driver whose route
            # changed — newly assigned by assign_job_to_truck OR removed by
            # unassign_truck — gets an immediate push instead of waiting for
            # the app's 15s poll. Notify BOTH the old and new driver: a
            # reassignment (or unassignment, where the new driver is empty)
            # must also tell the driver who lost the job so it disappears
            # from their app right away.
            old_driver = pre_driver.get(job.id)
            if job.driver_id != old_driver:
                notify_partner_ids = {p.id for p in (job.driver_id, old_driver) if p}
                if notify_partner_ids:
                    try:
                        self.env["bus.bus"]._sendmany([
                            [f"driver_route_{pid}", "route_updated", {"job_id": job.id}]
                            for pid in notify_partner_ids
                        ])
                    except Exception:
                        pass
        return result

    def _check_vehicle_compatibility(self, vehicle):
        """Returns (hard_blocks, soft_warnings) for assigning vehicle to self."""
        from datetime import datetime as _dt, timedelta as _td
        hard_blocks, soft_warnings = [], []
        if self.requires_reefer and not vehicle.x_reefer:
            hard_blocks.append("Reefer required — this truck is not refrigerated.")
        if self.requires_liftgate and not vehicle.x_liftgate:
            hard_blocks.append("Liftgate required — this truck has no liftgate.")

        # Use max_onboard_pallets (peak running load) when stop data exists,
        # fall back to approximate_skids (total booking estimate) otherwise.
        if self.max_onboard_pallets > 0:
            check_pallets = self.max_onboard_pallets
            pallet_label = f"Max onboard ({check_pallets} pallets peak)"
        else:
            check_pallets = self.approximate_skids
            pallet_label = f"Estimated skids ({check_pallets})"

        if vehicle.x_max_pallets and check_pallets:
            # Check combined load with other jobs already on this truck for the same day.
            # We compute the peak from all same-day jobs using a sequential timeline model:
            # if jobs don't time-overlap we take the max; if they do we sum them.
            other_jobs_pallets = 0
            if self.scheduled_pickup:
                pickup_day = self.scheduled_pickup.date()
                day_start = _dt.combine(pickup_day, _dt.min.time())
                day_end   = _dt.combine(pickup_day, _dt.max.time())
                other_jobs = self.env["prema.dispatch.job"].search([
                    ("vehicle_id", "=", vehicle.id),
                    ("id", "!=", self.id),
                    ("stage_id.is_cancelled", "=", False),
                    ("stage_id.is_completed", "=", False),
                    ("scheduled_pickup", ">=", day_start),
                    ("scheduled_pickup", "<=", day_end),
                ])
                # Conservative combined peak: sum of all jobs' max-onboard.
                # If they are clearly sequential (non-overlapping pickup times) the real peak
                # is lower — drivers can free capacity between jobs — so we flag as a soft
                # warning rather than a hard block, with guidance to verify timing.
                other_jobs_pallets = sum(
                    j.max_onboard_pallets or j.approximate_skids or 0
                    for j in other_jobs
                )

            combined = check_pallets + other_jobs_pallets
            if combined > vehicle.x_max_pallets:
                if other_jobs_pallets and check_pallets <= vehicle.x_max_pallets:
                    # This job alone fits, but combined with existing truck jobs it may not.
                    soft_warnings.append(
                        f"{pallet_label} ({check_pallets}p) combined with other truck jobs "
                        f"({other_jobs_pallets}p) gives {combined}p estimated peak — "
                        f"truck capacity is {vehicle.x_max_pallets}p. "
                        f"If deliveries happen before next pickup, actual peak may be lower."
                    )
                else:
                    soft_warnings.append(
                        f"{pallet_label} exceed truck capacity ({vehicle.x_max_pallets} pallets)."
                    )

        if (self.total_weight_lbs
                and vehicle.x_max_payload_lbs
                and self.total_weight_lbs > vehicle.x_max_payload_lbs):
            soft_warnings.append(
                f"Load weight ({self.total_weight_lbs:.0f} lbs) exceeds truck payload "
                f"({vehicle.x_max_payload_lbs:.0f} lbs)."
            )
        return hard_blocks, soft_warnings

    # ── Actions ───────────────────────────────────────────────────

    @api.model
    def _create_stops_from_ai_data(self, job, stops_data, base_date=None):
        """
        Create prema.dispatch.stop records from AI-parsed stop list.

        Handles both the new format (pallets_in/pallets_out/linked_load_group)
        and the legacy format (pallets) for backwards compatibility.
        """
        from datetime import datetime as _dt

        if not base_date:
            base_date = self._user_today()

        Stop = self.env["prema.dispatch.stop"]
        seq = 10
        for stop_data in stops_data:
            raw_type = (stop_data.get("type") or "dropoff").lower()
            if raw_type in ("delivery", "drop-off", "drop_off", "dropoff"):
                stop_type = "dropoff"
            elif raw_type == "return":
                stop_type = "return"
            else:
                stop_type = "pickup"

            pallets_in = int(stop_data.get("pallets_in") or 0)
            pallets_out = int(stop_data.get("pallets_out") or 0)
            # Backwards compat: legacy "pallets" field
            if pallets_in == 0 and pallets_out == 0:
                legacy = int(stop_data.get("pallets") or 0)
                if stop_type == "pickup":
                    pallets_in = legacy
                else:
                    pallets_out = legacy

            svc_min = 20 if stop_type == "pickup" else 15
            sched_time = None
            raw_sched = stop_data.get("scheduled_time")
            if raw_sched and raw_sched not in ("null", "", None):
                try:
                    sched_time = _dt.fromisoformat(str(raw_sched))
                except Exception:
                    pass

            # Time window fields from enhanced AI prompt
            tw_type = (stop_data.get("time_window_type") or "flexible").lower()
            if tw_type not in ("flexible", "window", "exact", "deadline"):
                tw_type = "flexible"

            def _parse_dt(val):
                if not val or str(val) in ("null", "", "None"):
                    return None
                try:
                    return _dt.fromisoformat(str(val))
                except Exception:
                    return None

            deadline_time = _parse_dt(stop_data.get("deadline_time"))
            exact_time_val = _parse_dt(stop_data.get("exact_time"))
            earliest_time = _parse_dt(stop_data.get("earliest_time"))
            latest_time = _parse_dt(stop_data.get("latest_time"))
            hard_deadline = bool(stop_data.get("hard_deadline") or tw_type == "deadline")

            # Auto-lock route for any stop after the first pickup that is itself a pickup
            # (return-to-origin multi-round pattern)
            is_return_load = stop_type == "pickup" and seq > 10

            Stop.create({
                "job_id": job.id,
                "sequence": seq,
                "stop_type": stop_type,
                "address": stop_data.get("address") or "",
                "dock_door": stop_data.get("dock_door") or "",
                "pallets_in": pallets_in,
                "pallets_out": pallets_out,
                "service_time_minutes": svc_min,
                "linked_load_group": int(stop_data.get("linked_load_group") or 0),
                "route_locked": is_return_load,
                "scheduled_time": sched_time,
                "time_window_type": tw_type,
                "deadline_time": deadline_time,
                "exact_time": exact_time_val,
                "earliest_time": earliest_time,
                "latest_time": latest_time,
                "hard_deadline": hard_deadline,
                "dispatcher_notes": stop_data.get("notes") or "",
                "pod_required": stop_type in ("dropoff", "return"),
            })
            seq += 10

        # Sync job.scheduled_pickup from the first pickup stop's scheduled_time
        # (AI prompt gives us the real pickup time; don't override with hardcoded 8AM)
        first_pickup = self.env["prema.dispatch.stop"].search(
            [("job_id", "=", job.id), ("stop_type", "=", "pickup")],
            order="sequence asc", limit=1,
        )
        if first_pickup and first_pickup.scheduled_time:
            if first_pickup.scheduled_time != job.scheduled_pickup:
                job.write({"scheduled_pickup": first_pickup.scheduled_time})

    def action_estimate_route(self):
        """Call Google Maps Directions API to compute ETAs for all stops."""
        self.ensure_one()
        if not self.stop_ids:
            raise exceptions.UserError("No stops on this job — add stops first.")

        from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
        svc = DispatchRouteService(self.env)
        result = svc.estimate_job_route(self)

        self.write({"route_estimated_at": fields.Datetime.now()})

        mins = result["total_minutes"]
        km = result["total_km"]
        h, m = divmod(mins, 60)
        duration_str = f"{h}h {m}min" if h else f"{m}min"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Route Estimated",
                "message": (
                    f"Route estimated: {duration_str} / {km:.1f} km "
                    f"across {result['stops_updated']} stops."
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_clear_route_estimates(self):
        """Clear all computed ETAs and drive times from stops."""
        self.ensure_one()
        self.stop_ids.write({
            "drive_time_from_prev_minutes": 0,
            "estimated_arrival": False,
            "estimated_departure": False,
        })
        self.write({
            "estimated_duration_minutes": 0,
            "estimated_distance_km": 0.0,
            "route_estimated_at": False,
        })

    def action_check_feasibility(self):
        """Open the feasibility checker wizard for this job."""
        self.ensure_one()
        pickup_stop = self.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")[:1]
        delivery_stop = self.stop_ids.filtered(lambda s: s.stop_type == "dropoff").sorted("sequence")[:1]

        return {
            "type": "ir.actions.act_window",
            "name": "Check Feasibility — Can We Do This?",
            "res_model": "prema.dispatch.feasibility.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_job_id": self.id,
                "default_pickup_address": pickup_stop.address if pickup_stop else "",
                "default_dropoff_address": delivery_stop.address if delivery_stop else "",
                "default_pallets": self.max_onboard_pallets or self.approximate_skids or 0,
                "default_requires_reefer": self.requires_reefer,
                "default_requires_liftgate": self.requires_liftgate,
                "default_delivery_deadline": self.delivery_deadline,
                "default_pickup_earliest": self.pickup_earliest,
                "default_pickup_latest": self.pickup_latest,
            },
        }

    def action_get_best_options(self):
        """Compute and display best dispatch options for this job."""
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService
        svc = DispatchOptimizationService(self.env)
        options = svc.get_best_dispatch_options(self.id)
        scored = svc.rank_trucks_for_job(self.id)

        if scored:
            best = scored[0]
            self.write({
                "recommended_truck_id": best["truck_id"],
                "optimization_score": max(0, 100 - best["score"]),
            })

        summary_lines = []
        for opt in options:
            summary_lines.append(f"• {opt['title']}: {opt['description']}")
            if opt.get("savings"):
                summary_lines.append(f"  Savings: {opt['savings']}")

        notes = "\n".join(summary_lines) if summary_lines else "No specific recommendations. All trucks checked."
        self.write({"feasibility_notes": notes})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Best Dispatch Options",
                "message": (
                    f"Found {len(options)} option(s). "
                    f"{'Recommended: ' + scored[0]['name'] if scored else 'No trucks available.'}"
                ),
                "type": "success" if options else "warning",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @api.model
    def action_suggest_consolidated_route(self, vehicle_id, date_str):
        """Called from the Planner's "Consolidate" button. Computes a
        suggested combined stop order across every job sharing this truck/
        day and opens it in a wizard for the dispatcher to accept or
        cancel — nothing is written until they accept."""
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService
        svc = DispatchOptimizationService(self.env)
        result = svc.suggest_consolidated_route(vehicle_id, date_str)

        if result.get("error"):
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Nothing to Consolidate", "message": result["error"],
                            "type": "warning", "sticky": False},
            }

        wizard = self.env["prema.dispatch.consolidation.wizard"].create({
            "vehicle_id": vehicle_id,
            "date": date_str,
            "line_ids": [(0, 0, {
                "sequence": i * 10,
                "job_id": s["job_id"],
                "stop_id": s["stop_id"],
                "stop_type": s["stop_type"],
                "address": s["address"],
                "pallets_in": s["pallets_in"],
                "pallets_out": s["pallets_out"],
                "eta": s["eta"],
                "cross_dock_type": s["cross_dock_type"] or False,
                "location_id": s["location_id"] or False,
                "pallets": s["pallets"],
                "origin_stop_name": s["origin_stop_name"],
                "origin_stop_id": s.get("origin_stop_id") or False,
            }) for i, s in enumerate(result["suggested_order"])],
        })
        return {
            "type": "ir.actions.act_window",
            "name": "Suggested Consolidated Route",
            "res_model": "prema.dispatch.consolidation.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_optimize_route(self):
        """Re-order pending stops to minimize drive time."""
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService
        svc = DispatchOptimizationService(self.env)
        result = svc.optimize_route(self.id)

        added = result.get("added_distance_km", 0)
        added_min = result.get("added_minutes", 0)
        msg = f"Route optimized. {result.get('stop_count', 0)} stops reordered."
        if added > 0:
            msg += f" Added {added:.1f} km / {added_min} min."
        elif added < 0:
            msg += f" Saved {-added:.1f} km / {-added_min} min."

        missed = result.get("missed_deadlines") or []
        if missed:
            lines = [
                f"⚠ Impossible Assignment — {m['stop_name']}: late by {m['late_by_minutes']} min "
                f"(deadline {m['deadline'][11:16]}, ETA {m['estimated_arrival'][11:16]})"
                for m in missed
            ]
            msg += "<br/>" + "<br/>".join(lines) + (
                "<br/>Recommendations: reorder flexible stops around this deadline, "
                "assign a second truck, or override manually."
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Route Optimized — Deadline At Risk",
                    "message": msg,
                    "type": "warning",
                    "sticky": True,
                    "next": {"type": "ir.actions.client", "tag": "reload"},
                },
            }

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Route Optimized",
                "message": msg,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_rank_trucks(self):
        """Rank all trucks for this job and set recommended_truck_id."""
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService
        svc = DispatchOptimizationService(self.env)
        scored = svc.rank_trucks_for_job(self.id)
        if scored:
            self.write({
                "recommended_truck_id": scored[0]["truck_id"],
                "optimization_score": max(0, 100 - scored[0]["score"]),
            })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Trucks Ranked",
                "message": (
                    f"Best truck: {scored[0]['name']} ({scored[0]['distance_to_pickup_km']:.1f} km away)."
                    if scored else "No qualifying trucks found."
                ),
                "type": "success" if scored else "warning",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    # ── JSON-RPC endpoints (called by OWL board) ──────────────────

    @api.model
    def get_booking_status_board_data(self):
        """Structured Booking Board data: a single unified list, one row per
        open job (Booking #, Customer, Status, Pickup, Deadline, Route,
        Skids, Equipment, Priority, Truck, Driver, Feasibility, Notes).
        Unassigned jobs (no vehicle_id yet) are regular rows too — Truck/
        Driver show "—" and Status shows "Unassigned" — sorted to the top
        so dispatchers still see them first, without a separate boxed
        panel. Feasibility is computed server-side per job (see
        _board_feasibility) — the client only renders the badge.
        Delivered jobs disappear on their own next refresh once
        _check_all_stops_done() flips the stage to completed. Cancelled
        jobs are only shown if they still have a truck assigned (so a
        dispatcher knows to release it) — a cancelled job with no truck
        needs no action and is dropped.
        """
        import pytz
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")

        def fmt_local(dt):
            """Naive UTC -> 12-hour AM/PM in the dispatcher's timezone."""
            if not dt:
                return None, None
            local = pytz.utc.localize(dt).astimezone(user_tz)
            return local.strftime("%I:%M %p").lstrip("0"), local.strftime("%d/%m/%Y")

        def route_label(job):
            return f"{job.pickup_city} → {job.delivery_cities}" if job.pickup_city else job.name

        def skids(job):
            return job.max_onboard_pallets or job.approximate_skids or 0

        jobs = self.search([
            ("stage_id.is_completed", "=", False),
            "|", ("vehicle_id", "!=", False), ("stage_id.is_cancelled", "=", False),
        ])
        # Unassigned jobs first (dispatcher needs to act on these), then by pickup time.
        from datetime import datetime as _dt
        jobs = jobs.sorted(key=lambda j: (bool(j.vehicle_id), j.scheduled_pickup or _dt.max))

        now = fields.Datetime.now()
        rows = []
        for job in jobs:
            stops = job.stop_ids.filtered(lambda s: s.status != "cancelled")
            done_stops = stops.filtered(lambda s: s.status == "completed")
            active_stops = stops.filtered(lambda s: s.status in ("en_route", "arrived"))
            pickup_stops = stops.filtered(lambda s: s.stop_type == "pickup")
            pickup_done = pickup_stops and all(s.status == "completed" for s in pickup_stops)
            # Next open Driver Transfer / Cross-Dock Drop boundary, if any —
            # drives the "Transfer" status below and the Handoff column.
            transfer_boundary = stops.filtered(
                lambda s: s.stop_type in ("transfer", "cross_dock_drop") and s.status != "completed"
            ).sorted("sequence")[:1]

            is_late = bool(
                job.delivery_deadline and job.delivery_deadline < now
                and not (stops and all(s.status == "completed" for s in stops))
            )

            if job.stage_id.is_cancelled:
                status_key, time_label, date_label = "cancelled", "—", "—"
            elif is_late:
                status_key = "late"
                time_label, date_label = fmt_local(job.delivery_deadline)
            elif not job.vehicle_id:
                status_key = "unassigned"
                time_label, date_label = fmt_local(job.scheduled_pickup)
            elif stops and len(done_stops) == len(stops):
                status_key = "delivered"
                last = stops.sorted("sequence")[-1:]
                dt = last.actual_departure_time if last else now
                time_label, date_label = fmt_local(dt)
            elif active_stops:
                status_key = "in_progress"
                dt = job.delivery_deadline or job.scheduled_pickup
                time_label, date_label = fmt_local(dt)
            elif transfer_boundary and pickup_done:
                status_key = "transfer"
                dt = job.delivery_deadline or job.scheduled_pickup
                time_label, date_label = fmt_local(dt)
            elif pickup_done and len(stops) > len(pickup_stops):
                status_key = "in_progress"
                dt = job.delivery_deadline or job.scheduled_pickup
                time_label, date_label = fmt_local(dt)
            elif pickup_done:
                status_key = "picked_up"
                first_pickup = pickup_stops.sorted("sequence")[:1]
                dt = first_pickup.actual_departure_time if first_pickup else now
                time_label, date_label = fmt_local(dt)
            else:
                status_key = "planned"
                time_label, date_label = fmt_local(job.scheduled_pickup)
            time_label = time_label or "—"
            date_label = date_label or "—"

            pickup_time, pickup_date = fmt_local(job.scheduled_pickup)
            deadline_time, deadline_date = fmt_local(job.delivery_deadline)
            badge = job._board_feasibility()

            handoff_label = ""
            if transfer_boundary:
                if transfer_boundary.transfer_to_vehicle_id:
                    handoff_label = f"Staged → {transfer_boundary.transfer_to_vehicle_id.display_name}"
                elif pickup_done:
                    handoff_label = "Awaiting receiving truck"
                else:
                    handoff_label = "Handoff planned"

            rows.append({
                "job_id":          job.id,
                "reference":       job.name,
                "tracking":        job.tracking_number or "",
                "customer":        job.partner_id.name or "",
                "pickup_label":    pickup_time or "—",
                "pickup_date":     pickup_date or "—",
                "deadline_label":  deadline_time or "—",
                "deadline_date":   deadline_date or "—",
                "route":           route_label(job),
                "skids":           skids(job),
                "equipment_type":  job.equipment_type or "",
                "priority":        job.priority or "normal",
                "feasibility":     badge["verdict"],
                "feasibility_reason": badge["reason"],
                "notes":           job.internal_notes or job.feasibility_notes or "",
                "status_key":      status_key,
                "time_label":      time_label,
                "date_label":      date_label,
                "truck":           job.vehicle_id.name or "—",
                "driver":          job.driver_id.name or "—",
                "handoff_label":   handoff_label,
            })

        return {"rows": rows}

    @api.model
    def get_dispatch_board_data(self, date_str=None, window_start=None, window_days=3):
        """Return data for the Dispatch Schedule Board OWL component.

        date_str     – the "active" date for the truck grid
        window_start – first day of the 3-day unassigned column window
        window_days  – how many days in the window (default 3)
        """
        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        from datetime import date, datetime, timedelta

        check_date = date.fromisoformat(date_str) if date_str else self._user_today()
        win_start  = date.fromisoformat(window_start) if window_start else check_date

        avail_svc = DispatchAvailabilityService(self.env)
        trucks = avail_svc.get_truck_day_schedule(check_date)

        # Unassigned jobs for the whole window range
        win_end   = win_start + timedelta(days=window_days)
        # Use a small 2h UTC buffer on either side to handle timezone edge cases
        # without pulling in jobs from entirely different days.
        day_start = datetime.combine(win_start, datetime.min.time()) - timedelta(hours=12)
        day_end   = datetime.combine(win_end,   datetime.max.time()) + timedelta(hours=12)

        import pytz
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")

        unassigned = self.env["prema.dispatch.job"].search([
            ("vehicle_id", "=", False),
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
        ] + ["|",
             ("scheduled_pickup", "=", False),
             "&",
             ("scheduled_pickup", ">=", day_start),
             ("scheduled_pickup", "<=", day_end),
        ])

        unassigned_cards = []
        win_dates = {(win_start + timedelta(days=i)).isoformat() for i in range(window_days)}

        for job in unassigned:
            # Convert scheduled_pickup to user-local date string for column grouping.
            # Only keep jobs whose pickup date actually falls within the display window;
            # jobs outside the window (caught by the UTC buffer) are excluded so each
            # job appears in exactly one column — its pickup date.
            pickup_date_local = None
            if job.scheduled_pickup:
                utc_dt = pytz.utc.localize(job.scheduled_pickup)
                local_dt = utc_dt.astimezone(user_tz)
                pickup_date_local = local_dt.date().isoformat()
                # Skip jobs whose pickup date is outside the visible window
                # (they were fetched only because of the UTC timezone buffer).
                if pickup_date_local not in win_dates and pickup_date_local is not None:
                    # Still include them — they'll appear in jobsWithoutDate on JS side
                    # if they have no date, but here they have a date outside window.
                    # Show them in the closest boundary column instead.
                    win_list = sorted(win_dates)
                    if pickup_date_local < win_list[0]:
                        pickup_date_local = win_list[0]
                    elif pickup_date_local > win_list[-1]:
                        pickup_date_local = win_list[-1]

            unassigned_cards.append({
                "job_id":                  job.id,
                "name":                    job.name,
                "partner":                 job.partner_id.name or "",
                "pickup_city":             job.pickup_city or "",
                "delivery_cities":         job.delivery_cities or "",
                "pallets":                 job.max_onboard_pallets or job.approximate_skids or 0,
                "requires_reefer":         job.requires_reefer,
                "requires_liftgate":       job.requires_liftgate,
                "priority":                job.priority,
                "scheduled_pickup":        job.scheduled_pickup.isoformat() if job.scheduled_pickup else None,
                "pickup_date_local":       pickup_date_local,
                "requested_delivery_date": job.requested_delivery_date.isoformat() if job.requested_delivery_date else None,
                "delivery_deadline":       job.delivery_deadline.isoformat() if job.delivery_deadline else None,
                "hard_deadline":           job.hard_deadline,
                "risk_level":              job.risk_level or "green",
                "corridor_tag":            job.corridor_tag or "",
                "service_type":            job.service_type or "",
                "source_document_name":    job.source_document_name or "",
            })

        # Summary per window day (for calendar popup)
        day_summaries = {}
        for d_offset in range(window_days + 1):
            d = win_start + timedelta(days=d_offset)
            d_str = d.isoformat()
            day_jobs = [j for j in unassigned_cards if j["pickup_date_local"] == d_str]
            # Also get assigned truck jobs for that day
            d_trucks = avail_svc.get_truck_day_schedule(d) if d != check_date else trucks
            day_summaries[d_str] = {
                "unassigned_count": len(day_jobs),
                "truck_summaries": [
                    {"name": t["name"], "jobs": len(t["jobs"]), "status": t["status"]}
                    for t in d_trucks
                ],
            }

        # Weekly calendar: 7 days starting from Monday of the active week
        monday = check_date - timedelta(days=check_date.weekday())
        week_summaries = {}
        for d_offset in range(7):
            d = monday + timedelta(days=d_offset)
            d_str = d.isoformat()
            d_trucks = avail_svc.get_truck_day_schedule(d) if d != check_date else trucks
            week_summaries[d_str] = {
                "label":       d.strftime("%a %d"),
                "weekday":     d.weekday(),
                "is_today":    d == self._user_today(),
                "is_selected": d == check_date,
                "unassigned":  sum(1 for j in unassigned_cards if j["pickup_date_local"] == d_str),
                "truck_summaries": [
                    {
                        "name":   t["name"],
                        "jobs":   len(t["jobs"]),
                        "status": t["status"],
                        "id":     t["truck_id"],
                    }
                    for t in d_trucks
                ],
            }

        return {
            "date":            check_date.isoformat(),
            "window_start":    win_start.isoformat(),
            "trucks":          trucks,
            "unassigned_jobs": unassigned_cards,
            "day_summaries":   day_summaries,
            "week_summaries":  week_summaries,
            "week_start":      monday.isoformat(),
        }

    @api.model
    def auto_plan_jobs(self, dates):
        """
        Auto-assign unassigned jobs from the given dates to the best available trucks.

        Scoring:
        - Equipment match is required (reefer, liftgate)
        - Corridor bonus: +40 if truck already has a job going same direction
        - Capacity: truck must have room
        - Distance: closest truck to pickup gets bonus

        Returns { assigned: [...], skipped: [...] }
        """
        from odoo.addons.prema_dispatch.services.optimization_service import DispatchOptimizationService
        from datetime import datetime, timedelta
        import pytz

        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")

        assigned_list = []
        skipped_list  = []

        # Collect all unassigned jobs for the requested dates
        date_objs = [datetime.fromisoformat(d).date() for d in dates]
        all_jobs  = []

        for d in date_objs:
            day_start = datetime.combine(d, datetime.min.time())
            day_end   = datetime.combine(d, datetime.max.time())
            jobs = self.env["prema.dispatch.job"].search([
                ("vehicle_id", "=", False),
                ("stage_id.is_cancelled", "=", False),
                ("stage_id.is_completed", "=", False),
                "|",
                ("scheduled_pickup", "=", False),
                "&",
                ("scheduled_pickup", ">=", day_start),
                ("scheduled_pickup", "<=", day_end),
            ])
            all_jobs.extend(jobs)

        if not all_jobs:
            return {"assigned": [], "skipped": [], "message": "No unassigned jobs found for the selected dates."}

        # Sort: emergency → urgent → hard_deadline → normal
        def _priority(job):
            p = {"emergency": 0, "urgent": 1, "normal": 3}.get(job.priority, 3)
            if job.hard_deadline:
                p = min(p, 2)
            return p

        all_jobs = sorted(set(all_jobs), key=_priority)

        opt_svc = DispatchOptimizationService(self.env)

        # Track trucks already assigned in this auto-plan run (to avoid double-booking capacity)
        assigned_in_run = {}  # truck_id → list of job_ids
        touched_truck_dates = set()  # (truck_id, date_str) — routed after assignment below

        for job in all_jobs:
            scored = opt_svc.rank_trucks_for_job(job.id)

            # Corridor bonus: boost trucks already heading the same direction
            if job.corridor_tag:
                for entry in scored:
                    truck_jobs = self.env["prema.dispatch.job"].search([
                        ("vehicle_id", "=", entry["truck_id"]),
                        ("stage_id.is_cancelled", "=", False),
                        ("stage_id.is_completed", "=", False),
                    ])
                    same_dir = any(
                        tj.corridor_tag == job.corridor_tag
                        for tj in truck_jobs
                    )
                    if same_dir:
                        entry["score"] = max(0, entry["score"] - 40)  # lower = better

            # Re-sort after corridor bonus
            scored.sort(key=lambda x: x["score"])

            if not scored:
                skipped_list.append({
                    "job_id": job.id, "job_name": job.name,
                    "reason": "No compatible truck available (equipment/capacity mismatch)",
                })
                continue

            best = scored[0]
            result = self.assign_job_to_truck(job.id, best["truck_id"])

            if result.get("success"):
                assigned_list.append({
                    "job_id":     job.id,
                    "job_name":   job.name,
                    "truck_id":   best["truck_id"],
                    "truck_name": best["name"],
                    "driver":     best.get("driver_name", ""),
                    "warnings":   result.get("warnings", ""),
                    "corridor":   job.corridor_tag or "",
                })
                assigned_in_run.setdefault(best["truck_id"], []).append(job.id)
                if job.scheduled_pickup:
                    touched_truck_dates.add((best["truck_id"], job.scheduled_pickup.date().isoformat()))
            else:
                skipped_list.append({
                    "job_id": job.id, "job_name": job.name,
                    "reason": result.get("error", "Assignment failed"),
                })

        # Auto Plan must hand back an actually driveable route, not just a
        # truck assignment — for every truck/day touched this run, build the
        # real stop sequence (including any cross-dock interleave) instead
        # of leaving that to a separate manual "Consolidate" step.
        cross_dock_legs_total = 0
        for truck_id, date_str in touched_truck_dates:
            try:
                consolidated = opt_svc.apply_consolidated_route(truck_id, date_str)
            except Exception:
                _logger.exception("Auto Plan: route consolidation failed for truck %s on %s", truck_id, date_str)
                continue
            if consolidated and not consolidated.get("error"):
                cross_dock_legs_total += consolidated.get("cross_dock_legs", 0)

        total = len(all_jobs)
        message = (
            f"Auto Plan: {len(assigned_list)}/{total} loads assigned. "
            f"{len(skipped_list)} could not be matched."
        )
        if cross_dock_legs_total:
            message += f" {cross_dock_legs_total} cross-dock leg(s) created to interleave loads."
        return {
            "assigned": assigned_list,
            "skipped":  skipped_list,
            "message":  message,
        }

    @api.model
    def check_feasibility_rpc(self, payload):
        """JSON-RPC wrapper for the feasibility service."""
        from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService
        svc = DispatchFeasibilityService(self.env)
        return svc.check(payload)

    def _feasibility_check_payload(self):
        """Build feasibility_service.py's check() payload for this job.

        Shared by _feasibility_option_for_truck (per-truck option) and
        _feasibility_overall_verdict (Booking Board's "can ANY truck do
        this?" verdict for unassigned jobs) so both work off the exact same
        request instead of two parallel payload builds.
        """
        self.ensure_one()
        pickup_stop = self.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")[:1]
        delivery_stop = self.stop_ids.filtered(lambda s: s.stop_type == "dropoff").sorted("sequence")[:1]
        if not pickup_stop or not delivery_stop or not pickup_stop.address or not delivery_stop.address:
            return None  # not enough route data yet to judge — don't block on missing info
        return {
            "pickup_address": pickup_stop.address,
            "dropoff_address": delivery_stop.address,
            "check_date": (self.scheduled_pickup or fields.Datetime.now()).date().isoformat(),
            "pallets": self.max_onboard_pallets or self.approximate_skids or 0,
            "requires_reefer": self.requires_reefer,
            "requires_liftgate": self.requires_liftgate,
            "delivery_deadline": self.delivery_deadline,
            "pickup_earliest": self.pickup_earliest,
            "pickup_latest": self.pickup_latest,
            # Excludes this job from "other jobs already on the truck today"
            # (it may already have vehicle_id set, e.g. a feasibility
            # re-check) and flags whether it touches an Allow Cross-Dock
            # location, both consumed by feasibility_service.check() to
            # avoid a false not_feasible when jobs can interleave through it.
            "exclude_job_id": self.id,
            "job_has_cross_dock_stop": any(
                s.saved_location_id.allow_cross_dock for s in self.stop_ids
            ),
        }

    def _feasibility_option_for_truck(self, vehicle_id):
        """Run the feasibility checker for this job and pick out the option
        for one specific truck — same payload shape as action_check_feasibility,
        reusing DispatchFeasibilityService instead of a second implementation."""
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService

        payload = self._feasibility_check_payload()
        if not payload:
            return None
        result = DispatchFeasibilityService(self.env).check(payload)
        for opt in result.get("options", []):
            if opt.get("truck_id") == vehicle_id:
                return opt
        return {"verdict": "not_feasible", "reason": "Truck doesn't meet this job's equipment/capacity requirements.", "truck_id": vehicle_id}

    def _feasibility_overall_verdict(self):
        """Can ANY truck feasibly do this job? Used for jobs with no truck
        assigned yet (Booking Board's Unassigned panel) where there's no
        specific truck to check against — reuses the same
        DispatchFeasibilityService.check() the per-truck option above uses,
        just reading its top-level verdict/reason instead of one option."""
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService

        payload = self._feasibility_check_payload()
        if not payload:
            return {"verdict": "unknown", "reason": ""}
        result = DispatchFeasibilityService(self.env).check(payload)
        return {"verdict": result.get("verdict") or "unknown", "reason": result.get("reason") or ""}

    def _board_feasibility(self):
        """Feasibility badge for the Booking Board — feasible/risky/not_feasible,
        reusing feasibility_service.py's verdict logic rather than a second
        heuristic. Prefers the already-computed feasibility_status/notes
        (kept fresh by assign_job_to_truck and reset to 'unknown' by
        unassign_truck) over a live check: the board polls every 20s, and
        re-running the Google-Maps-backed checker for every job on every
        poll would multiply route-API calls for data that hasn't changed.
        The first time a job has no cached verdict, it's computed here once
        and written back so later polls reuse it.
        """
        self.ensure_one()
        if self.feasibility_status and self.feasibility_status != "unknown":
            return {"verdict": self.feasibility_status, "reason": self.feasibility_notes or ""}
        if self.vehicle_id:
            option = self._feasibility_option_for_truck(self.vehicle_id.id)
            verdict = option.get("verdict") if option else "unknown"
            reason = option.get("reason", "") if option else ""
        else:
            result = self._feasibility_overall_verdict()
            verdict, reason = result["verdict"], result["reason"]
        if verdict and verdict != "unknown":
            self.write({"feasibility_status": verdict, "feasibility_notes": reason})
        return {"verdict": verdict or "unknown", "reason": reason}

    @api.model
    def assign_job_to_truck(self, job_id, truck_id, force=False):
        """Assign a truck to a job (Planner drag-and-drop) and return the
        updated job card data. Truck/driver assignment happens ONLY from
        dispatch-side flows like this one — accounting/sales users editing
        the invoice's Dispatch tab cannot set vehicle_id/driver_id (enforced
        by that view being read-only, see account_move_dispatch_views.xml).

        Runs a feasibility check first (item 15): a not_feasible truck blocks
        the assignment unless force=True and the caller is a dispatch
        manager, matching the "block unless override" rule from the audit.
        """
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        vehicle = self.env["fleet.vehicle"].browse(truck_id)
        if not vehicle.exists():
            return {"success": False, "error": "Truck not found"}

        # Run the (Google-Maps-backed) feasibility checker ONCE, not once to
        # gate + once to classify risky/feasible — the second call was pure
        # waste (double API cost per drag) and, worse, if it happened to
        # raise (network hiccup, timeout), the whole method aborted via the
        # broad except below *after* vehicle_id/driver_id had already been
        # committed by a separate write(), leaving a job with a truck+driver
        # assigned but stuck in the Draft stage — exactly the bug reported.
        # A feasibility-service failure now degrades to "unknown" instead of
        # blocking the assignment outright.
        try:
            option = job._feasibility_option_for_truck(truck_id)
        except Exception:
            _logger.exception("Feasibility check failed during assign_job_to_truck(%s, %s)", job_id, truck_id)
            option = None

        if not force and option and option.get("verdict") == "not_feasible":
            is_manager = self.env.user.has_group("prema_dispatch.group_dispatch_manager")
            return {
                "success": False,
                "feasibility_blocked": True,
                "can_override": is_manager,
                "error": option.get("reason") or "This truck can't feasibly complete this job.",
            }

        try:
            vals = {"vehicle_id": truck_id}
            risky_warning = ""
            if option and option.get("verdict") == "risky":
                vals["feasibility_status"] = "risky"
                vals["feasibility_notes"] = option.get("reason", "")
                risky_warning = f"At risk: {option.get('reason', '')}"
            elif option and option.get("verdict") == "feasible":
                vals["feasibility_status"] = "feasible"
            # Advance a booking-phase job to "Assigned" once it has a truck —
            # only moves forward, never overrides a stage the dispatcher
            # already pushed further along (e.g. Ready to Dispatch).
            assigned_stage = self.env.ref(
                "prema_dispatch.stage_assigned", raise_if_not_found=False
            )
            if (
                assigned_stage
                and job.stage_id
                and job.stage_id.is_booking_phase
                and job.stage_id.sequence < assigned_stage.sequence
            ):
                vals["stage_id"] = assigned_stage.id
            job.write(vals)
            no_driver_warning = ""
            if not job.driver_id:
                no_driver_warning = (
                    "This truck has no default driver assigned in Fleet."
                )
            warnings = "\n".join(
                w for w in (job.assignment_warnings or "", no_driver_warning, risky_warning) if w
            )
            return {
                "success": True,
                "job_id": job_id,
                "truck_name": vehicle.name,
                "driver_name": job.driver_id.name if job.driver_id else "",
                "stage_name": job.stage_id.name if job.stage_id else "",
                "warnings": warnings,
            }
        except Exception as exc:
            _logger.exception("assign_job_to_truck(%s, %s) failed after feasibility check", job_id, truck_id)
            return {"success": False, "error": str(exc)}

    @api.model
    def unassign_truck(self, job_id):
        """Remove truck/driver assignment — returns job to unassigned queue.

        Also known as the Booking Board / Planner "unassign" action
        (action_unassign_job in the audit spec) — kept as one @api.model
        method, matching the existing assign_job_to_truck(job_id, ...)
        RPC convention, instead of adding a second entry point.

        Clears everything assign_job_to_truck sets — vehicle_id, driver_id
        (auto-assigned from the truck by the write() override),
        feasibility_status/notes computed for that truck, assignment
        warnings, and any compatibility override — and moves the stage
        back to the pre-assignment stage (the same "Draft" stage new
        bookings are created in, see _default_stage / booking_template.py),
        but only while the job is still in the booking phase. A job that's
        already been dispatched to a driver keeps its stage untouched.
        """
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        try:
            vals = {
                "vehicle_id": False,
                "driver_id": False,
                "assignment_warnings": "",
                "feasibility_status": "unknown",
                "feasibility_notes": "",
                "assignment_override_reason": False,
                "assignment_override_by": False,
                "assignment_override_at": False,
            }
            if job.stage_id and job.stage_id.is_booking_phase:
                draft_stage = job._default_stage()
                if draft_stage:
                    vals["stage_id"] = draft_stage.id
            job.write(vals)
            return {"success": True, "job_name": job.name}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def action_send_to_driver(self):
        self.ensure_one()
        if not self.vehicle_id:
            raise exceptions.UserError(
                "Assign a truck before sending to driver."
            )
        assigned_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "dispatched")], limit=1
        )
        vals = {"sent_to_driver_at": fields.Datetime.now()}
        if assigned_stage:
            vals["stage_id"] = assigned_stage.id
        self.write(vals)
        driver_label = self.driver_id.name if self.driver_id else "No driver assigned"
        self.message_post(
            body=(
                f"<b>Dispatched to driver.</b> Truck: {self.vehicle_id.name} | "
                f"Driver: {driver_label}"
            )
        )
        self._post_timeline(
            self, "dispatch_confirmed",
            notes=f"Truck: {self.vehicle_id.name}" + (f" | Driver: {self.driver_id.name}" if self.driver_id else ""),
        )

    def action_mark_completed(self):
        """Mark job completed, attach docs to invoice, auto-post if all jobs done."""
        self.ensure_one()
        completed_stage = self.env["prema.dispatch.stage"].search(
            [("is_completed", "=", True)], limit=1, order="sequence asc"
        )
        if not completed_stage:
            raise exceptions.UserError(
                "No completed stage configured. Go to Prema Dispatch › Settings › Stages."
            )
        self.write({
            "stage_id": completed_stage.id,
            "completed_at": fields.Datetime.now(),
        })
        self._post_timeline(self, "all_stops_done")
        if self.invoice_id:
            self._attach_documents_to_invoice()
            all_jobs = self.invoice_id.dispatch_job_ids
            all_done = all(
                j.stage_id.is_completed and j.pod_complete
                for j in all_jobs
            )
            if all_done and self.invoice_id.state == "draft":
                self._auto_post_invoice()
                self._post_timeline(
                    self, "invoice_completed",
                    notes=self.invoice_id.name,
                )

    def action_reopen_job(self):
        """Undo action_mark_completed / auto-completion (_check_all_stops_done)
        so a job that was finished by mistake, or needs more work, can be
        worked again. Admin-only — drivers already have ORM write access to
        this model for their own stops/status updates, so the restriction
        has to be enforced here, not just via a view-level `groups`
        attribute, or a driver write() could still flip stage_id."""
        self.ensure_one()
        if not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise exceptions.AccessError(
                "Only a Dispatch Manager can restart a completed job."
            )
        reopen_stage = self.env["prema.dispatch.stage"].search(
            [("is_dispatched", "=", True)], limit=1, order="sequence desc"
        )
        if not reopen_stage:
            raise exceptions.UserError(
                "No dispatched-type stage configured to reopen into. "
                "Go to Prema Dispatch › Settings › Stages."
            )
        self.write({
            "stage_id": reopen_stage.id,
            "completed_at": False,
        })
        self._post_timeline(
            self, "job_reopened",
            notes=f"Reopened by {self.env.user.name}",
        )

    def action_open_stops(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Stops",
            "res_model": "prema.dispatch.stop",
            "view_mode": "list,form",
            "domain": [("job_id", "=", self.id)],
            "context": {"default_job_id": self.id},
        }

    def action_reassign_vehicle_driver(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Reassign Truck / Driver",
            "res_model": "prema.dispatch.job",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": {"reassign_mode": True},
        }

    def action_refresh_geotab_location(self):
        """Pull live GPS and motion data from GeoTab for the assigned vehicle."""
        self.ensure_one()
        if not self.vehicle_id or not self.vehicle_id.x_geotab_device_id:
            raise exceptions.UserError(
                "No GeoTab device linked to this truck. "
                "Configure it in Fleet › Vehicle › GeoTab tab."
            )
        try:
            from datetime import timezone
            from odoo.addons.premafirm_ai_engine.services.geotab_service import (
                GeotabService,
            )
            svc = GeotabService(self.env)
            device_id = self.vehicle_id.x_geotab_device_id
            status_list = svc.get_device_status_info(device_id)
            if not status_list:
                raise exceptions.UserError("No live data returned from GeoTab.")
            record = status_list[0]
            lat, lng, gps_dt = svc.extract_location(record)
            if gps_dt is not None and gps_dt.tzinfo is not None:
                # Odoo Datetime fields are naive UTC; GeoTab returns tz-aware UTC.
                gps_dt = gps_dt.astimezone(timezone.utc).replace(tzinfo=None)
            speed = float((record.get("speed") or 0))
            is_moving = speed > 2.0

            self.write({
                "vehicle_speed_kmh": speed,
                "vehicle_moving_state": "moving" if is_moving else "stopped",
                "vehicle_gps_refreshed_at": fields.Datetime.now(),
            })
            # Also update fleet.vehicle live fields
            update_vals = {"x_last_eld_sync_at": fields.Datetime.now()}
            if lat is not None:
                update_vals.update({
                    "x_last_location_lat": lat,
                    "x_last_location_lng": lng,
                    "x_last_location_at": gps_dt or fields.Datetime.now(),
                })
            self.vehicle_id.write(update_vals)
        except Exception as exc:
            _logger.exception("GeoTab refresh failed for job %s", self.name)
            raise exceptions.UserError(f"GeoTab refresh failed: {exc}") from exc

    def action_attach_documents_to_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise exceptions.UserError("No invoice linked to this dispatch job.")
        count = self._attach_documents_to_invoice()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Documents Attached",
                "message": f"{count} file(s) attached to invoice {self.invoice_id.name}.",
                "type": "success",
                "sticky": False,
            },
        }

    def action_open_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise exceptions.UserError("No invoice linked.")
        return {
            "type": "ir.actions.act_window",
            "name": "Invoice",
            "res_model": "account.move",
            "res_id": self.invoice_id.id,
            "view_mode": "form",
        }

    def action_find_best_truck(self):
        """Call suggest_dispatch_plan_rpc(), auto-assign the best truck, post chatter summary."""
        self.ensure_one()
        active_stops = self.stop_ids.filtered(
            lambda s: s.status != "cancelled"
        ).sorted("sequence")
        if len(active_stops) < 2:
            raise exceptions.UserError(
                "Add at least a pickup and a delivery stop before finding a truck."
            )
        stops_payload = [
            {
                "type": s.stop_type,
                "address": s.address or "",
                "lat": s.latitude,
                "lng": s.longitude,
            }
            for s in active_stops
        ]
        try:
            estimator = self.env["premafirm.rate.estimator"]
            result = estimator.suggest_dispatch_plan_rpc(
                stops=stops_payload,
                scheduled_at=self.scheduled_pickup.isoformat() if self.scheduled_pickup else None,
                require_reefer=self.requires_reefer,
                require_liftgate=self.requires_liftgate,
                load_pallets=self.approximate_skids or 0,
                load_weight_lbs=self.total_weight_lbs or 0.0,
            )
        except Exception as exc:
            _logger.exception("Find Best Truck RPC failed for job %s", self.name)
            raise exceptions.UserError(f"Could not reach truck availability service: {exc}") from exc

        if result.get("error"):
            avail = result.get("availability", {})
            slots = avail.get("suggested_slots") or result.get("suggested_slots") or []
            slot_text = ""
            if slots:
                slot_text = "\n\nSuggested available windows:\n" + "\n".join(
                    f"  • {s.get('start','?')} – {s.get('end','?')}" for s in slots[:5]
                )
            raise exceptions.UserError(result["error"] + slot_text)

        best = result["selected_truck"]
        truck = self.env["fleet.vehicle"].browse(best["id"]).exists()
        if not truck:
            raise exceptions.UserError("Selected truck no longer exists in the system.")

        driver = truck.driver_id or truck.x_current_driver_contact_id
        self.write({
            "vehicle_id": truck.id,
            "driver_id": driver.id if driver else False,
        })

        # Build ranked summary for chatter
        candidates = result.get("truck_candidates", [best])
        lines = ["<b>Find Best Truck — results:</b><br/>"]
        for i, c in enumerate(candidates[:10], 1):
            badges = []
            if c.get("reefer"):
                badges.append("❄️ Reefer")
            if c.get("liftgate"):
                badges.append("🔧 Liftgate")
            cap = f"{c.get('max_pallets', 0)} skids / {c.get('max_payload_lbs', 0):.0f} lbs" if c.get("max_pallets") else ""
            dist = f"{c['distance_km']:.0f} km away" if c.get("distance_km") else "distance unknown"
            sel = " ← assigned" if i == 1 else ""
            lines.append(
                f"  {i}. <b>{c['name']}</b> ({c.get('license_plate','')}) "
                f"| {c['status']} | {dist} | {', '.join(badges) or 'standard'} | {cap}{sel}"
            )

        self.message_post(body="<br/>".join(lines))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Truck Assigned",
                "message": f"{truck.name} assigned to {self.name}.",
                "type": "success",
                "sticky": False,
            },
        }

    # ── Live Map data ─────────────────────────────────────────────

    @api.model
    def _user_today(self, user_tz=None):
        """"Today" in the given (or current user's) timezone, not the server
        process's local date. The Odoo service runs with a UTC system clock,
        so a bare date.today()/datetime.now() call rolls over to the next day
        hours before it's actually midnight in Toronto — this made the Driver
        App and Live Map show "no jobs" every evening once the server's UTC
        date had advanced past the user's real local date."""
        import pytz
        from datetime import datetime
        if user_tz is None:
            user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        return datetime.now(pytz.utc).astimezone(user_tz).date()

    @api.model
    def _stop_local_date(self, stop, job, user_tz):
        """The local calendar date a stop belongs to — same rule the Driver App
        uses (stop.scheduled_time if set, else job.scheduled_pickup), so the two
        views agree on which day a stop shows up on. Shared here rather than
        duplicated so a future change to the rule only has to happen once."""
        import pytz
        if stop.scheduled_time:
            return pytz.utc.localize(stop.scheduled_time).astimezone(user_tz).date()
        if job.scheduled_pickup:
            return pytz.utc.localize(job.scheduled_pickup).astimezone(user_tz).date()
        return None

    def _local_date_of(self, dt, user_tz=None):
        """Local calendar date of a naive-UTC Datetime field value (or None)."""
        if not dt:
            return None
        import pytz
        if user_tz is None:
            user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        return pytz.utc.localize(dt).astimezone(user_tz).date()

    def _local_date_time_to_utc(self, local_date, local_time, user_tz=None):
        """Combine a local calendar date + time into the naive-UTC datetime
        Odoo stores, so Pickup/Delivery window defaults land on the calendar
        day the dispatcher actually sees, not shifted by the UTC offset."""
        import pytz
        from datetime import datetime
        if user_tz is None:
            user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        return user_tz.localize(
            datetime.combine(local_date, local_time)
        ).astimezone(pytz.utc).replace(tzinfo=None)

    def _job_segments(self):
        """Split this job's stops into custody segments at each Driver
        Transfer / Cross-Dock Drop boundary, so a job that changes hands
        mid-route can be attributed to the RIGHT truck per stop instead of
        one single job.vehicle_id covering everything (which used to make
        an already-completed pickup appear to belong to whichever truck the
        job was LAST reassigned to).

        Returns a list of dicts: {"vehicle", "driver", "stops": [(stop, role)]}
        — one segment per leg, in stop order. A job with no transfer/cross-
        dock stop returns a single segment on job.vehicle_id/driver_id
        (today's plain behavior, unchanged). Each boundary stop appears at
        the END of one segment (role="giving") and the START of the next
        (role="receiving") — so it renders on both trucks' boards. Segment
        vehicle/driver resolve to the boundary's captured transfer_from_*/
        transfer_to_*  when set, falling back to this job's current
        vehicle_id/driver_id when a handoff hasn't actually happened yet
        (both sides then correctly collapse onto the same truck).
        """
        self.ensure_one()
        stops = self.stop_ids.sorted("sequence")
        boundaries = list(stops.filtered(
            lambda s: s.stop_type in ("transfer", "cross_dock_drop")
        ).sorted("sequence"))
        if not boundaries:
            return [{
                "vehicle": self.vehicle_id, "driver": self.driver_id,
                "stops": [(s, None) for s in stops],
            }]

        segments = []
        next_boundary = boundaries.pop(0)
        cur_vehicle = next_boundary.transfer_from_vehicle_id or self.vehicle_id
        cur_driver = next_boundary.transfer_from_driver_id or self.driver_id
        current = []
        for st in stops:
            role = "giving" if next_boundary and st.id == next_boundary.id else None
            current.append((st, role))
            if next_boundary and st.id == next_boundary.id:
                segments.append({"vehicle": cur_vehicle, "driver": cur_driver, "stops": current})
                cur_vehicle = next_boundary.transfer_to_vehicle_id or self.vehicle_id
                cur_driver = next_boundary.transfer_to_driver_id or self.driver_id
                current = [(st, "receiving")]
                next_boundary = boundaries.pop(0) if boundaries else None
        segments.append({"vehicle": cur_vehicle, "driver": cur_driver, "stops": current})
        return segments

    @api.model
    def get_live_map_data(self, date_str=None):
        """Return ALL fleet vehicles with GPS + that day's job stops for the Live Map.

        Defaults to today and only shows stops scheduled for the requested date —
        previously this showed every non-cancelled stop on every active job
        regardless of date, so a truck running a job three days from now
        appeared identical to one running today (audit item 20/36).
        """
        from datetime import date as _date, datetime, timezone
        import pytz

        google_api_key = self.env["ir.config_parameter"].sudo().get_param(
            "google_maps_api_key", ""
        )
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        check_d = _date.fromisoformat(date_str) if date_str else self._user_today(user_tz)

        # All active fleet vehicles
        all_vehicles = self.env["fleet.vehicle"].sudo().search([("active", "=", True)])

        # Active dispatch jobs indexed by vehicle, widened +/-2 days so multi-day
        # jobs are still found before we filter individual stops down to check_d.
        utc_start = user_tz.localize(datetime.combine(check_d, datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
        utc_end = user_tz.localize(datetime.combine(check_d, datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)
        from datetime import timedelta as _td
        active_jobs = self.search([
            ("stage_id.stage_type", "not in", ["cancelled", "completed"]),
            ("vehicle_id", "!=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start - _td(days=2)),
            ("scheduled_pickup", "<=", utc_end + _td(days=2)),
        ], limit=500)
        jobs_by_vehicle = {}
        for job in active_jobs:
            jobs_by_vehicle.setdefault(job.vehicle_id.id, []).append(job)

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        trucks = []

        for vehicle in all_vehicles:
            lat = vehicle.x_last_location_lat or 0.0
            lng = vehicle.x_last_location_lng or 0.0
            gps_at = vehicle.x_last_location_at
            gps_age_min = None
            if gps_at:
                try:
                    delta = now_utc - gps_at.replace(tzinfo=None)
                    gps_age_min = int(delta.total_seconds() // 60)
                except Exception:
                    pass

            driver = (
                vehicle.driver_id.name
                or vehicle.x_current_driver_contact_id.name
                or ""
            )

            # Find most relevant job for this vehicle on the requested date —
            # prefer the one scheduled for that day; fall back to whichever is
            # closest to it. (Previously picked veh_jobs[0] with no date
            # awareness at all, so a truck could show an "ongoing" job due
            # days from now.)
            veh_jobs = jobs_by_vehicle.get(vehicle.id, [])

            def _job_date_rank(j):
                if not j.scheduled_pickup:
                    return (2, 0)
                d = j.scheduled_pickup.date()
                return (0 if d == check_d else 1, abs((d - check_d).days))

            veh_jobs = sorted(veh_jobs, key=_job_date_rank)

            # Collect every job's stops for this truck/day FIRST, then sort
            # once across all of them by actual chronology — a truck with
            # two jobs interleaved (e.g. a cross-dock consolidation) must
            # show its stops in real route order, not job A's stops in full
            # followed by job B's, which is what looping job-by-job produces
            # regardless of scheduled_time.
            pending = []
            for job_rank, job in enumerate(veh_jobs):
                for stop in job.stop_ids.filtered(
                    lambda s: s.status != "cancelled"
                ):
                    if self._stop_local_date(stop, job, user_tz) != check_d:
                        continue
                    pending.append((job_rank, job, stop))
            pending.sort(key=lambda t: (
                t[2].scheduled_time or t[1].scheduled_pickup or datetime.max,
                t[0], t[2].sequence,
            ))

            stops = []
            for _job_rank, job, stop in pending:
                stop_lat = stop.pin_lat if stop.pin_set and stop.pin_lat else (stop.latitude or 0.0)
                stop_lng = stop.pin_lng if stop.pin_set and stop.pin_lng else (stop.longitude or 0.0)
                stops.append({
                    "id": stop.id,
                    "seq": stop.sequence // 10,
                    "type": stop.stop_type,
                    "address": stop.address or "",
                    "lat": stop_lat,
                    "lng": stop_lng,
                    "status": stop.status,
                    "job_id": job.id,
                    "job_name": job.name,
                })

            # Primary job for the truck card: the job that actually has a stop
            # showing on this date, not just the closest by pickup date.
            jobs_with_stops_today = {s["job_id"] for s in stops}
            primary_job = next(
                (j for j in veh_jobs if j.id in jobs_with_stops_today), None
            ) or (veh_jobs[0] if veh_jobs else None)
            if primary_job and not driver:
                driver = primary_job.driver_id.name if primary_job.driver_id else ""

            trucks.append({
                "id": vehicle.id,
                "name": vehicle.name or "",
                "license_plate": vehicle.license_plate or "",
                "driver": driver,
                "customer": primary_job.partner_id.name if primary_job and primary_job.partner_id else "",
                "lat": lat,
                "lng": lng,
                "gps_age_min": gps_age_min,
                "address": vehicle.x_last_location_address or "",
                "job_id": primary_job.id if primary_job else False,
                "job_name": primary_job.name if primary_job else "",
                "stops": stops,
                "active_job_count": len(veh_jobs),
            })

        # Sort: trucks with GPS first, then by name
        trucks.sort(key=lambda t: (0 if t["lat"] else 1, t["name"]))

        return {
            "trucks": trucks,
            "google_api_key": google_api_key,
            "date": check_d.isoformat(),
            "is_today": check_d == self._user_today(user_tz),
        }

    # ── Internal helpers ─────────────────────────────────────────

    @api.model
    def _post_timeline(self, job, event_type, notes=None, stop=None):
        """Create a timeline event for job. Safe to call from create/write."""
        try:
            self.env["prema.dispatch.timeline.event"].sudo().create({
                "job_id": job.id,
                "event_type": event_type,
                "notes": notes or False,
                "stop_id": stop.id if stop else False,
                "user_id": self.env.user.id,
            })
        except Exception:
            _logger.exception("Failed to post timeline event %s for job %s", event_type, job.name)

    def _check_all_stops_done(self):
        """Called by stops when they complete — triggers job completion check."""
        for job in self:
            if job.all_stops_completed and job.pod_complete:
                completed_stage = self.env["prema.dispatch.stage"].search(
                    [("is_completed", "=", True)],
                    limit=1, order="sequence asc",
                )
                if completed_stage and not job.stage_id.is_completed:
                    job.write({
                        "stage_id": completed_stage.id,
                        "completed_at": fields.Datetime.now(),
                    })
                    job.message_post(
                        body="All stops completed and POD received. Job automatically moved to Completed."
                    )
                    if job.invoice_id:
                        job._attach_documents_to_invoice()
                        job.invoice_id.message_post(body=job._build_completion_summary())
                        all_jobs = job.invoice_id.dispatch_job_ids
                        if all(j.stage_id.is_completed and j.pod_complete for j in all_jobs):
                            if job.invoice_id.state == "draft":
                                job._auto_post_invoice()

    def _check_all_stops_cancelled(self):
        """If every stop on a job ends up cancelled (e.g. dispatcher cancels
        the pickup and the linked delivery with it), cancel the job itself
        instead of leaving an empty active job on the board."""
        for job in self:
            if not job.stop_ids or job.stage_id.is_cancelled:
                continue
            if all(s.status == "cancelled" for s in job.stop_ids):
                cancelled_stage = self.env["prema.dispatch.stage"].search(
                    [("is_cancelled", "=", True)], limit=1, order="sequence asc",
                )
                if cancelled_stage:
                    job.write({"stage_id": cancelled_stage.id})
                    job.message_post(body="All stops cancelled — job automatically cancelled.")

    def _build_completion_summary(self):
        """Plain-language delivery summary logged to the linked invoice when
        a job completes (Booking Board 'Delivered' rows disappear from the
        live list, but the record lives on here)."""
        self.ensure_one()
        lines = [f"<b>Dispatch job {self.name} completed</b>"]
        if self.vehicle_id:
            lines.append(f"Truck: {self.vehicle_id.name}")
        if self.driver_id:
            lines.append(f"Driver: {self.driver_id.name}")
        import pytz
        for stop in self.stop_ids.sorted("sequence"):
            if stop.status == "cancelled":
                continue
            when = "—"
            if stop.actual_arrival_time:
                tz = pytz.timezone(stop.tz_name) if stop.tz_name else pytz.timezone("America/Toronto")
                local = pytz.utc.localize(stop.actual_arrival_time).astimezone(tz)
                when = local.strftime("%b %d, %I:%M %p").replace(" 0", " ")
            lines.append(f"{stop.stop_type.title()} — {stop.address or ''}: arrived {when}, status {stop.status}")
        return "<br/>".join(lines)

    def _attach_documents_to_invoice(self):
        """Collect all attachments from this job and its stops and link to the invoice."""
        if not self.invoice_id:
            return 0

        Att = self.env["ir.attachment"]
        # Attachments already on invoice (to avoid duplicates)
        existing_names = set(
            Att.search([
                ("res_model", "=", "account.move"),
                ("res_id", "=", self.invoice_id.id),
            ]).mapped("name")
        )

        job_ref = _safe_fname(self.name)
        inv_ref = _safe_fname(self.invoice_id.name or self.invoice_id.ref or "INV")
        attached = 0

        def _link_attachment(att, stop_seq=None, category="DOC"):
            nonlocal attached
            ext = att.name.rsplit(".", 1)[-1] if "." in att.name else "bin"
            stop_part = f"_STOP{stop_seq}" if stop_seq else ""
            new_name = f"{inv_ref}_{job_ref}{stop_part}_{category}.{ext}"
            if new_name in existing_names:
                return
            Att.create({
                "name": new_name,
                "res_model": "account.move",
                "res_id": self.invoice_id.id,
                "type": att.type,
                "datas": att.datas,
                "mimetype": att.mimetype,
            })
            existing_names.add(new_name)
            attached += 1

        for stop in self.stop_ids.sorted("sequence"):
            seq = stop.sequence // 10
            for att in stop.pod_attachment_ids:
                _link_attachment(att, seq, "POD")
            for i, att in enumerate(stop.photo_attachment_ids, 1):
                _link_attachment(att, seq, f"PHOTO{i}")
            for att in stop.document_attachment_ids:
                _link_attachment(att, seq, "DOC")

        if attached:
            pod_count = sum(len(s.pod_attachment_ids) for s in self.stop_ids)
            photo_count = sum(len(s.photo_attachment_ids) for s in self.stop_ids)
            self.invoice_id.message_post(
                body=(
                    f"<b>Dispatch job {self.name} completed.</b><br/>"
                    f"POD files: {pod_count} | Photos: {photo_count} | "
                    f"Total attached: {attached}"
                )
            )
        return attached

    def _auto_post_invoice(self):
        """Attempt to confirm/post the linked invoice."""
        try:
            self.invoice_id.action_post()
            self.write({"auto_posted_invoice": True})
            self.invoice_id.message_post(
                body=(
                    f"<b>Invoice auto-confirmed</b> after all dispatch jobs completed. "
                    f"Jobs: {', '.join(j.name for j in self.invoice_id.dispatch_job_ids)}"
                )
            )
        except Exception as exc:
            _logger.exception("Auto-post failed for invoice %s", self.invoice_id.name)
            self.write({"auto_post_error": str(exc)})
            self.invoice_id.message_post(
                body=(
                    f"<b>Auto-confirm failed.</b> Please confirm manually.<br/>"
                    f"Error: {exc}"
                )
            )

    # ── Driver App API ────────────────────────────────────────────

    @staticmethod
    def _dt_iso_utc(dt):
        """Naive Odoo Datetime fields are always UTC but isoformat() alone
        omits any offset, so a browser's `new Date(...)` would wrongly parse
        it as local time. Appending 'Z' makes every timestamp we hand to the
        frontend unambiguous UTC, so Intl.DateTimeFormat(...,{timeZone: tz})
        converts it to the stop's local time correctly."""
        return (dt.isoformat() + "Z") if dt else None

    @api.model
    def _stop_type_label(self, stop_type):
        return {
            "pickup": "Pickup",
            "dropoff": "Drop-Off",
            "return": "Return",
            "transfer": "Driver Transfer",
            "cross_dock_drop": "Cross-Dock Drop / Transfer-In",
            "cross_dock_pickup": "Cross-Dock Pickup / Transfer-Out",
        }.get(stop_type, "Stop")

    @api.model
    def _stop_company_name(self, stop):
        loc = stop.saved_location_id
        if loc:
            return loc.business_name or loc.name or loc.address or ""
        return (
            stop.contact_name
            or (stop.partner_id.name if stop.partner_id else "")
            or ((stop.address or "").split(",")[0].strip())
        )

    @api.model
    def _attachment_payloads(self, attachments):
        return [
            {"id": att.id, "name": att.name, "url": f"/web/content/{att.id}"}
            for att in attachments
        ]

    def _driver_stop_dict(self, s):
        """Serialize a dispatch stop for the driver app."""
        from odoo.addons.prema_dispatch.models.dispatch_stop import tz_from_longitude_band
        lat = s.pin_lat if s.pin_set and s.pin_lat else (s.latitude or 0)
        lng = s.pin_lng if s.pin_set and s.pin_lng else (s.longitude or 0)
        loc = s.saved_location_id
        vehicle = s.completed_vehicle_id or s.job_id.vehicle_id
        driver = s.completed_driver_id or s.job_id.driver_id
        freight_items = s.freight_item_ids or s._items_for_custody_transition()
        transit_evidence = freight_items.mapped("evidence_attachment_ids")
        entrance_photo_url = ""
        if loc and loc.entrance_photo:
            entrance_photo_url = f"/web/image/prema.dispatch.location/{loc.id}/entrance_photo"
        return {
            "id":                s.id,
            "vehicle_id":        vehicle.id if vehicle else False,
            "driver_id":         driver.id if driver else False,
            "company_name":      self._stop_company_name(s),
            "business_name":     (loc.business_name if loc else "") or "",
            "sequence":          s.sequence,
            "type":              s.stop_type,
            "type_label":        self._stop_type_label(s.stop_type),
            "status":            s.status,
            "address":           s.address or "",
            "partner":           s.partner_id.name if s.partner_id else "",
            "contact_name":      s.contact_name or "",
            "contact_phone":     s.contact_phone or "",
            "dock_door":         s.dock_door or (loc.dock_door if loc else "") or "",
            "lat":               lat,
            "lng":               lng,
            "pin_set":           s.pin_set,
            "chain_name":        (loc.chain_name if loc else "") or "",
            "location_number":   (loc.location_number if loc else "") or "",
            "effective_lat":      lat,
            "effective_lng":      lng,
            "pin_source":         (loc.pin_source if loc else "") or ("stop_exact" if s.pin_set else "geocoded_address"),
            "pin_accuracy_m":     (loc.pin_accuracy_m if loc else 0.0) or 0.0,
            "exact_pin_available": bool((loc and loc.pin_set) or s.pin_set),
            "exact_pin_verified": bool(loc and loc.verification_state == "verified" and loc.pin_set),
            "tz_name":           s.tz_name or tz_from_longitude_band(lat, lng),
            "parking_notes":     (loc.parking_notes if loc else "") or "",
            "entrance_photo_url": entrance_photo_url,
            "saved_location_id": loc.id if loc else False,
            "allow_cross_dock":  bool(loc.allow_cross_dock) if loc else False,
            "pallets_in":        s.pallets_in,
            "pallets_in_estimated": s.pallets_in_estimated,
            "pallets_out":       s.pallets_out,
            "onboard_after":     s.onboard_load_after_stop,
            "pod_required":      s.pod_required,
            "service_time_min":  s.service_time_minutes or 15,
            "address_warning":   s.address_validation_warning or "",
            "transfer_to_driver_id": s.transfer_to_driver_id.id if s.transfer_to_driver_id else False,
            "transfer_to_driver": s.transfer_to_driver_id.name if s.transfer_to_driver_id else "",
            "transfer_to_vehicle_id": s.transfer_to_vehicle_id.id if s.transfer_to_vehicle_id else False,
            "transfer_to_vehicle": s.transfer_to_vehicle_id.display_name if s.transfer_to_vehicle_id else "",
            "transfer_to_vehicle_plate": s.transfer_to_vehicle_id.license_plate if s.transfer_to_vehicle_id else "",
            "freight_item_summary": s.freight_item_summary or "",
            "freight_items": [
                {
                    "id": item.id,
                    "label": item.display_label(),
                    "pallet_count": item.pallet_count,
                    "status": item.status,
                    "custody": item.current_custody_type,
                }
                for item in freight_items
            ],
            "transit_evidence": self._attachment_payloads(transit_evidence),
            "scheduled_time":    self._dt_iso_utc(s.scheduled_time),
            "estimated_arrival": self._dt_iso_utc(s.estimated_arrival),
            "estimated_departure": self._dt_iso_utc(s.estimated_departure),
            "actual_arrival_time": self._dt_iso_utc(s.actual_arrival_time),
            "actual_departure_time": self._dt_iso_utc(s.actual_departure_time),
        }

    @api.model
    def _serialized_stop_sort_key(self, stop_dict):
        return (
            stop_dict.get("scheduled_time")
            or stop_dict.get("estimated_arrival")
            or stop_dict.get("actual_arrival_time")
            or "9999-12-31T23:59:59Z",
            stop_dict.get("sequence") or stop_dict.get("seq") or 0,
            stop_dict.get("id") or 0,
        )

    @api.model
    def _apply_truck_onboard_counts(self, stop_dicts):
        """Overlay a truck-level running onboard count onto serialized stop
        payloads so interleaved multi-job routes show the real load after each
        stop instead of each job's local subtotal."""
        grouped = {}
        for stop_dict in stop_dicts:
            grouped.setdefault(stop_dict.get("vehicle_id") or 0, []).append(stop_dict)

        for group in grouped.values():
            running = 0
            for stop_dict in sorted(group, key=self._serialized_stop_sort_key):
                stop_type = stop_dict.get("type") or stop_dict.get("stop_type")
                if stop_type in ("pickup", "cross_dock_pickup"):
                    running += int(stop_dict.get("pallets_in") or 0)
                elif stop_type in ("dropoff", "return", "cross_dock_drop", "transfer"):
                    running = max(0, running - int(stop_dict.get("pallets_out") or 0))
                stop_dict["onboard_after"] = running
        return stop_dicts

    @api.model
    def get_driver_stops_for_date(self, date_str=None):
        """Return a flat, time-sorted list of ALL stops across the driver's jobs
        for a given date.  This is the primary data feed for the Driver App stop view.

        A stop is included on a date when:
        - stop.scheduled_time falls on that date (precise scheduling), OR
        - stop has no scheduled_time but job.scheduled_pickup falls on that date.

        Returns a flat list of stop dicts (no job grouping), plus truck + driver info.
        """
        from datetime import date, datetime, timedelta
        import pytz

        user    = self.env.user
        partner = user.partner_id
        user_tz = pytz.timezone(user.tz or "America/Toronto")
        check_d = date.fromisoformat(date_str) if date_str else self._user_today(user_tz)

        def to_utc(d, t):
            return user_tz.localize(datetime.combine(d, t)).astimezone(pytz.utc).replace(tzinfo=None)

        # Fetch all active driver jobs (wider window to capture multi-day jobs)
        utc_start = to_utc(check_d - timedelta(days=2), datetime.min.time())
        utc_end   = to_utc(check_d + timedelta(days=2), datetime.max.time())

        jobs = self.env["prema.dispatch.job"].search([
            ("driver_id", "=", partner.id),
            ("stage_id.is_cancelled", "=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start),
            ("scheduled_pickup", "<=", utc_end),
        ])

        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        truck = jobs[0].vehicle_id if jobs else None

        def stop_date_local(s, job):
            """Get the local date this stop belongs to."""
            if s.scheduled_time:
                return pytz.utc.localize(s.scheduled_time).astimezone(user_tz).date()
            if job.scheduled_pickup:
                return pytz.utc.localize(job.scheduled_pickup).astimezone(user_tz).date()
            return None

        stops_out = []
        for job in jobs:
            for s in job.stop_ids.sorted("sequence"):
                sd = stop_date_local(s, job)
                if sd != check_d:
                    continue

                def att_list(atts):
                    return [{"id": a.id, "name": a.name,
                             "url": f"/web/content/{a.id}"} for a in atts]

                stop_dict = self._driver_stop_dict(s)
                stop_dict.update({
                    "city":            (s.address or "").split(",")[0].strip(),
                    "job_id":          job.id,
                    "job_name":        job.name,
                    "job_partner":     job.partner_id.name if job.partner_id else "",
                    "job_all_stops_completed": job.all_stops_completed,
                    "job_completed":   bool(job.stage_id.is_completed),
                    "pop_attachments": att_list(s.pop_attachment_ids),
                    "pod_attachments": att_list(s.pod_attachment_ids),
                })
                stops_out.append(stop_dict)

        # Sort by scheduled_time then sequence
        stops_out.sort(key=self._serialized_stop_sort_key)
        self._apply_truck_onboard_counts(stops_out)

        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        available_transfer_trucks = []
        for truck_data in DispatchAvailabilityService(self.env).get_truck_day_schedule(check_d):
            driver_name = truck_data.get("driver_name") or ""
            if not driver_name:
                continue
            available_transfer_trucks.append({
                "id": truck_data["truck_id"],
                "name": truck_data.get("name") or "",
                "plate": truck_data.get("license_plate") or "",
                "driver_id": truck_data.get("driver_id") or False,
                "driver_name": driver_name,
            })

        return {
            "date":        check_d.isoformat(),
            "is_today":    check_d == self._user_today(user_tz),
            "driver_name": partner.name,
            "google_api_key": api_key,
            "truck": {
                "id":    truck.id if truck else False,
                "name":  truck.name if truck else "",
                "plate": truck.license_plate if truck else "",
                "lat":   (truck.x_last_location_lat or 0) if truck else 0,
                "lng":   (truck.x_last_location_lng or 0) if truck else 0,
            } if truck else {},
            "available_transfer_trucks": available_transfer_trucks,
            "stops": stops_out,
        }

    @api.model
    def driver_add_evidence(self, stop_id, ev_type, data_b64, filename):
        """Add a POP or POD attachment to a stop.

        ev_type: 'pop' (proof of pickup) | 'pod' (proof of delivery)
        data_b64: base64-encoded file content
        filename: original filename

        Automatically also links the attachment to the job's invoice/quote.
        """
        import base64 as b64mod
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        from odoo.addons.prema_dispatch.services.dispatch_upload import (
            decode_and_validate, find_duplicate, UploadError,
        )
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "code": "record_not_found", "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "code": "unauthorized", "error": "Not authorized for this stop"}
        if stop.job_id.stage_id.is_cancelled:
            return {"success": False, "code": "unauthorized",
                    "error": "This job has been cancelled — evidence can no longer be added."}

        try:
            validated = decode_and_validate(data_b64, filename, category=ev_type)
        except UploadError as e:
            return {"success": False, "code": e.code, "error": e.message}

        field = "pop_attachment_ids" if ev_type == "pop" else "pod_attachment_ids"
        dup = find_duplicate(self.env, stop[field], validated["checksum_sha256"])
        if dup:
            return {
                "success": True, "duplicate": True,
                "id": dup.id, "existing_attachment_id": dup.id,
                "name": dup.name, "url": f"/web/content/{dup.id}",
                "message": "This file was already uploaded.",
            }

        try:
            att = self.env["ir.attachment"].create({
                "name":        validated["filename"],
                "type":        "binary",
                "datas":       b64mod.b64encode(validated["data"]),
                "res_model":   "prema.dispatch.stop",
                "res_id":      stop.id,
                "mimetype":    validated["mimetype"],
            })
            stop.write({field: [(4, att.id)]})
            linked_items = stop._items_for_custody_transition()
            if linked_items:
                linked_items.write({"evidence_attachment_ids": [(4, att.id)]})
            # Attach to the invoice linked to THIS stop if set (multi-invoice
            # consolidated jobs), otherwise fall back to the job's invoice/quote.
            # Tagged via description so driver_remove_evidence can find and
            # delete this copy too when the original is removed.
            job = stop.job_id
            target_invoice = stop.invoice_id or job.invoice_id
            tag = f"__evidence_source:{att.id}__"
            if target_invoice:
                att.copy({"res_model": "account.move", "res_id": target_invoice.id, "description": tag})
            elif job.sale_order_id:
                att.copy({"res_model": "sale.order", "res_id": job.sale_order_id.id, "description": tag})
            return {
                "success": True, "id": att.id, "name": att.name,
                "url": f"/web/content/{att.id}", "mimetype": att.mimetype,
                "checksum_sha256": validated["checksum_sha256"],
                "preview_available": validated["preview_available"],
            }
        except Exception:
            _logger.exception("driver_add_evidence failed")
            return {"success": False, "code": "upload_failed",
                    "error": "Could not save this upload. Please try again."}

    @api.model
    def driver_remove_evidence(self, stop_id, ev_type, att_id):
        """Remove a POP or POD attachment from a stop, and any copy of it
        already attached to an invoice/quotation (see driver_add_evidence)."""
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        field = "pop_attachment_ids" if ev_type == "pop" else "pod_attachment_ids"
        stop.write({field: [(3, att_id)]})
        stop.job_id.item_ids.filtered(
            lambda item: att_id in item.evidence_attachment_ids.ids
        ).write({"evidence_attachment_ids": [(3, att_id)]})
        tag = f"__evidence_source:{att_id}__"
        copies = self.env["ir.attachment"].search([("description", "=", tag)])
        copies.unlink()
        return {"success": True}

    @api.model
    def get_or_create_driver_channel(self):
        """Return (or create) the direct message channel between this driver and dispatchers."""
        user    = self.env.user
        partner = user.partner_id
        Channel = self.env["discuss.channel"]

        # Look for an existing channel named "Driver: {name}"
        ch_name = f"Driver: {partner.name}"
        channel = Channel.search([("name", "=", ch_name)], limit=1)
        if not channel:
            # Find dispatch managers to add as members
            manager_group = self.env.ref("prema_dispatch.group_dispatch_manager", raise_if_not_found=False)
            dispatcher_group = self.env.ref("prema_dispatch.group_dispatcher", raise_if_not_found=False)
            manager_partners = (
                (manager_group.users if manager_group else self.env["res.users"]) |
                (dispatcher_group.users if dispatcher_group else self.env["res.users"])
            ).mapped("partner_id").filtered(lambda p: p.id != partner.id)

            channel = Channel.create({
                "name":         ch_name,
                "channel_type": "channel",
                "description":  f"Direct communication with driver {partner.name}",
            })
            members = manager_partners | partner
            channel.add_members(members.ids)

        # Return channel info + last 30 messages
        messages = self.env["mail.message"].search(
            [("res_id", "=", channel.id), ("model", "=", "discuss.channel"),
             ("message_type", "in", ["comment", "email"])],
            order="date desc", limit=30
        )
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return {
            "channel_id": channel.id,
            "channel_name": ch_name,
            "messages": [{
                "id":     m.id,
                "author": m.author_id.name or "Unknown",
                "body":   m.body or "",
                "date":   m.date.isoformat() if m.date else "",
                "is_me":  m.author_id.id == partner.id,
            } for m in reversed(messages)],
        }

    def action_manage_chat_members(self):
        """Open the Invite/Remove Chat Members wizard for this job's driver."""
        self.ensure_one()
        if not self.driver_id:
            raise exceptions.UserError("Assign a driver first — there's no chat channel without one.")
        info = self.get_driver_channel_info()
        wizard = self.env["prema.dispatch.chat.invite.wizard"].create({
            "job_id": self.id,
            "channel_name": info.get("channel_name") or "",
            "current_member_ids": [(6, 0, [m["id"] for m in info.get("members", [])])],
        })
        return {
            "type": "ir.actions.act_window",
            "name": "Manage Driver Chat",
            "res_model": "prema.dispatch.chat.invite.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def get_driver_channel_info(self):
        """Dispatcher-side lookup of this job's driver channel (by job.driver_id,
        same channel naming/creation as get_or_create_driver_channel) — used by
        the Invite/Remove Chat Members wizard on the job form. The channel is
        per-driver (a running thread across all their jobs), not per-job; see
        invite_to_driver_channel for why this wasn't split per-job."""
        self.ensure_one()
        if not self.driver_id:
            return {"channel_id": False, "members": []}
        Channel = self.env["discuss.channel"]
        ch_name = f"Driver: {self.driver_id.name}"
        channel = Channel.search([("name", "=", ch_name)], limit=1)
        if not channel:
            return {"channel_id": False, "members": []}
        members = channel.channel_partner_ids
        return {
            "channel_id": channel.id,
            "channel_name": ch_name,
            "members": [{"id": p.id, "name": p.name} for p in members],
        }

    def invite_to_driver_channel(self, partner_ids):
        """Dispatcher/manager action: add extra users to this job's driver
        channel (item 26 — driver chat defaults to driver+dispatch only,
        dispatcher can invite others in)."""
        self.ensure_one()
        if not self.env.user.has_group("prema_dispatch.group_dispatcher") \
           and not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise exceptions.UserError("Only dispatchers/managers can invite users to a driver chat.")
        info = self.get_driver_channel_info()
        if not info["channel_id"]:
            raise exceptions.UserError("This driver has no chat channel yet — they need to open the Driver App chat once first.")
        self.env["discuss.channel"].browse(info["channel_id"]).add_members(partner_ids)
        return True

    def remove_from_driver_channel(self, partner_id):
        """Dispatcher/manager action: remove an invited user from this job's
        driver channel. Cannot remove the driver themselves."""
        self.ensure_one()
        if not self.env.user.has_group("prema_dispatch.group_dispatcher") \
           and not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise exceptions.UserError("Only dispatchers/managers can remove users from a driver chat.")
        if self.driver_id and partner_id == self.driver_id.id:
            raise exceptions.UserError("Can't remove the driver from their own chat.")
        info = self.get_driver_channel_info()
        if not info["channel_id"]:
            return True
        channel = self.env["discuss.channel"].browse(info["channel_id"])
        member = channel.channel_partner_ids.filtered(lambda p: p.id == partner_id)
        if member:
            channel._action_unfollow(member)
        return True

    @api.model
    def driver_send_message(self, channel_id, body):
        """Post a message to the driver channel."""
        channel = self.env["discuss.channel"].browse(channel_id)
        if not channel.exists():
            return {"success": False, "error": "Channel not found"}
        channel.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_comment")
        return {"success": True}

    @api.model
    def get_driver_available_dates(self, week_offset=0):
        """Return all 7 days of the requested week + job counts per day.

        week_offset = 0 → current week (Mon-Sun)
        week_offset = -1 → last week
        week_offset = +1 → next week

        Always returns exactly 7 entries (Mon-Sun) so the calendar is
        always a full week regardless of whether jobs exist.
        """
        from datetime import date, datetime, timedelta
        import pytz

        user    = self.env.user
        partner = user.partner_id
        user_tz = pytz.timezone(user.tz or "America/Toronto")
        today   = self._user_today(user_tz)

        # Find Monday of the target week
        mon = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
        sun = mon + timedelta(days=6)

        # Fetch all non-cancelled driver jobs in this week window
        def to_utc(d, t): return user_tz.localize(datetime.combine(d, t)).astimezone(pytz.utc).replace(tzinfo=None)
        utc_start = to_utc(mon, datetime.min.time())
        utc_end   = to_utc(sun, datetime.max.time())

        all_jobs = self.env["prema.dispatch.job"].search([
            ("driver_id", "=", partner.id),
            ("stage_id.is_cancelled", "=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start),
            ("scheduled_pickup", "<=", utc_end),
        ])

        dates_map = {}
        for job in all_jobs:
            if not job.scheduled_pickup:
                continue
            ld = pytz.utc.localize(job.scheduled_pickup).astimezone(user_tz).date().isoformat()
            if ld not in dates_map:
                dates_map[ld] = {"total": 0, "active": 0}
            dates_map[ld]["total"] += 1
            if not job.stage_id.is_completed:
                dates_map[ld]["active"] += 1

        result = []
        for i in range(7):
            d = mon + timedelta(days=i)
            d_str = d.isoformat()
            info  = dates_map.get(d_str, {"total": 0, "active": 0})
            result.append({
                "date":       d_str,
                "weekday":    d.strftime("%a"),
                "day_num":    d.strftime("%d"),
                "month":      d.strftime("%b"),
                "is_today":   d == today,
                "is_past":    d < today,
                "job_count":  info["total"],
                "has_active": info["active"] > 0,
                "all_done":   info["total"] > 0 and info["active"] == 0,
            })

        return {
            "days":         result,
            "week_start":   mon.isoformat(),
            "week_label":   f"{mon.strftime('%b %d')} – {sun.strftime('%b %d, %Y')}",
            "week_offset":  week_offset,
            "today":        today.isoformat(),
        }

    @api.model
    def get_driver_today_jobs(self, date_str=None):
        """Return dispatched jobs for the logged-in driver on a given date (default today).

        Called by the Driver App on page load or when driver switches days.
        """
        from datetime import date, datetime, timedelta
        import pytz

        user    = self.env.user
        partner = user.partner_id
        user_tz = pytz.timezone(user.tz or "America/Toronto")

        if date_str:
            check_date = date.fromisoformat(date_str)
        else:
            check_date = self._user_today(user_tz)

        local_start = user_tz.localize(datetime.combine(check_date, datetime.min.time()))
        local_end   = user_tz.localize(datetime.combine(check_date, datetime.max.time()))
        utc_start   = local_start.astimezone(pytz.utc).replace(tzinfo=None)
        utc_end     = local_end.astimezone(pytz.utc).replace(tzinfo=None)

        jobs = self.env["prema.dispatch.job"].search([
            ("driver_id", "=", partner.id),
            ("stage_id.is_cancelled", "=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start),
            ("scheduled_pickup", "<=", utc_end),
        ])

        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        truck = jobs[0].vehicle_id if jobs else None

        result = {
            "driver_name": partner.name,
            "date":        check_date.isoformat(),
            "is_today":    check_date == self._user_today(user_tz),
            "google_api_key": api_key,
            "truck": {
                "name":  truck.name if truck else "",
                "plate": truck.license_plate if truck else "",
                "lat":   (truck.x_last_location_lat or 0) if truck else 0,
                "lng":   (truck.x_last_location_lng or 0) if truck else 0,
            } if truck else {},
            "jobs": [],
        }

        all_stops = []
        for job in jobs:
            stops_out = [self._driver_stop_dict(s) for s in job.stop_ids.sorted("sequence")]
            for stop_dict in stops_out:
                stop_dict["job_id"] = job.id
                all_stops.append(stop_dict)
            result["jobs"].append({
                "id":      job.id,
                "name":    job.name,
                "partner": job.partner_id.name if job.partner_id else "",
                "route":   f"{job.pickup_city} → {job.delivery_cities}" if job.pickup_city else job.name,
                "pallets": job.max_onboard_pallets or job.approximate_skids or 0,
                "stops":   stops_out,
                **job._driver_job_summary(),
            })
        self._apply_truck_onboard_counts(all_stops)

        return result

    @api.model
    def geocode_stops_for_date(self, date_str=None):
        """Geocode all stops for the given date that are missing lat/lng.
        Called by the dispatch board when a truck is selected.
        Returns count of stops geocoded.
        """
        from datetime import date, datetime, timedelta
        import pytz
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        check_d = date.fromisoformat(date_str) if date_str else self._user_today(user_tz)
        utc_start = user_tz.localize(datetime.combine(check_d, datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
        utc_end   = user_tz.localize(datetime.combine(check_d, datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)

        jobs = self.env["prema.dispatch.job"].search([
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
            "|", ("scheduled_pickup", "=", False),
            "&", ("scheduled_pickup", ">=", utc_start), ("scheduled_pickup", "<=", utc_end),
        ])
        stops = jobs.mapped("stop_ids").filtered(lambda s: s.address and not (s.latitude and s.longitude))
        count = 0
        for s in stops[:20]:   # limit to 20 per call to stay within rate limits
            s._geocode_address()
            count += 1
        return {"geocoded": count}

    @api.model
    def get_weather_for_location(self, lat, lng):
        """Proxy Google's Weather API server-side (keeps the key off the
        client for this REST call, same pattern as geocoding/validation).
        Returns {} on any failure — weather is informational, never blocking.
        """
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        if not api_key or not lat or not lng:
            return {}
        try:
            import requests
            r = requests.get(
                "https://weather.googleapis.com/v1/currentConditions:lookup",
                params={"key": api_key, "location.latitude": lat, "location.longitude": lng},
                timeout=5,
            )
            data = r.json()
            cond = data.get("weatherCondition", {})
            if not cond:
                return {}
            return {
                "description":    cond.get("description", {}).get("text", ""),
                "icon_url":       f"{cond['iconBaseUri']}.png" if cond.get("iconBaseUri") else "",
                "temp_c":         data.get("temperature", {}).get("degrees"),
                "precip_percent": data.get("precipitation", {}).get("probability", {}).get("percent"),
                "wind_kph":       data.get("wind", {}).get("speed", {}).get("value"),
            }
        except Exception:
            _logger.exception("Weather lookup failed for %s,%s", lat, lng)
            return {}

    @api.model
    def driver_delete_stop(self, stop_id):
        """Delete a pending stop. Refuses if stop is already started."""
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        if stop.status not in ("pending", "cancelled"):
            return {"success": False, "error": "Only pending stops can be deleted"}
        job_id = stop.job_id.id
        stop.unlink()
        return {"success": True, "job_id": job_id}

    @api.model
    def driver_update_service_time(self, stop_id, minutes):
        """Update the loading/unloading service time for a stop."""
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        stop.write({"service_time_minutes": max(5, min(120, int(minutes)))})
        return {"success": True, "minutes": stop.service_time_minutes}

    @api.model
    def regeocode_stop(self, stop_id):
        """Force re-geocode a stop's address and reset the pin to that location.

        Used by the "Use Address" button in the pin editor to undo a bad
        manual pin placement, or to retry after a geocoding outage is fixed.
        """
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        ok = stop._geocode_address(force=True)
        if not ok:
            return {"success": False, "error": (
                "Could not geocode this address. The Geocoding API may not be "
                "enabled on the Google Cloud project for this API key."
            )}
        return {"success": True, "lat": stop.latitude, "lng": stop.longitude}

    @api.model
    def driver_reorder_stops(self, job_id, stop_order):
        """Reorder stops for a job. stop_order is a list of stop IDs in the desired sequence.

        Also re-estimates the route once the new sequence is written — the
        Planner's timeline bar (block width/position, per-stop markers) is
        positioned from each stop's estimated_arrival/estimated_departure,
        which were computed for the OLD stop order. Without recomputing them
        here, the reorder itself succeeds (sequence is updated) but the
        Planner keeps showing the truck's timeline laid out for the old
        order until someone manually re-estimates the route.
        """
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_job_access
        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        if not check_job_access(self.env, job, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this job"}
        try:
            for seq, stop_id in enumerate(stop_order, start=10):
                stop = self.env["prema.dispatch.stop"].browse(stop_id)
                if stop.exists() and stop.job_id.id == job_id:
                    stop.write({"sequence": seq * 10})
            if job.route_estimated_at:
                from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
                try:
                    DispatchRouteService(self.env).estimate_job_route(job)
                    job.write({"route_estimated_at": fields.Datetime.now()})
                except Exception:
                    _logger.exception(
                        "driver_reorder_stops: route re-estimate failed for job %s, "
                        "sequence was still updated", job_id
                    )
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @api.model
    def driver_reorder_stops_for_truck(self, stop_order):
        """Reorder stops across a truck's whole stop list, which can span
        several jobs at once (the Planner's per-truck stop panel). Unlike
        driver_reorder_stops() above — which is correct for its own use case,
        a driver reordering stops within their OWN single job — this assigns
        one strictly-increasing sequence across every stop in stop_order
        regardless of which job it belongs to. Calling the per-job method for
        each job separately here used to renumber each job's own stops back
        into the same fixed low range (10,20,30...) on every drop, so a stop
        could never actually be dragged past another job's stops sharing the
        same truck — it always snapped back to wherever its own job's range put it.
        """
        stops = self.env["prema.dispatch.stop"].browse(stop_order).exists()
        by_id = {s.id: s for s in stops}
        if not by_id:
            return {"success": False, "error": "No valid stops"}
        touched_jobs = self.env["prema.dispatch.job"]
        try:
            for i, stop_id in enumerate(stop_order, start=1):
                stop = by_id.get(stop_id)
                if stop:
                    stop.write({"sequence": i * 10})
                    touched_jobs |= stop.job_id
            from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService
            for job in touched_jobs.filtered("route_estimated_at"):
                try:
                    DispatchRouteService(self.env).estimate_job_route(job)
                    job.write({"route_estimated_at": fields.Datetime.now()})
                except Exception:
                    _logger.exception(
                        "driver_reorder_stops_for_truck: route re-estimate failed for job %s, "
                        "sequence was still updated", job.id
                    )
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @api.model
    def driver_update_stop(self, stop_id, action, data=None):
        """Driver App: update a stop's status or pin.

        action: 'arrived' | 'completed' | 'delayed' | 'en_route' | 'update_pin'
        data:   dict with optional keys: lat, lng, notes, delay_reason
        """
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this stop"}
        data = data or {}

        try:
            if action == "arrived":
                stop.action_mark_arrived()
                stop.write({
                    "gps_stamp_lat":  data.get("lat", 0),
                    "gps_stamp_lng":  data.get("lng", 0),
                    "gps_stamp_time": fields.Datetime.now(),
                })
            elif action == "completed":
                stop.action_mark_completed()
            elif action in ("delayed", "issue"):
                stop.write({"status": "issue"})
                if data.get("delay_reason"):
                    stop.job_id.message_post(
                        body=f"<b>Stop delayed:</b> {stop.address} — {data['delay_reason']}"
                    )
            elif action == "en_route":
                stop.write({"status": "en_route"})
            elif action == "update_pin":
                lat = data.get("lat")
                lng = data.get("lng")
                if lat is None or lng is None:
                    return {"success": False, "error": "lat/lng required"}
                stop.write({
                    "pin_lat": lat,
                    "pin_lng": lng,
                    "pin_set": True,
                })
                # Update saved location if linked
                if stop.saved_location_id:
                    stop.saved_location_id.update_pin(lat, lng, source="driver")
                else:
                    # Auto-link / create saved location
                    loc = self.env["prema.dispatch.location"].find_or_create_by_address(
                        stop.address
                    )
                    if loc:
                        loc.update_pin(lat, lng, source="driver")
                        stop.saved_location_id = loc.id
            elif action == "restore":
                stop.action_restore_stop()
            elif action == "assign_receiving_truck":
                result = stop.action_assign_receiving_truck(
                    data.get("vehicle_id") or False,
                    bool(data.get("stage_unassigned")),
                )
                result.setdefault("status", stop.status)
                result.setdefault("stop_id", stop_id)
                return result
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            return {"success": True, "stop_id": stop_id, "status": stop.status}
        except Exception as exc:
            _logger.exception("driver_update_stop failed for stop %s action %s", stop_id, action)
            return {"success": False, "error": str(exc)}

    @api.model
    def driver_finish_job(self, job_id):
        """Driver App: explicit "Job Finished" tap once every stop on a job
        is done. The job actually auto-completes via
        _check_all_stops_done() as soon as its last stop is marked
        completed — this gives the driver a visible confirmation of that
        (and a clear reason if it *hasn't* auto-completed yet, e.g. a
        missing POD) instead of the completion happening silently."""
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_job_access
        job = self.browse(job_id)
        if not job.exists():
            return {"success": False, "error": "Job not found"}
        if not check_job_access(self.env, job, raise_on_fail=False):
            return {"success": False, "error": "Not authorized for this job"}

        pending = job.stop_ids.filtered(lambda s: s.status not in ("completed", "skipped"))
        if pending:
            return {
                "success": False,
                "error": "Still open: " + ", ".join(pending.mapped(lambda s: s.address or s.stop_type)),
            }

        missing_pod = job.stop_ids.filtered(lambda s: s.pod_required and not s.pod_uploaded)
        if missing_pod:
            return {
                "success": False,
                "error": "POD still needed at: " + ", ".join(missing_pod.mapped(lambda s: s.address or s.stop_type)),
            }

        if not job.stage_id.is_completed:
            job._check_all_stops_done()

        return {
            "success": True,
            "job_id": job.id,
            "job_name": job.name,
            "completed": bool(job.stage_id.is_completed),
        }

    @api.model
    def driver_upload_entrance_photo(self, stop_id, image_b64, filename="entrance.jpg"):
        """Driver/dispatcher uploads entrance photo for a stop's saved location."""
        import base64 as b64mod
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        from odoo.addons.prema_dispatch.services.dispatch_upload import decode_and_validate, UploadError
        stop = self.env["prema.dispatch.stop"].browse(stop_id)
        if not stop.exists():
            return {"success": False, "code": "record_not_found", "error": "Stop not found"}
        if not check_stop_access(self.env, stop, raise_on_fail=False):
            return {"success": False, "code": "unauthorized", "error": "Not authorized for this stop"}

        try:
            validated = decode_and_validate(image_b64, filename, category="entrance_photo")
        except UploadError as e:
            return {"success": False, "code": e.code, "error": e.message}

        try:
            loc = stop.saved_location_id
            if not loc:
                loc = self.env["prema.dispatch.location"].find_or_create_by_address(
                    stop.address
                )
                if loc:
                    stop.saved_location_id = loc.id
            if loc:
                loc.write({
                    "entrance_photo": b64mod.b64encode(validated["data"]),
                    "entrance_photo_fname": validated["filename"],
                })
                return {
                    "success": True, "location_id": loc.id, "mimetype": validated["mimetype"],
                    "checksum_sha256": validated["checksum_sha256"],
                    "preview_available": validated["preview_available"],
                }
            return {"success": False, "code": "record_not_found", "error": "Could not find/create saved location"}
        except Exception:
            _logger.exception("driver_upload_entrance_photo failed for stop %s", stop_id)
            return {"success": False, "code": "upload_failed", "error": "Could not save this photo. Please try again."}

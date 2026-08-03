"""Operating Corridor — THE single source of truth for operational routes.

This model absorbs the previously-separate logistics.route.template and
logistics.route.run concepts (now deprecated). A corridor defines the ordered
stop sequence, recurrence rules, and scheduled departures in ONE place.

@deprecated models replaced by this one:
    - logistics.route.template  → corridor fields (phase, truck_slot, weekday, etc.)
    - logistics.route.run       → logistics.corridor.departure
"""

import logging as _logging
import datetime
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

WEEKDAY_SELECTION = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"),
    ("3", "Thursday"), ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
]


class LogisticsCorridor(models.Model):
    _name = "logistics.corridor"
    _description = "Operating Corridor (ordered truck route) — single source of truth"
    _order = "name"

    name = fields.Char(required=True)
    direction = fields.Selection([
        ("eastbound", "Eastbound"), ("westbound", "Westbound"),
        ("northbound", "Northbound"), ("southbound", "Southbound"),
        ("bidirectional", "Bidirectional"), ("local_loop", "Local Loop"),
        ("round_trip", "Round Trip"),
    ], required=True)
    equipment_type = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry", required=True)
    active = fields.Boolean(default=True)

    # ── Absorbed from logistics.route.template ──────────────────────
    phase = fields.Integer(string="Network Phase", default=1,
                           help="Phase 1-4. Corridor activates when fleet reaches this phase.")
    truck_slot = fields.Integer(string="Truck Slot", default=1,
                                help="Which truck in the fleet this corridor belongs to (1-4).")
    default_vehicle_id = fields.Many2one("fleet.vehicle", string="Default Truck",
                                          help="Default truck assigned to this weekly service. "
                                               "Copied to new departures on generation.")
    default_driver_id = fields.Many2one("res.partner", string="Default Driver",
                                         help="Default driver for this weekly service.")
    weekday = fields.Selection(WEEKDAY_SELECTION, string="Primary Operating Day",
                               help="Primary day this corridor operates. For multi-day, see departure schedule.")
    recurring_weekdays = fields.Char(string="Recurring Weekdays",
                                     help="Comma-separated weekday numbers (0=Mon...6=Sun) for recurring operation.")
    start_time = fields.Float(string="Start Time", default=7.0, help="24h float, e.g. 7.0 = 7:00 AM")
    overnight = fields.Boolean(string="Overnight", help="Driver rests overnight before return.")
    conditional = fields.Boolean(string="Conditional",
                                 help="Only dispatched if minimum revenue or bookings met.")
    min_departure_revenue = fields.Float(string="Min Departure Revenue", default=0.0,
                                         help="If conditional, the minimum booked revenue to dispatch.")
    temperature_capability = fields.Selection([
        ("dry", "Dry Only"), ("chilled", "Dry + Chilled"), ("all", "Dry + Chilled + Frozen"),
    ], default="all")

    # ── Hub linkage ──────────────────────────────────────────────────
    # NEW: canonical hub references (M2o logistics.hub)
    origin_hub_id = fields.Many2one(
        "logistics.hub", string="Origin Hub", index=True,
        help="Departure hub for this weekly service. Replaces start_hub_id."
    )
    destination_hub_id = fields.Many2one(
        "logistics.hub", string="Destination Hub",
        help="Arrival hub for this weekly service. Replaces end_hub_id."
    )
    transfer_hub_id = fields.Many2one(
        "logistics.hub", string="Transfer Hub",
        help="Intermediate transfer hub. Replaces via_hub_id."
    )
    same_day_return = fields.Boolean(
        string="Same-Day Return",
        help="Vehicle returns to origin hub on the same operating day."
    )
    paired_return_service_id = fields.Many2one(
        "logistics.corridor", string="Paired Return Service",
        help="The return-direction weekly service paired with this outbound service."
    )

    # DEPRECATED: legacy region-valued hub references — kept for migration,
    # superseded by origin_hub_id / destination_hub_id / transfer_hub_id.
    start_hub_id = fields.Many2one("logistics.region", string="Start Region (deprecated)", index=True)
    end_hub_id = fields.Many2one("logistics.region", string="End Region (deprecated)")
    via_hub_id = fields.Many2one("logistics.region", string="Via Region (deprecated)")

    # ── Lane linkage (Phase 2) ──────────────────────────────────────
    lane_ids = fields.Many2many("logistics.lane", "corridor_lane_rel",
                                "corridor_id", "lane_id", string="Lanes Served")

    # ── Round-trip pairing (Phase 12) ───────────────────────────────
    return_corridor_id = fields.Many2one("logistics.corridor", string="Return Corridor",
                                          help="The return/backhaul corridor paired with this outbound corridor.")
    feeds_corridor_id = fields.Many2one("logistics.corridor", string="Feeds Corridor",
                                         help="Local ops corridor that feeds freight into this corridor (Phase 13).")

    # ── Distance & revenue ──────────────────────────────────────────
    full_distance_km = fields.Float(string="Full Corridor Distance (km)")
    full_revenue_target = fields.Float(string="Full-Corridor Revenue Target")
    planned_pallets = fields.Integer(string="Planned Pallets", default=8)
    truck_capacity = fields.Integer(string="Truck Capacity", default=12)

    # ── Equipment (absorbed from route.template) ────────────────────
    equipment_profile_id = fields.Many2one(
        "logistics.equipment.profile", string="Equipment Requirement",
        domain="[('is_requirement_class', '=', True)]",
    )

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    departure_ids = fields.One2many("logistics.corridor.departure", "corridor_id")
    stop_ids = fields.One2many("logistics.corridor.stop", "corridor_id")

    @api.model
    def generate_segment_rates(self, corridor_id, preview=True):
        corridor = self.browse(corridor_id)
        if not corridor.exists():
            return {"error": "Corridor not found"}
        stops = corridor.stop_ids.sorted("sequence")
        segments = []
        for i, orig in enumerate(stops):
            if not orig.pickup_allowed:
                continue
            for j, dest in enumerate(stops):
                if j <= i or not dest.delivery_allowed:
                    continue
                d = dest.distance_from_origin_km - orig.distance_from_origin_km
                if d <= 0:
                    continue
                ratio = d / max(corridor.full_distance_km or 1, 1)
                target = round(corridor.full_revenue_target * ratio, 2)
                pp = corridor.planned_pallets or 8
                segments.append({
                    "origin_region": orig.region_id.name or str(orig.sequence),
                    "dest_region": dest.region_id.name or str(dest.sequence),
                    "origin_seq": orig.sequence, "dest_seq": dest.sequence,
                    "distance_km": d, "revenue_target": target,
                    "planned_pallets": pp, "price_per_pallet": round(target / pp, 2),
                })
        return {"corridor": corridor.name, "full_distance": corridor.full_distance_km,
                "full_target": corridor.full_revenue_target, "segments": segments, "count": len(segments)}


class LogisticsCorridorStop(models.Model):
    _name = "logistics.corridor.stop"
    _description = "Corridor Stop"
    _order = "corridor_id, sequence"

    corridor_id = fields.Many2one("logistics.corridor", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, default=10)
    region_id = fields.Many2one("logistics.region", string="Region")
    saved_location_id = fields.Many2one("prema.dispatch.location", string="Hub/Depot")
    name = fields.Char()
    pickup_allowed = fields.Boolean(default=True)
    delivery_allowed = fields.Boolean(default=True)
    planned_arrival_time = fields.Float()
    planned_departure_time = fields.Float()
    day_offset = fields.Integer(default=0)
    distance_from_origin_km = fields.Float()
    active = fields.Boolean(default=True)


class LogisticsCorridorDeparture(models.Model):
    _name = "logistics.corridor.departure"
    _description = "Scheduled Corridor Departure"
    _order = "departure_date, departure_time"

    @api.model
    def _maintain_departure_horizon(self):
        """Daily cron: maintain a 12-week rolling departure horizon.
        Idempotent — uses (corridor_id, departure_date) as business key.
        """
        try:
            from odoo.addons.prema_logistics_booking.scripts.generate_phase1_departures import (
                generate_phase1_departures,
            )
        except ImportError:
            _logger = _logging.getLogger(__name__)
            _logger.warning("Departure horizon generator not available")
            return

        _logger = _logging.getLogger(__name__)
        try:
            result = generate_phase1_departures(self.env, weeks=12)
            _logger.info(
                "Departure horizon: created=%d skipped=%d over %d weeks",
                result["created"], result["skipped"], result["weeks"],
            )
        except Exception:
            _logger.exception("Departure horizon cron failed")
            raise

    corridor_id = fields.Many2one("logistics.corridor", required=True, ondelete="cascade", index=True)
    name = fields.Char(compute="_compute_name", store=True)
    departure_date = fields.Date(required=True)
    departure_time = fields.Float(help="e.g. 1.0 = 01:00 AM")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    driver_id = fields.Many2one("res.partner", string="Driver")
    active = fields.Boolean(default=True)
    status = fields.Selection([
        ("scheduled", "Scheduled"), ("departed", "Departed"),
        ("in_transit", "In Transit"), ("completed", "Completed"),
        ("cancelled", "Cancelled"), ("delayed", "Delayed"),
    ], default="scheduled")
    peak_pallets = fields.Integer(default=0)
    total_handled_pallets = fields.Integer(default=0)

    # ── Phase 8: Leg-segment computed capacity ───────────────────────
    computed_peak_pallets = fields.Integer(
        string="Peak Pallets (Computed)", compute="_compute_leg_capacity", store=False,
        help="Peak pallets across all corridor segments, computed from confirmed bookings."
    )
    computed_peak_weight = fields.Float(
        string="Peak Weight lbs (Computed)", compute="_compute_leg_capacity", store=False,
        help="Peak weight across all corridor segments, computed from confirmed bookings."
    )
    computed_total_handled = fields.Integer(
        string="Total Handled Pallets", compute="_compute_leg_capacity", store=False,
        help="Sum of all booking pallets handled across the entire corridor."
    )

    max_capacity = fields.Integer(string="Truck Capacity", default=12)
    service_offering_id = fields.Many2one("logistics.service.offering")
    cutoff_time = fields.Float()

    def _compute_leg_capacity(self):
        """Compute leg-segment peak capacity for each departure."""
        for dep in self:
            try:
                from ..services.capacity_engine import CapacityEngine
                engine = CapacityEngine(self.env)
                result = engine.compute_departure_peak(dep)
                dep.computed_peak_pallets = result["peak_pallets"]
                dep.computed_peak_weight = result["peak_weight"]
                dep.computed_total_handled = result["total_handled"]
            except Exception:
                dep.computed_peak_pallets = 0
                dep.computed_peak_weight = 0.0
                dep.computed_total_handled = 0

    @api.depends("corridor_id", "departure_date")
    def _compute_name(self):
        for r in self:
            cn = r.corridor_id.name if r.corridor_id else ""
            d = r.departure_date.strftime("%a %b %d") if r.departure_date else ""
            r.name = f"{cn} — {d}" if cn else d

    @api.model
    def get_available_trucks(self):
        """Return list of active operational trucks for the truck selector.
        Excludes non-operational vehicles like DEMO-01."""
        vehicles = self.env["fleet.vehicle"].sudo().search([
            ("active", "=", True),
            ("x_operational_logistics", "=", True),
        ])
        return [{"id": v.id, "name": v.name or v.license_plate or f"Truck {v.id}"} for v in vehicles]

    @api.model
    def get_available_corridors(self):
        """Return list of active corridors for the + lane picker."""
        corridors = self.env["logistics.corridor"].sudo().search([("active", "=", True)])
        return [{"id": c.id, "name": c.name, "direction": c.direction, "equipment_type": c.equipment_type} for c in corridors]

    @api.model
    def add_departure(self, corridor_id, departure_date, vehicle_id, departure_time=1.0, cutoff_time=16.0):
        """Add a new scheduled departure. Returns the created record.
        Requires Dispatcher or Logistics Manager group."""
        if not self.env.user.has_group("prema_dispatch.group_dispatcher") and \
           not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise AccessError(_("Only dispatchers and logistics managers can manage departures."))
        dep = self.sudo().create({
            "corridor_id": corridor_id,
            "departure_date": departure_date,
            "departure_time": departure_time,
            "vehicle_id": vehicle_id,
            "cutoff_time": cutoff_time,
            "status": "scheduled",
            "max_capacity": 13,
        })
        return {"id": dep.id, "name": dep.name}

    @api.model
    def remove_departure(self, departure_id):
        """Remove a scheduled departure.
        Requires Dispatcher or Logistics Manager group."""
        if not self.env.user.has_group("prema_dispatch.group_dispatcher") and \
           not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise AccessError(_("Only dispatchers and logistics managers can manage departures."))
        dep = self.sudo().browse(departure_id)
        if dep.exists():
            dep.write({"active": False})
            return {"success": True}
        return {"success": False, "error": "Not found"}

    @api.model
    def get_weekly_board_data(self, vehicle_id=None):
        """RPC for the weekly schedule board. Filter by vehicle if provided."""
        today = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
        sunday = monday + datetime.timedelta(days=6)
        dn = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        wd = [{"date": (monday + datetime.timedelta(days=i)).isoformat(), "label": dn[i]} for i in range(7)]
        domain = [("active","=",True),("departure_date",">=",monday),("departure_date","<=",sunday)]
        if vehicle_id:
            domain.append(("vehicle_id","=",int(vehicle_id)))
        deps = self.search(domain)

        # Batch-fetch booking data for all departures in this week
        departure_ids = deps.ids
        bookings = self.env["logistics.booking"].sudo().search([
            ("departure_id", "in", departure_ids),
            ("state", "not in", ["cancelled"]),
        ]) if departure_ids else self.env["logistics.booking"].sudo().browse()

        # Index bookings by departure_id for O(1) lookup
        bookings_by_dep = {}
        for bk in bookings:
            dep_id = bk.departure_id.id
            if dep_id not in bookings_by_dep:
                bookings_by_dep[dep_id] = []
            bookings_by_dep[dep_id].append(bk)

        dc = {str(i): [] for i in range(7)}
        for dep in deps:
            di = (dep.departure_date - monday).days
            if di < 0 or di > 6:
                continue
            dd = dep.departure_date
            s, sl = "scheduled", "SCHEDULED"
            if dd < today: s, sl = "completed", "COMPLETED"
            elif dd == today: s, sl = "ontime", "ON TIME"
            if dep.status == "cancelled": s, sl = "cancelled", "CANCELLED"
            elif dep.status == "delayed": s, sl = "delayed", "DELAYED"
            cor = dep.corridor_id
            cutoff = f"{int(dep.cutoff_time):02d}:{int((dep.cutoff_time%1)*60):02d}" if dep.cutoff_time else ""
            dep_str = f"{dn[di]} {int(dep.departure_time):02d}:{int((dep.departure_time%1)*60):02d}" if dep.departure_time else ""
            cd = ""
            if dd == today and dep.departure_time:
                now = datetime.datetime.now()
                dt = now.replace(hour=int(dep.departure_time), minute=int((dep.departure_time%1)*60), second=0)
                diff = (dt - now).total_seconds()
                if diff > 0: cd = f"{int(diff//3600)}h {int((diff%3600)//60)}m"

            # Compute real data from bookings (no more placeholders)
            dep_bookings = bookings_by_dep.get(dep.id, [])
            dep_booking_count = len(dep_bookings)
            outbound_revenue = sum(bk.calculated_price for bk in dep_bookings)
            outbound_cost = sum(bk.estimated_cost or 0.0 for bk in dep_bookings)
            departure_net_profit = outbound_revenue - outbound_cost

            # ── Round-Trip / Cycle Profit ────────────────────────────────
            backhaul_revenue = 0.0
            backhaul_cost = 0.0
            return_cor = cor.return_corridor_id
            if return_cor:
                # Find the return departure in the same week (e.g. Wed return for Tue outbound)
                return_dep = self.search([
                    ("corridor_id", "=", return_cor.id),
                    ("departure_date", ">=", dd),
                    ("departure_date", "<=", sunday),
                    ("active", "=", True),
                ], limit=1)
                if return_dep:
                    return_bookings = bookings_by_dep.get(return_dep.id, [])
                    backhaul_revenue = sum(bk.calculated_price for bk in return_bookings)
                    backhaul_cost = sum(bk.estimated_cost or 0.0 for bk in return_bookings)

            # Use lane round-trip targets for cost estimation when no bookings
            lane = cor.lane_ids[:1] if cor.lane_ids else None
            if not backhaul_cost and lane:
                backhaul_cost = lane.return_estimated_cost or 0.0
            if not outbound_cost and lane:
                outbound_cost = lane.estimated_one_way_cost or 0.0

            gross_revenue = outbound_revenue + backhaul_revenue
            cycle_cost = outbound_cost + backhaul_cost
            cycle_net_profit = gross_revenue - cycle_cost
            departure_margin_pct = (departure_net_profit / outbound_revenue * 100) if outbound_revenue > 0 else 0.0
            cycle_margin_pct = (cycle_net_profit / gross_revenue * 100) if gross_revenue > 0 else 0.0

            # Use computed peak (CapacityEngine) or fall back to stored field
            try:
                from ..services.capacity_engine import CapacityEngine
                peak = CapacityEngine(self.env).compute_departure_peak(dep)
            except Exception:
                peak = {"peak_pallets": dep.peak_pallets or 0, "total_handled": dep.total_handled_pallets or 0}

            dc[str(di)].append({
                "id": cor.id, "departure_id": dep.id, "route": cor.name or "", "is_corridor": True,
                "status": s, "status_label": sl,
                "truck": dep.vehicle_id.name or "", "driver": dep.driver_id.name or "",
                "equipment": cor.equipment_type.upper(),
                "max_pallets": cor.truck_capacity or 12,
                "booked_pallets": peak.get("peak_pallets", dep.peak_pallets or 0),
                "total_handled": peak.get("total_handled", dep.total_handled_pallets or 0),
                "peak_weight": peak.get("peak_weight", 0.0),
                "revenue_target": cor.full_revenue_target or 0,
                # Outbound / Departure metrics
                "outbound_revenue": round(outbound_revenue, 2),
                "booked_revenue": round(outbound_revenue, 2),
                "estimated_cost": round(outbound_cost, 2),
                "departure_net_profit": round(departure_net_profit, 2),
                "departure_margin_pct": round(departure_margin_pct, 1),
                # Cycle / Round-trip metrics
                "gross_revenue": round(gross_revenue, 2),
                "backhaul_revenue": round(backhaul_revenue, 2),
                "cycle_cost": round(cycle_cost, 2),
                "cycle_net_profit": round(cycle_net_profit, 2),
                "cycle_margin_pct": round(cycle_margin_pct, 1),
                # Meta
                "cutoff": cutoff, "departure": dep_str, "countdown": cd,
                "booking_count": dep_booking_count,
                "stop_count": len(cor.stop_ids),
                "return_corridor_id": return_cor.id if return_cor else None,
            })
        return {"week_days": wd, "day_cards": dc,
                "date_range": f"{monday.strftime('%B %d')} — {sunday.strftime('%B %d, %Y')}",
                "today": today.isoformat()}

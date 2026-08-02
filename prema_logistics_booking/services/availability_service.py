"""Scheduled LTL availability engine — finds deliverable service options for a customer
shipment across weeks, respecting capacity, temperature, and route compatibility.

V3: Canonical data source is logistics.corridor.departure.
Legacy logistics.route.run is searched ONLY as a logged fallback when no corridor
departure exists.

Service search priority:
  1. SAME-DAY DIRECT
  2. SAME-DAY EN-ROUTE
  3. SAME-DAY RETURN-PATH
  4. NEXT EXISTING SCHEDULED RUN (current week)
  5. HUB-CONNECTED SCHEDULED RUN (current week)
  6. NEXT WEEK'S VALID RUN
  7. CUSTOM QUOTE
"""
import datetime
import logging
from zoneinfo import ZoneInfo
from collections import defaultdict

_logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo("America/Toronto")
MAX_WEEKS_AHEAD = 8  # configurable booking horizon

SEARCH_PRIORITY = [
    "same_day_direct",
    "same_day_en_route",
    "same_day_return_path",
    "scheduled_current_week",
    "hub_connected_current_week",
    "next_week_scheduled",
    "custom_quote",
]


class DeliveryOption:
    """One bookable service option for a customer shipment."""
    def __init__(self, priority, delivery_date, pickup_date, price,
                 service_label="", routing_strategy="", route_run=None,
                 departure=None, temperature_mode="dry", available_pallets=0,
                 capacity_ok=True, reason=""):
        self.priority = priority
        self.delivery_date = delivery_date
        self.pickup_date = pickup_date
        self.price = price
        self.service_label = service_label
        self.routing_strategy = routing_strategy
        self.route_run = route_run        # legacy — kept for request_quote controller compatibility
        self.departure = departure        # V3 canonical
        self.temperature_mode = temperature_mode
        self.available_pallets = available_pallets
        self.capacity_ok = capacity_ok
        self.reason = reason


class SchedulerAvailabilityResult:
    def __init__(self):
        self.options = []       # list of DeliveryOption
        self.current_week_label = ""
        self.available_weeks = []  # list of week label strings


class ScheduledAvailabilityService:
    """Finds what delivery days are available for a customer shipment."""
    def __init__(self, env):
        self.env = env(su=True)

    def find_available_services(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                                 temperature_mode="dry", requested_week_offset=0):
        """Returns SchedulerAvailabilityResult with all bookable options."""
        result = SchedulerAvailabilityResult()

        if not pickup_fsa.region_id or not delivery_fsa.region_id:
            return result

        origin_code = pickup_fsa.region_id.code
        dest_code = delivery_fsa.region_id.code
        now = datetime.datetime.now(tz=BUSINESS_TZ)

        # Generate available week labels
        weeks = self._generate_weeks(now)
        result.available_weeks = [w["label"] for w in weeks]

        # Only search the requested week
        target_week = weeks[min(requested_week_offset, len(weeks) - 1)]
        result.current_week_label = target_week["label"]
        week_start = target_week["start"]
        week_end = target_week["end"]

        # 1. Search same-day options (today only, if within target week)
        if now.date() >= week_start and now.date() <= week_end:
            same_day = self._search_same_day(origin_code, dest_code, pallets, weight_lbs,
                                              temperature_mode, now)
            result.options.extend(same_day)

        # 2. Search scheduled route runs in target week
        scheduled = self._search_scheduled_runs(origin_code, dest_code, pallets, weight_lbs,
                                                 temperature_mode, week_start, week_end)
        result.options.extend(scheduled)

        # 3. If nothing in target week, search next week(s)
        if not result.options:
            for future_week in weeks[1:]:
                f_start = future_week["start"]
                f_end = future_week["end"]
                future_opts = self._search_scheduled_runs(origin_code, dest_code, pallets, weight_lbs,
                                                           temperature_mode, f_start, f_end)
                if future_opts:
                    for opt in future_opts:
                        opt.priority = "next_week_scheduled"
                    result.options.extend(future_opts)
                    break

        # 4. If still nothing, offer custom quote
        if not result.options:
            result.options.append(DeliveryOption(
                priority="custom_quote",
                delivery_date=None,
                pickup_date=None,
                price=0,
                service_label="Request a custom quote — this route is outside our scheduled network",
                routing_strategy="custom_quote",
                reason="no_scheduled_service",
            ))

        # Sort by priority
        priority_order = {p: i for i, p in enumerate(SEARCH_PRIORITY)}
        result.options.sort(key=lambda o: priority_order.get(o.priority, 99))

        return result

    def _generate_weeks(self, now):
        """Generate week labels for the booking horizon."""
        weeks = []
        # Find Monday of current week
        monday = now.date() - datetime.timedelta(days=now.weekday())
        for i in range(MAX_WEEKS_AHEAD):
            ws = monday + datetime.timedelta(weeks=i)
            we = ws + datetime.timedelta(days=4)  # Mon-Fri
            if i == 0:
                label = f"Current Week — {ws.strftime('%b %d')} – {we.strftime('%b %d')}"
            elif i == 1:
                label = f"Next Week — {ws.strftime('%b %d')} – {we.strftime('%b %d')}"
            else:
                label = f"Week of {ws.strftime('%b %d')} – {we.strftime('%b %d')}"
            weeks.append({"label": label, "start": ws, "end": we, "offset": i})
        return weeks

    # ── V3: Canonical search via logistics.corridor.departure ──────────

    def _search_same_day(self, origin_code, dest_code, pallets, weight_lbs, temp_mode, now):
        """Find same-day delivery options via corridor departures (V3 canonical)."""
        options = []
        today = now.date()
        weekday = today.weekday()
        if weekday >= 5:
            return options

        # Canonical: search corridor departures
        Departure = self.env["logistics.corridor.departure"]
        deps = Departure.search([
            ("departure_date", "=", today),
            ("status", "in", ("scheduled", "departed", "in_transit")),
            ("active", "=", True),
        ])
        for dep in deps:
            if not dep.corridor_id:
                continue
            if not self._dep_temp_compatible(dep, temp_mode):
                continue
            if not self._dep_has_capacity(dep, pallets, weight_lbs):
                continue

            dep_origin = dep.corridor_id.start_hub_id.code if dep.corridor_id.start_hub_id else ""
            dep_dest = dep.corridor_id.end_hub_id.code if dep.corridor_id.end_hub_id else ""

            if origin_code == dep_origin and dest_code == dep_dest:
                options.append(self._make_dep_option("same_day_direct", today, today, dep, temp_mode))
            elif self._dep_regions_in_corridor(origin_code, dest_code, dep):
                options.append(self._make_dep_option("same_day_en_route", today, today, dep, temp_mode))

        # Legacy fallback: only if no corridor departures exist at all
        if not options and not deps:
            options = self._search_same_day_legacy(origin_code, dest_code, pallets, weight_lbs, temp_mode, now)

        return options

    def _search_scheduled_runs(self, origin_code, dest_code, pallets, weight_lbs, temp_mode, week_start, week_end):
        """Find scheduled departures in the given week (V3 canonical)."""
        options = []
        Departure = self.env["logistics.corridor.departure"]

        deps = Departure.search([
            ("departure_date", ">=", week_start),
            ("departure_date", "<=", week_end),
            ("status", "in", ("scheduled", "confirmed")),
            ("active", "=", True),
        ], order="departure_date")

        for dep in deps:
            if not dep.corridor_id:
                continue
            if not self._dep_temp_compatible(dep, temp_mode):
                continue
            if not self._dep_has_capacity(dep, pallets, weight_lbs):
                continue

            cor = dep.corridor_id
            dep_origin = cor.start_hub_id.code if cor.start_hub_id else ""
            dep_dest = cor.end_hub_id.code if cor.end_hub_id else ""
            dep_via = cor.via_hub_id.code if cor.via_hub_id else ""
            dep_date = dep.departure_date

            dest_matches = (dest_code == dep_dest or dest_code == dep_via)
            if origin_code == dep_origin and dest_matches:
                pickup_date = dep_date  # hub origins ship same day
                options.append(self._make_dep_option("scheduled_current_week", dep_date, pickup_date, dep, temp_mode))

            # Hub-connected via R1
            elif origin_code != "R1" and dest_code != "R1":
                hub_dep_out = Departure.search([
                    ("departure_date", ">=", week_start), ("departure_date", "<=", week_end),
                    ("corridor_id.start_hub_id.code", "=", origin_code),
                    ("corridor_id.end_hub_id.code", "in", ("R1", dest_code)),
                    ("status", "in", ("scheduled", "confirmed")), ("active", "=", True),
                ], limit=1)
                hub_dep_in = Departure.search([
                    ("departure_date", ">=", week_start), ("departure_date", "<=", week_end),
                    ("corridor_id.start_hub_id.code", "=", "R1"),
                    ("corridor_id.end_hub_id.code", "=", dest_code),
                    ("status", "in", ("scheduled", "confirmed")), ("active", "=", True),
                ], limit=1)
                if hub_dep_out and hub_dep_in and hub_dep_out.departure_date <= hub_dep_in.departure_date:
                    already = any(o.pickup_date == hub_dep_out.departure_date and o.delivery_date == hub_dep_in.departure_date for o in options)
                    if not already:
                        options.append(DeliveryOption(
                            priority="hub_connected_current_week",
                            delivery_date=hub_dep_in.departure_date,
                            pickup_date=hub_dep_out.departure_date,
                            price=self._estimate_price(origin_code, dest_code, pallets, weight_lbs, temp_mode),
                            service_label=f"Scheduled LTL via Hub — Pickup {hub_dep_out.departure_date.strftime('%A %b %d')}, Delivery {hub_dep_in.departure_date.strftime('%A %b %d')}",
                            routing_strategy="hub_transfer",
                            departure=hub_dep_in,
                            temperature_mode=temp_mode,
                            available_pallets=hub_dep_in.max_capacity - (hub_dep_in.computed_peak_pallets or 0),
                        ))

        # Legacy fallback
        if not options and not deps:
            options = self._search_scheduled_runs_legacy(origin_code, dest_code, pallets, weight_lbs, temp_mode, week_start, week_end)

        return options

    # ── Corridor Departure helpers ────────────────────────────────────

    def _make_dep_option(self, priority, delivery_date, pickup_date, dep, temp_mode):
        day_name = delivery_date.strftime("%A") if delivery_date else ""
        return DeliveryOption(
            priority=priority, delivery_date=delivery_date, pickup_date=pickup_date,
            price=self._estimate_price(
                dep.corridor_id.start_hub_id.code if dep.corridor_id.start_hub_id else "",
                dep.corridor_id.end_hub_id.code if dep.corridor_id.end_hub_id else "",
                1, 0, temp_mode,
            ),
            service_label=f"Scheduled LTL — {day_name} — {dep.corridor_id.name}",
            routing_strategy=priority.replace("_current_week", ""),
            departure=dep,
            temperature_mode=temp_mode,
            available_pallets=dep.max_capacity - (dep.computed_peak_pallets or 0),
        )

    def _dep_has_capacity(self, dep, pallets, weight_lbs):
        peak = dep.computed_peak_pallets or 0
        return (dep.max_capacity - peak) >= pallets

    def _dep_temp_compatible(self, dep, temp_mode):
        if not dep.corridor_id:
            return False
        cap = dep.corridor_id.temperature_capability or "dry"
        if cap == "all":
            return True
        if cap == "dry" and temp_mode == "dry":
            return True
        if cap == "chilled" and temp_mode in ("dry", "chilled"):
            return True
        return False

    def _dep_regions_in_corridor(self, origin_code, dest_code, dep):
        cor = dep.corridor_id
        if not cor:
            return False
        codes = set()
        if cor.start_hub_id:
            codes.add(cor.start_hub_id.code)
        if cor.end_hub_id:
            codes.add(cor.end_hub_id.code)
        if cor.via_hub_id:
            codes.add(cor.via_hub_id.code)
        return origin_code in codes and dest_code in codes

    # ── LEGACY FALLBACKS (logged, only when no corridor departures exist) ──

    def _search_same_day_legacy(self, origin_code, dest_code, pallets, weight_lbs, temp_mode, now):
        _logger.warning("availability_service: falling back to legacy logistics.route.run for same-day search")
        options = []
        today = now.date()
        RouteRun = self.env["logistics.route.run"]
        runs = RouteRun.search([("run_date", "=", today), ("state", "in", ("scheduled", "confirmed"))])
        for run in runs:
            if not self._temp_compatible(run, temp_mode) or not self._has_capacity(run, pallets, weight_lbs):
                continue
            run_origin = run.origin_region_id.code if run.origin_region_id else ""
            run_dest = run.destination_region_id.code if run.destination_region_id else ""
            if origin_code == run_origin and dest_code == run_dest:
                options.append(self._make_option("same_day_direct", today, today, run, temp_mode, pallets, weight_lbs))
            elif self._regions_in_corridor(origin_code, dest_code, run):
                options.append(self._make_option("same_day_en_route", today, today, run, temp_mode, pallets, weight_lbs))
            elif self._regions_in_return_path(origin_code, dest_code, run):
                options.append(self._make_option("same_day_return_path", today, today, run, temp_mode, pallets, weight_lbs))
        return options

    def _search_scheduled_runs_legacy(self, origin_code, dest_code, pallets, weight_lbs, temp_mode, week_start, week_end):
        _logger.warning("availability_service: falling back to legacy logistics.route.run for scheduled search")
        options = []
        RouteRun = self.env["logistics.route.run"]
        runs = RouteRun.search([
            ("run_date", ">=", week_start), ("run_date", "<=", week_end),
            ("state", "in", ("scheduled", "confirmed")),
        ], order="run_date")
        for run in runs:
            if not self._temp_compatible(run, temp_mode) or not self._has_capacity(run, pallets, weight_lbs):
                continue
            run_origin = run.origin_region_id.code if run.origin_region_id else ""
            run_dest = run.destination_region_id.code if run.destination_region_id else ""
            run_via = run.via_hub_id.code if run.via_hub_id else ""
            run_date = run.run_date
            dest_matches = (dest_code == run_dest or dest_code == run_via)
            if origin_code == run_origin and dest_matches:
                pickup_date = self._compute_pickup_date(run, run_date, origin_code, dest_code)
                options.append(self._make_option("scheduled_current_week", run_date, pickup_date, run, temp_mode, pallets, weight_lbs))
        return options

    def _make_option(self, priority, delivery_date, pickup_date, run, temp_mode, pallets, weight_lbs):
        """Build a DeliveryOption from a route run."""
        day_name = delivery_date.strftime("%A") if delivery_date else ""
        pickup_day_name = pickup_date.strftime("%A %b %d") if pickup_date else ""
        delivery_day_name = delivery_date.strftime("%A %b %d") if delivery_date else ""

        return DeliveryOption(
            priority=priority,
            delivery_date=delivery_date,
            pickup_date=pickup_date,
            price=self._estimate_price(
                run.origin_region_id.code if run.origin_region_id else "",
                run.destination_region_id.code if run.destination_region_id else "",
                pallets, weight_lbs, temp_mode,
            ),
            service_label=f"Scheduled LTL — {day_name} — Pickup {pickup_day_name}, Delivery {delivery_day_name}",
            routing_strategy=priority.replace("_current_week", ""),
            route_run=run,
            temperature_mode=temp_mode,
            available_pallets=run.available_pallets,
        )

    def _estimate_price(self, origin_code, dest_code, pallets, weight_lbs, temp_mode):
        """Get a price estimate for a lane. Returns 0 if not available."""
        try:
            from .pricing_service import PricingService
            Fsa = self.env["logistics.fsa"]
            Lane = self.env["logistics.lane"]
            Region = self.env["logistics.region"]

            origin_region = Region.search([("code", "=", origin_code)], limit=1)
            dest_region = Region.search([("code", "=", dest_code)], limit=1)
            if not origin_region or not dest_region:
                return 0.0

            pickup_fsa = Fsa.search([("region_id", "=", origin_region.id)], limit=1)
            delivery_fsa = Fsa.search([("region_id", "=", dest_region.id)], limit=1)
            if not pickup_fsa or not delivery_fsa:
                return 0.0

            result = PricingService(self.env).calculate(
                pickup_fsa, delivery_fsa, "ltl", temp_mode, pallets, weight_lbs,
            )
            return result.calculated_price if result.available else 0.0
        except Exception:
            return 0.0

    def _compute_pickup_date(self, run, delivery_date, origin_code, dest_code):
        """Determine the required pickup date for a delivery date."""
        # For direct routes: pickup is 1 business day before delivery (typical LTL)
        # This should be driven by the lane schedule in production
        if origin_code == "R1":
            return delivery_date  # Hub origins ship same day
        return delivery_date - datetime.timedelta(days=1)

    def _has_capacity(self, run, pallets, weight_lbs):
        """Check if the route run has capacity for this shipment."""
        return (run.available_pallets >= pallets and
                run.available_weight_lbs >= weight_lbs)

    def _temp_compatible(self, run, temp_mode):
        """Check temperature compatibility based on VEHICLE capability, not run label."""
        # "all" runs support everything (vehicle is reefer-capable)
        if run.temperature_mode == "all":
            return True
        # Dry runs: check if vehicle actually supports reefing
        if run.temperature_mode == "dry":
            if temp_mode == "dry":
                return True
            # Check if the assigned vehicle is reefer-capable
            if run.vehicle_id and run.vehicle_id.x_reefer:
                return True  # vehicle can do reefer even if run was labeled dry
            return False
        # Chilled runs: can take dry, chilled, or frozen
        if run.temperature_mode == "chilled":
            return True
        # Frozen runs: can take any temp (frozen needs coldest)
        if run.temperature_mode == "frozen":
            return True
        return False

    def _regions_in_corridor(self, origin_code, dest_code, run):
        """Check if both regions are on the run's corridor (for en-route)."""
        corridor_codes = set()
        if run.origin_region_id:
            corridor_codes.add(run.origin_region_id.code)
        if run.destination_region_id:
            corridor_codes.add(run.destination_region_id.code)
        if run.via_hub_id:
            corridor_codes.add(run.via_hub_id.code)
        return origin_code in corridor_codes and dest_code in corridor_codes

    def _regions_in_return_path(self, origin_code, dest_code, run):
        """Check if origin→dest follows return direction (dest appears before origin on return)."""
        # Simplified: check if run is westbound and origin is east of dest
        run_dir = run.corridor_name or ""
        if "westbound" in run_dir.lower() or "return" in run_dir.lower():
            corridor = set()
            if run.origin_region_id: corridor.add(run.origin_region_id.code)
            if run.destination_region_id: corridor.add(run.destination_region_id.code)
            return origin_code in corridor and dest_code in corridor
        return False

"""Operating Corridor — THE single source of truth for operational routes.

This model absorbed the previously-separate logistics.route.template and
logistics.route.run concepts (removed in 18.0.6.0.0). A corridor defines the
ordered stop sequence, recurrence rules, and scheduled departures in ONE place.

@removed models this one replaced:
    - logistics.route.template  → corridor fields (phase, truck_slot, operate_*)
    - logistics.route.run       → logistics.corridor.departure
"""

import logging as _logging
import datetime
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class LogisticsCorridor(models.Model):
    _name = "logistics.corridor"
    _description = "Operating Corridor (ordered truck route) — single source of truth"
    _order = "name"

    name = fields.Char(required=True)
    direction = fields.Selection([
        ("eastbound", "Eastbound"), ("westbound", "Westbound"),
        ("northbound", "Northbound"), ("southbound", "Southbound"),
        ("bidirectional", "Bidirectional"), ("local", "Local Operations"),
        ("local_loop", "Local Loop"), ("round_trip", "Round Trip"),
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
    start_time = fields.Float(string="Start Time", default=7.0, help="24h float, e.g. 7.0 = 7:00 AM")
    destination_hub_arrival_time = fields.Float(
        string="Hub Arrival Time",
        help="Expected arrival back at the Hub or at the Destination Hub. Used to validate same-day transfers.",
    )
    operate_monday = fields.Boolean(string="Monday")
    operate_tuesday = fields.Boolean(string="Tuesday")
    operate_wednesday = fields.Boolean(string="Wednesday")
    operate_thursday = fields.Boolean(string="Thursday")
    operate_friday = fields.Boolean(string="Friday")
    operate_saturday = fields.Boolean(string="Saturday")
    operate_sunday = fields.Boolean(string="Sunday")
    operating_days_display = fields.Char(
        string="Every Week", compute="_compute_operating_days_display",
        help="The weekly days used to generate exact departures.",
    )
    departure_horizon_weeks = fields.Integer(
        string="Customer Booking Horizon (weeks)", default=8,
        help="Exactly this many weekly occurrences are maintained. The maximum is eight weeks.",
    )
    holiday_calendar_ids = fields.Many2many(
        "logistics.holiday.calendar", "logistics_corridor_holiday_rel",
        "corridor_id", "calendar_id", string="Holiday / Blackout Calendars",
    )
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
        help="Departure hub for this weekly service."
    )
    destination_hub_id = fields.Many2one(
        "logistics.hub", string="Destination Hub",
        help="Arrival hub for this weekly service."
    )
    transfer_hub_id = fields.Many2one(
        "logistics.hub", string="Transfer Hub",
        help="Intermediate transfer hub."
    )
    same_day_return = fields.Boolean(
        string="Same-Day Return",
        help="Vehicle returns to origin hub on the same operating day."
    )
    paired_return_service_id = fields.Many2one(
        "logistics.corridor", string="Paired Return Service",
        help="The return-direction weekly service paired with this outbound service."
    )

    # ── Distance & customer pricing authority ──────────────────────
    rate_per_km = fields.Float(
        string="$ / km", default=4.0,
        help="Truck revenue target per kilometre for this corridor.",
    )
    planned_pallets = fields.Integer(string="Planned Pallets", default=8)
    included_weight_per_pallet = fields.Float(
        string="Included Weight per Pallet (lb)", default=500.0,
    )
    minimum_booking_charge = fields.Monetary(
        string="Minimum Booking Charge", default=150.0,
        help="Applied once to the complete booking, never once per transfer leg.",
    )
    ltl_additional_stop_charge = fields.Monetary(
        string="Additional Stop Charge", default=0.0,
        help="Charge applied for each additional delivery stop within the "
             "same destination city/region after the first stop.",
    )
    ltl_additional_pickup_charge = fields.Monetary(
        string="Additional Pickup Charge", default=0.0,
        help="Charge applied for each additional qualifying pickup stop "
             "after the first pickup. Corridor-specific; never hardcoded.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    pallet_rate_per_km = fields.Float(
        string="$ / km per Pallet", compute="_compute_corridor_pricing", store=True,
        digits=(12, 6),
    )
    full_distance_km = fields.Float(
        string="Full Corridor Distance (km)", compute="_compute_corridor_pricing", store=True,
        help="Farthest configured stop distance; doubled for a same-day return.",
    )
    destination_hub_distance_km = fields.Float(
        string="Destination Hub Distance (km)", readonly=True, copy=False,
        help="Cumulative road distance to the Destination Hub, calculated with the ordered route.",
    )
    full_revenue_target = fields.Monetary(
        string="Full-Corridor Revenue Target", compute="_compute_corridor_pricing", store=True,
    )

    # ── Phase 3: Customer Pricing ───────────────────────────────────
    excess_weight_rate_per_lb = fields.Float(
        string="Excess Weight $ / lb",
        default=0.10,
        help="Charge per pound over included weight. Corridor override; "
             "falls back to global default (Dispatch Settings) if 0.",
    )
    enable_volume_discounts = fields.Boolean(string="Enable Volume Discounts")
    pallet_volume_tier_ids = fields.One2many(
        "logistics.pallet.volume.tier", "corridor_id", string="Pallet Volume Tiers",
        copy=True,
    )
    enable_ftl = fields.Boolean(string="Enable FTL")
    ftl_threshold_pallets = fields.Integer(
        string="FTL Threshold", default=10,
        help="Physical pallets at or above this count trigger FTL pricing.",
    )
    ftl_rate_per_km = fields.Float(string="FTL $ / km", default=0.0)
    # Retired from pricing and UI — replaced by FTL Regional Minimums
    # (ftl_regional_minimum_ids). Kept as a database column for migration
    # compatibility with existing corridor records; never read by any new
    # FTL calculation.
    ftl_minimum_charge = fields.Monetary(string="Minimum FTL Charge", default=0.0)
    ftl_regional_minimum_ids = fields.One2many(
        "logistics.ftl.regional.minimum", "corridor_id",
        string="FTL Regional Pricing", copy=True,
    )
    ftl_reserve_entire_truck = fields.Boolean(string="Reserve Entire Truck", default=True)
    ftl_behavior = fields.Selection([
        ("recommend", "Recommend FTL"),
        ("auto_price", "Automatically Price as FTL"),
        ("dispatcher_approval", "Dispatcher Approval Required"),
    ], string="FTL Threshold Behavior", default="auto_price")
    enable_transit_pricing = fields.Boolean(string="Enable Transit / Feeder Pricing")
    feeder_pricing_method = fields.Selection([
        ("percentage", "Percentage Discount"),
        ("dedicated_km", "Dedicated Feeder $/km"),
        ("connected_corridor", "Connected Corridor Rate"),
    ], string="Feeder Pricing Method", default="percentage")
    feeder_discount_pct = fields.Float(string="Feeder Discount %", default=0.0)
    feeder_rate_per_km = fields.Float(string="Feeder $/km Override", default=0.0)
    feeder_minimum_charge = fields.Monetary(string="Minimum Feeder Charge", default=0.0)
    allowed_feeder_region_ids = fields.Many2many(
        "logistics.region", "corridor_feeder_region_rel",
        "corridor_id", "region_id", string="Allowed Feeder Regions",
        domain="[('is_official_ltl_region', '=', True), ('active', '=', True)]",
    )

    # ── Equipment (absorbed from route.template) ────────────────────
    equipment_profile_id = fields.Many2one(
        "logistics.equipment.profile", string="Equipment Requirement",
        domain="[('is_requirement_class', '=', True)]",
    )

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    departure_ids = fields.One2many("logistics.corridor.departure", "corridor_id")
    stop_ids = fields.One2many("logistics.corridor.stop", "corridor_id")

    is_two_way = fields.Boolean(
        string="Two-Way Service", compute="_compute_is_two_way",
        help="True only when paired_return_service_id is set AND that paired "
             "corridor's stop order is the exact reverse of this one's — "
             "never inferred from the two corridors merely sharing regions.",
    )

    @api.depends(
        "paired_return_service_id",
        "stop_ids.sequence", "stop_ids.region_id",
        "paired_return_service_id.stop_ids.sequence", "paired_return_service_id.stop_ids.region_id",
    )
    def _compute_is_two_way(self):
        for rec in self:
            paired = rec.paired_return_service_id
            if not paired:
                rec.is_two_way = False
                continue
            # Plain list comprehensions, not .mapped(...).ids — mapped() on a
            # Many2one collapses repeated regions (a recordset is a set of
            # ids), which would silently break the ordering comparison for
            # any corridor whose stops revisit the same region (e.g. a
            # self-contained round-trip corridor).
            this_order = [s.region_id.id for s in rec.stop_ids.sorted("sequence")]
            paired_order = [s.region_id.id for s in paired.stop_ids.sorted("sequence")]
            rec.is_two_way = bool(this_order) and this_order == list(reversed(paired_order))

    @api.depends(
        "operate_monday", "operate_tuesday", "operate_wednesday",
        "operate_thursday", "operate_friday", "operate_saturday", "operate_sunday",
    )
    def _compute_operating_days_display(self):
        labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        fields_by_day = (
            "operate_monday", "operate_tuesday", "operate_wednesday",
            "operate_thursday", "operate_friday", "operate_saturday", "operate_sunday",
        )
        for rec in self:
            rec.operating_days_display = ", ".join(
                label for label, field_name in zip(labels, fields_by_day) if rec[field_name]
            ) or "Not scheduled"

    @api.depends(
        "rate_per_km", "planned_pallets", "same_day_return",
        "stop_ids.distance_from_origin_km", "stop_ids.active",
        "destination_hub_distance_km",
    )
    def _compute_corridor_pricing(self):
        for rec in self:
            farthest = max(
                (rec.stop_ids.filtered("active").mapped("distance_from_origin_km") or [0.0])
                + [rec.destination_hub_distance_km or 0.0]
            )
            rec.full_distance_km = farthest * (2.0 if rec.same_day_return else 1.0)
            rec.pallet_rate_per_km = (
                rec.rate_per_km / rec.planned_pallets if rec.planned_pallets > 0 else 0.0
            )
            rec.full_revenue_target = rec.full_distance_km * rec.rate_per_km

    @api.constrains("planned_pallets", "rate_per_km", "included_weight_per_pallet", "departure_horizon_weeks")
    def _check_corridor_pricing_and_horizon(self):
        for rec in self:
            if rec.planned_pallets <= 0:
                raise ValidationError(_("Planned Pallets must be greater than zero."))
            if rec.rate_per_km < 0:
                raise ValidationError(_("$ / km cannot be negative."))
            if rec.included_weight_per_pallet <= 0:
                raise ValidationError(_("Included weight per pallet must be greater than zero."))
            if not 1 <= (rec.departure_horizon_weeks or 0) <= 8:
                raise ValidationError(_("Customer booking horizon must be between 1 and 8 weeks."))

    def get_ftl_regional_rule(self, origin_region, destination_region):
        """Return the active FTL Regional Pricing rule for an exact
        origin → destination region pair (empty recordset when none)."""
        self.ensure_one()
        return self.ftl_regional_minimum_ids.filtered(
            lambda rule: rule.active
            and rule.origin_region_id == origin_region
            and rule.destination_region_id == destination_region
        )[:1]

    def compute_ftl_price(self, origin_region, destination_region, distance_km):
        """FTL regional pricing for one dedicated truck movement.

        No exact active rule        → distance × corridor FTL $ / km.
        pricing_type corridor_default → distance × corridor FTL $ / km.
        pricing_type flat_rate        → rule.flat_rate (distance-independent,
                                         no minimum comparison).
        pricing_type per_km          → distance × rule.ftl_rate_per_km_override.

        The retired Minimum FTL Charge fields (regional and corridor-wide)
        never participate in any calculation.
        """
        self.ensure_one()
        rule = self.get_ftl_regional_rule(origin_region, destination_region)
        currency = self.currency_id
        distance_km = distance_km or 0.0
        pricing_type = rule.pricing_type if rule else "corridor_default"
        if rule and pricing_type == "flat_rate":
            final_price = rule.flat_rate
            rate_per_km = 0.0
            distance_price = 0.0
        else:
            rate_per_km = (
                rule.ftl_rate_per_km_override
                if rule and pricing_type == "per_km"
                else self.ftl_rate_per_km
            )
            distance_price = currency.round(distance_km * rate_per_km) if currency \
                else round(distance_km * rate_per_km, 2)
            final_price = distance_price
        return {
            "price": final_price,
            "distance_price": distance_price,
            "rate_per_km": rate_per_km,
            "regional_rule": rule,
            "pricing_type": pricing_type,
        }

    def _operating_weekdays(self):
        self.ensure_one()
        names = (
            "operate_monday", "operate_tuesday", "operate_wednesday",
            "operate_thursday", "operate_friday", "operate_saturday", "operate_sunday",
        )
        # The visible Every Week checkboxes are the sole scheduling authority.
        return {idx for idx, field_name in enumerate(names) if self[field_name]}

    def _excluded_departure_dates(self):
        self.ensure_one()
        return set(self.holiday_calendar_ids.mapped("line_ids.date"))

    def _vehicle_capacity(self, vehicle=None):
        self.ensure_one()
        vehicle = vehicle or self.default_vehicle_id
        if not vehicle:
            return 0
        if hasattr(vehicle, "get_layout_capacity"):
            return vehicle.get_layout_capacity() or 0
        return vehicle.pin_wheel_pallet_capacity or vehicle.straight_pallet_capacity or vehicle.x_max_pallets or 0

    def _default_vehicle_for_date(self, departure_date, exclude_departure=None):
        """Return the default truck only when that truck/day is still free.

        A schedule row is still created when the truck is occupied, but it is
        deliberately left unassigned and therefore cannot accept bookings.
        The daily reconciliation assigns the default automatically once the
        conflict is removed.
        """
        self.ensure_one()
        vehicle = self.default_vehicle_id
        if not vehicle:
            return vehicle
        departure_domain = [
            ("vehicle_id", "=", vehicle.id),
            ("departure_date", "=", departure_date),
            ("active", "=", True),
            ("status", "not in", ("cancelled", "completed")),
        ]
        if exclude_departure:
            departure_domain.append(("id", "!=", exclude_departure.id))
        if self.env["logistics.corridor.departure"].sudo().search_count(departure_domain):
            return self.env["fleet.vehicle"]
        planner_jobs = self.env["prema.dispatch.job"].sudo().search([
            ("vehicle_id", "=", vehicle.id),
            ("operation_date", "=", departure_date),
            ("stage_id.stage_type", "not in", ("cancelled", "completed")),
        ])
        for job in planner_jobs:
            if exclude_departure and job.corridor_departure_id == exclude_departure:
                continue
            # A delivery-day card from yesterday's LTL departure reserves the
            # physical truck today. Pairing two corridors never makes that
            # truck available for a second job or departure on the same day.
            return self.env["fleet.vehicle"]
        return vehicle

    def resolve_region_segment(self, origin_region, destination_region):
        """Return the usable ordered segment between two served regions.

        A normal one-way corridor only permits travel in stop order.  A
        same-day-return corridor has a second (return) visit to every stop,
        so a pickup made later on the outbound route can still be delivered
        to an earlier stop on the way back without forcing a hub transfer.
        The returned distance is the distance the truck actually travels
        after pickup, not a straight-line estimate.
        """
        self.ensure_one()

        # Special case: intra-region booking (pickup and delivery in the same
        # service Region). The portal quote flow resolves at the Region level
        # (via FSAs) before we have exact addresses, so we treat this as a
        # zero-distance segment on a local corridor instead of accidentally
        # pricing the entire corridor loop from "hub visit #1" to "hub visit #2".
        #
        # IMPORTANT: Restrict this to local corridors only; linehaul corridors
        # that revisit the hub Region must not be usable as "local within R1".
        if origin_region and destination_region and origin_region == destination_region:
            if self.direction not in ("local", "local_loop"):
                return False
            stops = self.stop_ids.filtered(lambda s: s.active and s.region_id).sorted("sequence")
            # Need at least one served stop/hub match for this region with both permissions.
            served = stops.filtered(
                lambda s: s.region_id == origin_region and s.pickup_allowed and s.delivery_allowed
            )
            if not served:
                return False
            stop = served[:1]
            return {
                "corridor": self,
                "origin_stop": stop,
                "destination_stop": stop,
                "origin_region": origin_region,
                "destination_region": destination_region,
                "distance_km": 0.0,
                "pickup_day_offset": 0,
                "delivery_day_offset": 0,
                "origin_direction": "local",
                "destination_direction": "local",
                "origin_departure_time": self.start_time,
                "destination_arrival_time": False,
                "destination_sequence": stop.sequence,
            }

        stops = self.stop_ids.filtered(lambda stop: stop.active and stop.region_id).sorted("sequence")
        route_end = max(
            (stops.mapped("distance_from_origin_km") or [0.0])
            + [self.destination_hub_distance_km or 0.0]
        )
        last_day = max(stops.mapped("day_offset") or [0])

        def visits_for(region, pickup):
            visits = []
            permission = "pickup_allowed" if pickup else "delivery_allowed"
            for stop in stops.filtered(lambda item: item.region_id == region and item[permission]):
                visits.append({
                    "position": stop.distance_from_origin_km,
                    "day": stop.day_offset or 0,
                    "direction": "outbound",
                    "stop": stop,
                    "sequence": stop.sequence,
                    "arrival_time": stop.planned_arrival_time,
                    "departure_time": stop.planned_departure_time,
                })
                if self.same_day_return and route_end:
                    visits.append({
                        "position": 2.0 * route_end - stop.distance_from_origin_km,
                        "day": last_day,
                        "direction": "return",
                        "stop": stop,
                        "sequence": 100000 - stop.sequence,
                        "arrival_time": stop.planned_arrival_time,
                        "departure_time": stop.planned_departure_time,
                    })

            origin_hub_region = self.origin_hub_id.canonical_region_id
            if origin_hub_region and region == origin_hub_region:
                visits.append({
                    "position": 0.0,
                    "day": 0,
                    "direction": "outbound",
                    "stop": False,
                    "sequence": 0,
                    "arrival_time": self.start_time,
                    "departure_time": self.start_time,
                })
                if self.same_day_return and route_end:
                    visits.append({
                        "position": 2.0 * route_end,
                        "day": last_day,
                        "direction": "return",
                        "stop": False,
                        "sequence": 100000,
                        "arrival_time": self.destination_hub_arrival_time or False,
                        "departure_time": False,
                    })

            destination_hub_region = self.destination_hub_id.canonical_region_id
            if (
                destination_hub_region
                and region == destination_hub_region
                and self.destination_hub_distance_km > 0
            ):
                visits.append({
                    "position": self.destination_hub_distance_km,
                    "day": last_day,
                    "direction": "outbound",
                    "stop": False,
                    "sequence": 99999,
                    "arrival_time": self.destination_hub_arrival_time or False,
                    "departure_time": False,
                })
            return visits

        candidates = []
        for origin_visit in visits_for(origin_region, pickup=True):
            for destination_visit in visits_for(destination_region, pickup=False):
                if destination_visit["day"] < origin_visit["day"] or (
                    destination_visit["day"] == origin_visit["day"]
                    and destination_visit["position"] <= origin_visit["position"]
                ):
                    continue
                candidates.append({
                    "corridor": self,
                    "origin_stop": origin_visit["stop"],
                    "destination_stop": destination_visit["stop"],
                    "origin_region": origin_region,
                    "destination_region": destination_region,
                    "distance_km": destination_visit["position"] - origin_visit["position"],
                    "pickup_day_offset": origin_visit["day"],
                    "delivery_day_offset": destination_visit["day"],
                    "origin_direction": origin_visit["direction"],
                    "destination_direction": destination_visit["direction"],
                    "origin_departure_time": origin_visit["departure_time"],
                    "destination_arrival_time": destination_visit["arrival_time"],
                    "destination_sequence": destination_visit["sequence"],
                })
        if not candidates:
            return False
        return min(candidates, key=lambda segment: (
            segment["delivery_day_offset"] - segment["pickup_day_offset"],
            segment["distance_km"],
            segment["destination_sequence"],
        ))

    def action_refresh_departures(self):
        summary = {"created": 0, "updated": 0, "removed": 0, "preserved_booked": 0}
        for corridor in self:
            result = corridor._reconcile_departure_horizon()
            for key in summary:
                summary[key] += result.get(key, 0)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Departure schedule refreshed"),
                "message": _(
                    "Created %(created)s, updated %(updated)s, removed %(removed)s; "
                    "preserved %(preserved_booked)s booked departure(s).",
                    **summary,
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_recalculate_route_distance(self):
        """Fill cumulative road distance for ordered regions using Google Routes."""
        from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService

        for corridor in self:
            ordered = corridor.stop_ids.filtered("active").sorted("sequence")
            if not ordered:
                raise ValidationError(_("Add ordered regions before calculating distance."))

            def stop_location(record):
                saved = record.saved_location_id
                if saved:
                    if saved.pin_lat and saved.pin_lng:
                        return (saved.pin_lat, saved.pin_lng)
                    return saved.normalized_address or saved.address
                region = record.region_id
                if region and region.marker_latitude and region.marker_longitude:
                    return (region.marker_latitude, region.marker_longitude)
                return region.main_city if region else ""

            def hub_location(hub):
                if not hub:
                    return False
                if hub.saved_location_id and hub.saved_location_id.pin_lat and hub.saved_location_id.pin_lng:
                    return (hub.saved_location_id.pin_lat, hub.saved_location_id.pin_lng)
                if hub.latitude and hub.longitude:
                    return (hub.latitude, hub.longitude)
                return hub.formatted_address or (
                    hub.saved_location_id.address if hub.saved_location_id else ""
                )

            origin_hub = corridor.origin_hub_id
            origin_location = hub_location(origin_hub)
            if origin_hub and not origin_location:
                raise ValidationError(_(
                    "The Origin Hub needs a verified Saved Location, coordinates, or address."
                ))
            remaining_stops = ordered
            locations = []
            targets = []
            if origin_location:
                locations.append(origin_location)
                # A first stop representing the hub itself starts at 0 km.
                if ordered[0].region_id == origin_hub.canonical_region_id:
                    ordered[0].distance_from_origin_km = 0.0
                    remaining_stops = ordered[1:]
            else:
                ordered[0].distance_from_origin_km = 0.0
                locations.append(stop_location(ordered[0]))
                remaining_stops = ordered[1:]

            for stop in remaining_stops:
                locations.append(stop_location(stop))
                targets.append(("stop", stop))

            destination_hub = corridor.destination_hub_id
            destination_is_last_region = bool(
                destination_hub
                and destination_hub.canonical_region_id
                and ordered[-1].region_id == destination_hub.canonical_region_id
            )
            if destination_hub and not destination_is_last_region:
                locations.append(hub_location(destination_hub))
                targets.append(("destination_hub", destination_hub))

            if any(not value for value in locations):
                raise ValidationError(_(
                    "Every ordered region needs a map marker or Saved Location, and each configured Hub needs coordinates."
                ))
            legs = DispatchRouteService(self.env).get_sequential_travel(locations)
            if len(legs) != len(targets):
                raise ValidationError(_("Google Routes did not return every corridor leg."))
            cumulative = 0.0
            destination_hub_distance = 0.0
            for (target_type, target), leg in zip(targets, legs):
                cumulative += leg.get("distance_km") or 0.0
                if target_type == "stop":
                    target.distance_from_origin_km = round(cumulative, 1)
                else:
                    destination_hub_distance = round(cumulative, 1)
            if destination_is_last_region:
                destination_hub_distance = ordered[-1].distance_from_origin_km
            corridor.destination_hub_distance_km = destination_hub_distance
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("Corridor distance updated"),
                "message": _("Ordered road distances and the revenue target were recalculated."),
                "type": "success", "sticky": False,
            },
        }

    def _reconcile_departure_horizon(self, today=None):
        """Maintain the next eight weekly occurrences for this corridor.

        Future unbooked Scheduled rows that no longer match the weekly pattern
        are removed. Completed/in-progress rows and any booked future departure
        are preserved. Manual truck overrides are never overwritten.
        """
        self.ensure_one()
        today = today or fields.Date.context_today(self)
        weekdays = self._operating_weekdays()
        weeks = min(max(self.departure_horizon_weeks or 8, 1), 8)
        excluded = self._excluded_departure_dates()
        target_dates = set()
        cursor = today
        horizon_end = today + datetime.timedelta(weeks=weeks) - datetime.timedelta(days=1)
        local_now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        while weekdays and cursor <= horizon_end:
            weekday = cursor.weekday()
            departure_has_passed = (
                cursor == local_now.date()
                and (self.start_time or 0.0)
                <= (local_now.hour + local_now.minute / 60.0)
            )
            if weekday in weekdays and cursor not in excluded and not departure_has_passed:
                target_dates.add(cursor)
            cursor += datetime.timedelta(days=1)

        Departure = self.env["logistics.corridor.departure"].sudo()
        Leg = self.env["logistics.booking.leg"].sudo()
        future = Departure.search([
            ("corridor_id", "=", self.id),
            ("departure_date", ">=", today),
            ("status", "=", "scheduled"),
            ("active", "=", True),
        ])
        summary = {"created": 0, "updated": 0, "removed": 0, "preserved_booked": 0}
        for departure in future:
            booked = bool(Leg.search_count([
                ("departure_id", "=", departure.id),
                ("reservation_state", "in", ("pending", "reserved", "consumed")),
                ("booking_id.state", "!=", "cancelled"),
            ]))
            if departure.departure_date not in target_dates:
                if booked:
                    summary["preserved_booked"] += 1
                else:
                    departure.unlink()
                    summary["removed"] += 1
                continue
            if booked:
                # An already-sold exact departure is frozen. Schedule/default
                # changes apply to new or still-empty rows; dispatchers may
                # deliberately override this row from Open: Departure.
                summary["preserved_booked"] += 1
                continue
            vals = {}
            if departure.departure_time != self.start_time:
                vals["departure_time"] = self.start_time
            available_default = self._default_vehicle_for_date(
                departure.departure_date, exclude_departure=departure,
            )
            if departure.vehicle_assignment_source != "manual_override" and departure.vehicle_id != available_default:
                vals.update({
                    "vehicle_id": available_default.id or False,
                    "vehicle_assignment_source": "corridor_default",
                    "max_capacity": self._vehicle_capacity(available_default),
                })
            if vals:
                departure.with_context(corridor_default_sync=True).write(vals)
                summary["updated"] += 1

        existing_dates = set(future.filtered(lambda d: d.exists()).mapped("departure_date"))
        for departure_date in sorted(target_dates - existing_dates):
            available_default = self._default_vehicle_for_date(departure_date)
            Departure.with_context(corridor_default_sync=True).create({
                "corridor_id": self.id,
                "departure_date": departure_date,
                "departure_time": self.start_time,
                "vehicle_id": available_default.id or False,
                "vehicle_assignment_source": "corridor_default",
                "status": "scheduled",
                "max_capacity": self._vehicle_capacity(available_default),
            })
            summary["created"] += 1
        return summary

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("skip_departure_reconcile"):
            for record in records.filtered(lambda r: r.active and r._operating_weekdays()):
                record._reconcile_departure_horizon()
        return records

    def write(self, vals):
        vals = dict(vals)
        if "destination_hub_id" in vals:
            vals.setdefault("destination_hub_distance_km", 0.0)
        result = super().write(vals)
        schedule_fields = {
            "operate_monday", "operate_tuesday", "operate_wednesday",
            "operate_thursday", "operate_friday", "operate_saturday", "operate_sunday",
            "start_time", "default_vehicle_id",
            "departure_horizon_weeks", "holiday_calendar_ids", "active",
        }
        if schedule_fields.intersection(vals) and not self.env.context.get("skip_departure_reconcile"):
            for record in self.filtered("active"):
                record._reconcile_departure_horizon()
        return result

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
        """Daily cron: maintain an eight-week rolling departure horizon.
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
            result = generate_phase1_departures(self.env, weeks=8)
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
    vehicle_assignment_source = fields.Selection([
        ("corridor_default", "Corridor Default"),
        ("manual_override", "Manual Override"),
    ], default="corridor_default", required=True, copy=False)
    driver_id = fields.Many2one("res.partner", string="Driver")
    special_operation = fields.Boolean(
        string="Special Operation", default=False,
        help="Manual/overflow/special-operation departure. Not auto-generated.",
    )
    special_operation_reason = fields.Char(string="Special Operation Reason")
    special_operation_approved_by = fields.Many2one("res.users", string="Approved By")
    source_corridor_id = fields.Many2one(
        "logistics.corridor", string="Source Pricing Corridor",
        help="The corridor whose pricing applies to this special departure.",
    )
    routing_review_required = fields.Boolean(
        string="Routing Review Required", default=False,
        help="Flagged when corridor changes affect this departure's bookings.",
    )
    capacity_status = fields.Selection([
        ("available", "Available"),
        ("limited", "Limited"),
        ("full", "Full"),
        ("overbooked_review", "Overbooked — Review"),
    ], string="Capacity Status", compute="_compute_capacity_status", store=True)
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

    # ── Dynamic capacity / layout (VehicleCapacityService) ─────────────
    capacity_layout_override_id = fields.Many2one(
        "fleet.vehicle.pallet.layout", string="Layout Override",
        help="Optional dispatcher override. Must belong to the assigned "
             "truck and fit the currently reserved pallet positions.",
    )
    capacity_layout_code = fields.Char(compute="_compute_capacity_display")
    capacity_layout_name = fields.Char(compute="_compute_capacity_display")
    capacity_max_pallets = fields.Integer(compute="_compute_capacity_display")
    capacity_reserved_pallets = fields.Integer(compute="_compute_capacity_display")
    capacity_remaining_pallets = fields.Integer(compute="_compute_capacity_display")

    # ── Sellable capacity audit (shared LTL positions + FTL exclusivity) ──
    # LTL bookings reserve exactly their physical positions; an FTL /
    # Dedicated / Exclusive service booking reserves the ENTIRE vehicle.
    # A corridor's FTL PRICING threshold never reserves the truck — these
    # fields are service-type driven (see CapacityEngine._is_exclusive_service).
    exclusive_vehicle_reserved = fields.Boolean(
        string="Truck Exclusively Reserved", compute="_compute_capacity_display",
        help="A Full Truckload / Dedicated / Exclusive service booking owns "
             "this departure's ENTIRE vehicle — no other booking may join.")
    exclusive_booking_ref = fields.Char(
        string="Exclusive Booking", compute="_compute_capacity_display",
        help="Booking number(s) holding the truck exclusively.")
    reserved_ltl_positions = fields.Integer(
        string="Reserved LTL Positions", compute="_compute_capacity_display",
        help="Physical pallet positions reserved by LTL service bookings "
             "(segment peak — milk-run movements count their own spans).")
    planned_peak_onboard = fields.Integer(
        string="Planned Peak Onboard", compute="_compute_capacity_display",
        help="Highest physical pallet count across all corridor segments "
             "(LTL positions + any exclusive booking's positions).")
    vehicle_max_bookable_positions = fields.Integer(
        string="Max Bookable Positions", compute="_compute_capacity_display",
        help="The assigned vehicle's maximum pallet positions (best layout).")
    remaining_sellable_capacity = fields.Integer(
        string="Remaining Sellable Capacity", compute="_compute_capacity_display",
        help="Positions a NEW LTL booking may reserve: max − reserved LTL; "
             "0 when the truck is exclusively held.")

    def _compute_capacity_display(self):
        from ..services.vehicle_capacity_service import VehicleCapacityService
        service = VehicleCapacityService(self.env)
        for departure in self:
            result = service.evaluate(departure.vehicle_id, departure, 0)
            layout = result["selected_layout"] or {}
            departure.capacity_layout_code = layout.get("code", "")
            departure.capacity_layout_name = layout.get("name", "")
            departure.capacity_max_pallets = result["maximum_capacity"]
            departure.capacity_reserved_pallets = result["reserved_pallets"]
            departure.capacity_remaining_pallets = result["remaining_pallets"]
            departure.reserved_ltl_positions = result["reserved_ltl_positions"]
            departure.planned_peak_onboard = result["reserved_pallets"]
            departure.vehicle_max_bookable_positions = result["maximum_capacity"]
            departure.remaining_sellable_capacity = result["remaining_sellable_capacity"]
            departure.exclusive_vehicle_reserved = result["exclusive_vehicle_reserved"]
            exclusive_ids = result["exclusive_booking_ids"] or []
            if exclusive_ids:
                names = self.env["logistics.booking"].browse(exclusive_ids).mapped(
                    lambda b: b.booking_number or "Booking %s" % b.id)
                departure.exclusive_booking_ref = ", ".join(names)
            else:
                departure.exclusive_booking_ref = ""

    @api.constrains("capacity_layout_override_id", "vehicle_id")
    def _check_layout_override_valid(self):
        from ..services.vehicle_capacity_service import VehicleCapacityService
        service = VehicleCapacityService(self.env)
        for departure in self:
            override = departure.capacity_layout_override_id
            if not override:
                continue
            if override.vehicle_id != departure.vehicle_id:
                raise ValidationError(_(
                    "The layout override must belong to the assigned truck."))
            reserved = service.reserved_pallets(departure)
            if override.max_pallets < reserved:
                raise ValidationError(_(
                    "Layout %(layout)s cannot carry the %(reserved)s reserved "
                    "pallet positions.",
                    layout=override.display_name, reserved=reserved,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            corridor = self.env["logistics.corridor"].browse(vals.get("corridor_id"))
            if corridor:
                departure_date = fields.Date.to_date(vals.get("departure_date"))
                vals.setdefault("departure_time", corridor.start_time)
                if "vehicle_id" not in vals:
                    default_vehicle = corridor._default_vehicle_for_date(departure_date)
                    vals["vehicle_id"] = default_vehicle.id or False
                if "vehicle_assignment_source" not in vals:
                    vals["vehicle_assignment_source"] = (
                        "corridor_default" if self.env.context.get("corridor_default_sync")
                        else "manual_override" if vals.get("vehicle_id") else "corridor_default"
                    )
                selected_vehicle = (
                    self.env["fleet.vehicle"].browse(vals["vehicle_id"]).exists()
                    if vals.get("vehicle_id")
                    else self.env["fleet.vehicle"]
                )
                vals.setdefault(
                    "max_capacity",
                    corridor._vehicle_capacity(selected_vehicle) if selected_vehicle else 0,
                )
        records = super().create(vals_list)
        records._check_vehicle_day_conflicts()
        return records

    def write(self, vals):
        vals = dict(vals)
        if "vehicle_id" in vals and not self.env.context.get("corridor_default_sync"):
            vals["vehicle_assignment_source"] = "manual_override"
            vehicle = self.env["fleet.vehicle"].browse(vals.get("vehicle_id"))
            corridor = self[:1].corridor_id if self else self.env["logistics.corridor"]
            vals["max_capacity"] = corridor._vehicle_capacity(vehicle) if corridor else 0
            # Truck reassignment guard: the new truck must be able to carry
            # the pallets already reserved on this departure.
            from ..services.vehicle_capacity_service import VehicleCapacityService
            service = VehicleCapacityService(self.env)
            maximum = service.maximum_capacity(vehicle)
            for departure in self:
                reserved = service.reserved_pallets(departure)
                if reserved > maximum:
                    raise ValidationError(_(
                        "Truck cannot be assigned: %(reserved)s pallets are "
                        "reserved but this vehicle supports a maximum of "
                        "%(max)s pallet positions.",
                        reserved=reserved, max=maximum,
                    ))
        result = super().write(vals)
        if {"vehicle_id", "departure_date", "active", "status"}.intersection(vals):
            self._check_vehicle_day_conflicts()
        return result

    def _check_vehicle_day_conflicts(self):
        vehicle_ids = sorted(set(self.filtered("vehicle_id").mapped("vehicle_id").ids))
        if vehicle_ids:
            # Serialize assignments per physical vehicle so two concurrent
            # confirmations cannot both pass the Python conflict search.
            self.env.cr.execute(
                "SELECT id FROM fleet_vehicle WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(vehicle_ids)],
            )
        for departure in self.filtered(
            lambda d: d.active and d.vehicle_id and d.status not in ("cancelled", "completed")
        ):
            conflict = self.search([
                ("id", "!=", departure.id),
                ("vehicle_id", "=", departure.vehicle_id.id),
                ("departure_date", "=", departure.departure_date),
                ("active", "=", True),
                ("status", "not in", ("cancelled", "completed")),
            ], limit=1)
            if conflict:
                raise ValidationError(_(
                    "Truck %(truck)s is already booked for %(route)s on %(date)s. "
                    "Reassign one of the departures before accepting another route.",
                    truck=departure.vehicle_id.display_name,
                    route=conflict.corridor_id.display_name,
                    date=departure.departure_date,
                ))
            planner_jobs = self.env["prema.dispatch.job"].sudo().search([
                ("vehicle_id", "=", departure.vehicle_id.id),
                ("operation_date", "=", departure.departure_date),
                ("stage_id.stage_type", "not in", ("cancelled", "completed")),
            ])
            blocking_job = self.env["prema.dispatch.job"]
            for job in planner_jobs:
                if job.corridor_departure_id == departure:
                    continue
                blocking_job = job
                break
            if blocking_job:
                raise ValidationError(_(
                    "Truck %(truck)s already has job %(job)s on %(date)s. "
                    "Move that job or choose another truck before scheduling this departure.",
                    truck=departure.vehicle_id.display_name,
                    job=blocking_job.display_name,
                    date=departure.departure_date,
                ))

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

    @api.depends("peak_pallets", "max_capacity")
    def _compute_capacity_status(self):
        for r in self:
            cap = r.max_capacity or 12
            used = r.peak_pallets or 0
            avail = cap - used
            if avail <= 0:
                r.capacity_status = "full" if used <= cap else "overbooked_review"
            elif avail <= 2:
                r.capacity_status = "limited"
            else:
                r.capacity_status = "available"

"""Routing logic — determines the best routing strategy for a lane.
Used by both pricing (to estimate delivery dates) and booking (to create correct stops).

The hardcoded WEEKLY_TEMPLATE / SELLABLE_SCHEDULE dicts below are FALLBACK data.
The canonical source of truth is now logistics.corridor + logistics.corridor.departure
records. Once Phase 14 corridor/departure seed data is loaded into production, these
dicts should be removed and all routing resolved from the database.
"""
import datetime
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/Toronto")

ROUTING_STRATEGIES = ["direct", "en_route", "hub_transfer", "scheduled_connection", "multi_leg", "custom_quote"]

# ── DEPRECATED FALLBACK DATA ──────────────────────────────────────────
# Phase 1 weekly operating template (from business spreadsheet)
# Day index: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
# TODO: Remove once logistics.corridor records are seeded for Phase 14
WEEKLY_TEMPLATE = {
    0: {  # Monday: Southwest feeder
        "corridor": "Southwest Feeder",
        "regions": ["R1", "R2", "R3"],
        "direction": "bidirectional",
    },
    1: {  # Tuesday: Eastbound backbone
        "corridor": "Eastbound Quebec",
        "regions": ["R1", "R8", "R10", "R11", "R13", "R14", "R15"],
        "direction": "eastbound",
    },
    2: {  # Wednesday: Westbound backbone
        "corridor": "Westbound Return",
        "regions": ["R15", "R14", "R13", "R11", "R10", "R8", "R1"],
        "direction": "westbound",
    },
    3: {  # Thursday: Southwest distribution
        "corridor": "Southwest Distribution",
        "regions": ["R1", "R2", "R3"],
        "direction": "bidirectional",
    },
    4: {  # Friday: Ottawa / flex
        "corridor": "Ottawa Flex",
        "regions": ["R1", "R12"],
        "direction": "bidirectional",
    },
}

# Phase 1 sellable lines: (origin_region, dest_region) → (pickup_day_name, delivery_day_name)
SELLABLE_SCHEDULE = {
    ("R2","R1"): ("Monday", "Monday"),
    ("R2","R13"): ("Monday", "Tuesday"),
    ("R3","R1"): ("Monday", "Monday"),
    ("R3","R13"): ("Monday", "Tuesday"),
    ("R1","R8"): ("Tuesday", "Tuesday"),
    ("R1","R10"): ("Tuesday", "Tuesday"),
    ("R1","R11"): ("Tuesday", "Tuesday"),
    ("R1","R13"): ("Tuesday", "Tuesday"),
    ("R1","R14"): ("Tuesday", "Tue/Wed"),
    ("R1","R15"): ("Tuesday", "Tue/Wed"),
    ("R13","R1"): ("Wednesday", "Wed/Thu"),
    ("R13","R2"): ("Wednesday", "Thursday"),
    ("R15","R1"): ("Wednesday", "Wed/Thu"),
    ("R1","R2"): ("Thursday", "Thursday"),
    ("R1","R3"): ("Thursday", "Thursday"),
    ("R1","R12"): ("Friday", "Friday"),
}

DAY_INDEX = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}


class RoutingResult:
    def __init__(self, strategy, pickup_date=None, delivery_date=None, reason=None, via_hub=None):
        self.strategy = strategy
        self.pickup_date = pickup_date
        self.delivery_date = delivery_date
        self.reason = reason
        self.via_hub = via_hub


class RoutingService:
    def __init__(self, env):
        self.env = env(su=True)

    def determine_routing(self, origin_region_code, dest_region_code, reference_dt=None):
        """Returns RoutingResult with the best routing strategy for this lane."""
        if reference_dt is None:
            reference_dt = datetime.datetime.now(tz=BUSINESS_TZ)

        lane_key = (origin_region_code, dest_region_code)

        # 1. Check if this is a direct sellable line (Phase 1)
        sellable = SELLABLE_SCHEDULE.get(lane_key)
        if sellable:
            pickup_day_name, delivery_day_name = sellable
            # Compute next pickup date
            pickup_weekday = DAY_INDEX.get(pickup_day_name)
            if pickup_weekday is not None:
                pickup_date = self._next_weekday(reference_dt, pickup_weekday)
                # Delivery: same day or next business day
                if delivery_day_name == pickup_day_name:
                    delivery_date = pickup_date
                elif "Tue/Wed" in delivery_day_name:
                    delivery_date = pickup_date + datetime.timedelta(days=1)
                else:
                    delivery_weekday = DAY_INDEX.get(delivery_day_name, pickup_weekday + 1)
                    delivery_date = self._next_weekday(pickup_date, delivery_weekday)
                return RoutingResult("direct", pickup_date, delivery_date,
                                     via_hub=None if origin_region_code == "R1" or dest_region_code == "R1" else "R1")

        # 2. En-route: same corridor, same day
        for day_idx, template in WEEKLY_TEMPLATE.items():
            if origin_region_code in template["regions"] and dest_region_code in template["regions"]:
                if template["direction"] == "bidirectional":
                    pickup_date = self._next_weekday(reference_dt, day_idx)
                    return RoutingResult("en_route", pickup_date, pickup_date,
                                         reason="Same corridor, same day")

        # 3. Hub transfer: origin→R1, R1→destination
        if lane_key not in SELLABLE_SCHEDULE:
            # Check if origin→R1 is sellable and R1→dest is sellable
            to_hub = SELLABLE_SCHEDULE.get((origin_region_code, "R1"))
            from_hub = SELLABLE_SCHEDULE.get(("R1", dest_region_code))
            if to_hub and from_hub:
                pickup_day = DAY_INDEX.get(to_hub[0], 0)
                delivery_day = DAY_INDEX.get(from_hub[0], 1)
                pickup_date = self._next_weekday(reference_dt, pickup_day)
                delivery_date = self._next_weekday(pickup_date, delivery_day)
                if delivery_date <= pickup_date:
                    delivery_date = pickup_date + datetime.timedelta(days=1)
                return RoutingResult("hub_transfer", pickup_date, delivery_date,
                                     reason="Via GTA Hub (Mississauga)", via_hub="R1")

        # 4. Scheduled connection: check future days
        for day_offset in range(1, 8):
            check_date = reference_dt + datetime.timedelta(days=day_offset)
            check_weekday = check_date.weekday()
            if check_weekday < 5:  # Mon-Fri
                for day_idx, template in WEEKLY_TEMPLATE.items():
                    if day_idx == check_weekday:
                        if origin_region_code in template["regions"] and dest_region_code in template["regions"]:
                            return RoutingResult("scheduled_connection", check_date.date(), check_date.date(),
                                                 reason=f"Next available {check_date.strftime('%A')}")

        # 5. Custom quote
        return RoutingResult("custom_quote", reason="Outside scheduled network — requires manual quote")

    def _next_weekday(self, from_dt, target_weekday):
        """Return the next date matching target_weekday (0=Mon)."""
        current = from_dt.date() if hasattr(from_dt, 'date') else from_dt
        if hasattr(from_dt, 'date'):
            current_weekday = current.weekday()
        else:
            current_weekday = from_dt.weekday() if hasattr(from_dt, 'weekday') else 0

        days_ahead = target_weekday - current_weekday
        if days_ahead < 0:
            days_ahead += 7
        if days_ahead == 0 and hasattr(from_dt, 'hour') and from_dt.hour >= 16:
            days_ahead = 7  # past cutoff, next week
        return current + datetime.timedelta(days=days_ahead)

    # ── Phase 2: Full resolution chain (Postal Code → Dispatch) ──────

    def resolve_postal_code(self, postal_code):
        """Postal Code → FSA → Region → Lane(s).

        Returns dict: {fsa, region, outbound_lanes, inbound_lanes}
        """
        fsa_code = postal_code[:3].upper().replace(" ", "") if postal_code else ""
        fsa = self.env["logistics.fsa"].search([("fsa", "=", fsa_code)], limit=1)
        if not fsa:
            return None
        region = fsa.region_id
        if not region:
            return None
        outbound = self.env["logistics.lane"].search([
            ("origin_region_id", "=", region.id), ("active", "=", True)
        ])
        inbound = self.env["logistics.lane"].search([
            ("destination_region_id", "=", region.id), ("active", "=", True)
        ])
        return {"fsa": fsa, "region": region, "outbound_lanes": outbound, "inbound_lanes": inbound}

    def resolve_lane_to_corridor(self, lane):
        """Lane → Corridor(s) → Next Departure.

        Returns the next available departure for this lane, or None.
        """
        if not lane.corridor_ids:
            return None
        today = datetime.date.today()
        # Find the next departure within 14 days for any serving corridor
        departure = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "in", lane.corridor_ids.ids),
            ("departure_date", ">=", today),
            ("departure_date", "<=", today + datetime.timedelta(days=14)),
            ("active", "=", True),
            ("status", "=", "scheduled"),
        ], order="departure_date, departure_time", limit=1)
        return departure

    def full_resolve(self, pickup_postal, delivery_postal, **kwargs):
        """Complete resolution chain: Postal Code → FSA → Region → Lane → Corridor → Departure.

        Returns dict with all resolved entities plus estimated pickup/delivery dates.
        """
        pickup = self.resolve_postal_code(pickup_postal)
        delivery = self.resolve_postal_code(delivery_postal)
        if not pickup or not delivery:
            return {"available": False, "reason": "Could not resolve one or both postal codes to an FSA/region."}

        # Find the lane
        lane = self.env["logistics.lane"].search([
            ("origin_region_id", "=", pickup["region"].id),
            ("destination_region_id", "=", delivery["region"].id),
            ("active", "=", True),
        ], limit=1)

        if not lane:
            # Try hub-and-spoke: pickup→hub→delivery
            hub_region = self.env["logistics.region"].search([("code", "=", "R1")], limit=1)
            leg1 = self.env["logistics.lane"].search([
                ("origin_region_id", "=", pickup["region"].id),
                ("destination_region_id", "=", hub_region.id),
                ("active", "=", True),
            ], limit=1) if hub_region else None
            leg2 = self.env["logistics.lane"].search([
                ("origin_region_id", "=", hub_region.id),
                ("destination_region_id", "=", delivery["region"].id),
                ("active", "=", True),
            ], limit=1) if hub_region else None
            if leg1 and leg2:
                dep1 = self.resolve_lane_to_corridor(leg1)
                dep2 = self.resolve_lane_to_corridor(leg2)
                return {
                    "available": True,
                    "routing": "hub_transfer",
                    "via_hub": hub_region,
                    "pickup_region": pickup["region"],
                    "delivery_region": delivery["region"],
                    "leg1_lane": leg1,
                    "leg1_departure": dep1,
                    "leg2_lane": leg2,
                    "leg2_departure": dep2,
                    "pickup_date": dep1.departure_date if dep1 else None,
                    "delivery_date": dep2.departure_date if dep2 else None,
                }
            return {"available": False, "reason": "No lane connects these regions."}

        departure = self.resolve_lane_to_corridor(lane)
        return {
            "available": True,
            "routing": "direct" if not lane.via_hub_id else "hub_transfer",
            "pickup_region": pickup["region"],
            "delivery_region": delivery["region"],
            "lane": lane,
            "corridor": departure.corridor_id if departure else None,
            "departure": departure,
            "pickup_date": departure.departure_date if departure else None,
            "delivery_date": (departure.departure_date + datetime.timedelta(
                days=1 if departure.corridor_id.overnight else 0
            )) if departure else None,
        }

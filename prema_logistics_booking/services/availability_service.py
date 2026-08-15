"""Customer calendar adapter over the canonical pricing/resolver flow.

No routing, pricing or capacity policy is duplicated here.  The calendar asks
PricingService for the exact itinerary and CapacityEngine for remaining space.
"""
import datetime
from zoneinfo import ZoneInfo

from .temperature_compat import REEFER, to_canonical_temperature_mode


BUSINESS_TZ = ZoneInfo("America/Toronto")
MAX_WEEKS_AHEAD = 8


class DeliveryOption:
    def __init__(self, priority, delivery_date, pickup_date, price,
                 service_label="", routing_strategy="", departure=None,
                 transfer_departure=None, temperature_mode="dry",
                 available_pallets=0, capacity_ok=True, reason=""):
        self.priority = priority
        self.delivery_date = delivery_date
        self.pickup_date = pickup_date
        self.price = price
        self.service_label = service_label
        self.routing_strategy = routing_strategy
        self.route_run = None
        self.departure = departure
        self.transfer_departure = transfer_departure
        self.departure_ids = [
            record.id for record in (departure, transfer_departure) if record
        ]
        self.temperature_mode = temperature_mode
        self.available_pallets = available_pallets
        self.capacity_ok = capacity_ok
        self.reason = reason


class SchedulerAvailabilityResult:
    def __init__(self):
        self.options = []
        self.current_week_label = ""
        self.available_weeks = []


class ScheduledAvailabilityService:
    def __init__(self, env):
        self.env = env(su=True)

    def find_available_services(self, pickup_fsa, delivery_fsa, pallets, weight_lbs,
                                 temperature_mode="dry", requested_week_offset=0,
                                 required_temperature_c=None):
        result = SchedulerAvailabilityResult()
        temperature_mode = to_canonical_temperature_mode(temperature_mode)
        weeks = self._generate_weeks(datetime.datetime.now(tz=BUSINESS_TZ))
        result.available_weeks = [week["label"] for week in weeks]
        offset = min(max(int(requested_week_offset or 0), 0), MAX_WEEKS_AHEAD - 1)
        target = weeks[offset]
        result.current_week_label = target["label"]

        if temperature_mode == REEFER and required_temperature_c is None:
            return result
        if not pickup_fsa or not delivery_fsa:
            return result

        from .pricing_service import PricingService
        earliest = max(target["start"], datetime.datetime.now(tz=BUSINESS_TZ).date())
        priced = PricingService(self.env).calculate(
            pickup_fsa, delivery_fsa, "ltl", temperature_mode,
            pallets, weight_lbs,
            required_temperature_c=required_temperature_c,
            resolve_departures=True,
            reference_dt=earliest,
        )
        if not priced.available or not priced.pickup_date:
            result.options.append(DeliveryOption(
                priority="custom_quote", delivery_date=None, pickup_date=None,
                price=0.0, routing_strategy="custom_quote",
                service_label="No scheduled LTL departure is available in this week.",
                reason=priced.reason or "no_scheduled_service",
            ))
            return result
        if not target["start"] <= priced.pickup_date <= target["end"]:
            return result

        leg_snapshots = (priced.route_snapshot or {}).get("legs") or []
        dep_ids = [leg.get("departure_id") for leg in leg_snapshots if leg.get("departure_id")]
        departures = self.env["logistics.corridor.departure"].browse(dep_ids).exists()
        ordered_departures = []
        for leg in leg_snapshots:
            dep_id = leg.get("departure_id")
            dep = departures.filtered(lambda d, wanted=dep_id: d.id == wanted)[:1] if dep_id else False
            if dep:
                ordered_departures.append(dep)

        remaining = self._immediately_bookable_pallets(ordered_departures)
        is_transfer = len(ordered_departures) > 1
        result.options.append(DeliveryOption(
            priority="hub_connected_current_week" if is_transfer else "scheduled_current_week",
            delivery_date=priced.delivery_date_estimate,
            pickup_date=priced.pickup_date,
            price=priced.calculated_price,
            service_label=(
                "Scheduled LTL via Hub" if is_transfer else
                f"Scheduled LTL — {priced.corridor.name}"
            ),
            routing_strategy="hub_transfer" if is_transfer else "direct",
            departure=ordered_departures[0] if ordered_departures else None,
            transfer_departure=ordered_departures[1] if len(ordered_departures) > 1 else None,
            temperature_mode=temperature_mode,
            available_pallets=remaining,
            capacity_ok=remaining >= pallets,
        ))
        return result

    @staticmethod
    def _generate_weeks(now):
        monday = now.date() - datetime.timedelta(days=now.weekday())
        weeks = []
        for offset in range(MAX_WEEKS_AHEAD):
            start = monday + datetime.timedelta(weeks=offset)
            end = start + datetime.timedelta(days=6)
            if offset == 0:
                label = f"Current Week — {start:%b %d} – {end:%b %d}"
            elif offset == 1:
                label = f"Next Week — {start:%b %d} – {end:%b %d}"
            else:
                label = f"Week of {start:%b %d} – {end:%b %d}"
            weeks.append({"label": label, "start": start, "end": end, "offset": offset})
        return weeks

    def _immediately_bookable_pallets(self, departures):
        if not departures:
            return 0
        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        remaining = []
        for departure in departures:
            vehicle = departure.vehicle_id
            if not vehicle:
                return 0
            capacity = engine.vehicle_booking_capacity(vehicle, allow_pinwheel_override=False)
            if not capacity:
                return 0
            peak = engine.compute_departure_peak(departure)
            remaining.append(max(0, capacity - peak["peak_pallets"]))
        return min(remaining) if remaining else 0

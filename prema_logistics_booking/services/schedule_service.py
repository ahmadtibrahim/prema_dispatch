"""Schedule engine — Layer 1 (theoretical, schedule-based availability).

Pure logic, no ORM writes. Given a service offering, computes the next
actual available pickup date (respecting weekday pattern + cutoff + holiday
calendars) and the resulting estimated delivery date (respecting the
offering's delivery-offset rule). No hard-coded weekday/holiday data lives
here — everything is read from logistics.lane.schedule /
logistics.holiday.calendar records.
"""

import datetime
from zoneinfo import ZoneInfo

# Server clock is UTC; the business (and its cutoff times) operate on
# Toronto wall-clock time. Never compare against bare UTC now() here — see
# prema_dispatch's own CLAUDE.md for the same class of bug already fixed
# once in that module.
BUSINESS_TZ = ZoneInfo("America/Toronto")

MAX_LOOKAHEAD_DAYS = 30

WEEKDAY_FIELDS = [
    "pickup_monday", "pickup_tuesday", "pickup_wednesday", "pickup_thursday",
    "pickup_friday", "pickup_saturday", "pickup_sunday",
]  # index 0 = Monday, matching date.weekday()


class ScheduleResult:
    def __init__(self, available, reason=None, schedule=None, pickup_date=None, delivery_date=None):
        self.available = available
        self.reason = reason
        self.schedule = schedule
        self.pickup_date = pickup_date
        self.delivery_date = delivery_date


class ScheduleService:
    def __init__(self, env):
        # sudo(): same reasoning as PricingService -- reads schedule/holiday
        # config a customer has no direct ACL on; authorization is the
        # caller's responsibility, not this internal calculation.
        self.env = env(su=True)

    def _active_schedule(self, service_offering, on_date):
        Schedule = self.env["logistics.lane.schedule"]
        domain = [
            ("service_offering_id", "=", service_offering.id),
            ("active", "=", True),
            "|", ("effective_from", "=", False), ("effective_from", "<=", on_date),
            "|", ("effective_to", "=", False), ("effective_to", ">=", on_date),
        ]
        return Schedule.search(domain, limit=1)

    def _holiday_dates(self, schedule):
        dates = set()
        for cal in schedule.holiday_calendar_ids:
            dates.update(line.date for line in cal.line_ids)
        return dates

    def next_pickup_and_delivery(self, service_offering, reference_dt=None):
        """Returns a ScheduleResult. reference_dt defaults to now (server tz)."""
        if reference_dt is None:
            reference_dt = datetime.datetime.now(tz=BUSINESS_TZ)
        elif reference_dt.tzinfo is None:
            reference_dt = reference_dt.replace(tzinfo=BUSINESS_TZ)
        else:
            reference_dt = reference_dt.astimezone(BUSINESS_TZ)

        schedule = self._active_schedule(service_offering, reference_dt.date())
        if not schedule:
            return ScheduleResult(False, reason="not_configured")

        holidays = self._holiday_dates(schedule)
        cutoff_hour = int(schedule.cutoff_time or 0)
        cutoff_minute = int(round((schedule.cutoff_time or 0) % 1 * 60))
        cutoff_today = reference_dt.replace(hour=cutoff_hour, minute=cutoff_minute, second=0, microsecond=0)

        candidate = reference_dt.date()
        # If checking "today" and we're already past cutoff, start looking from tomorrow.
        if reference_dt >= cutoff_today:
            candidate += datetime.timedelta(days=1)

        pickup_date = None
        for _ in range(MAX_LOOKAHEAD_DAYS):
            if candidate in holidays:
                candidate += datetime.timedelta(days=1)
                continue
            weekday_field = WEEKDAY_FIELDS[candidate.weekday()]
            if schedule[weekday_field]:
                pickup_date = candidate
                break
            candidate += datetime.timedelta(days=1)

        if not pickup_date:
            return ScheduleResult(False, reason="no_pickup_in_window", schedule=schedule)

        delivery_date = self._compute_delivery_date(schedule, pickup_date, holidays)
        return ScheduleResult(True, schedule=schedule, pickup_date=pickup_date, delivery_date=delivery_date)

    def _compute_delivery_date(self, schedule, pickup_date, holidays):
        offset_type = schedule.delivery_offset_type
        if offset_type == "same_day":
            candidate = pickup_date
        elif offset_type == "next_day":
            candidate = pickup_date + datetime.timedelta(days=1)
        elif offset_type == "scheduled_days":
            candidate = pickup_date + datetime.timedelta(days=schedule.delivery_offset_days or 0)
        else:  # next_business_day
            candidate = pickup_date + datetime.timedelta(days=1)

        skip_weekends = offset_type == "next_business_day"
        for _ in range(MAX_LOOKAHEAD_DAYS):
            is_weekend = candidate.weekday() >= 5
            if candidate in holidays or (skip_weekends and is_weekend):
                candidate += datetime.timedelta(days=1)
                continue
            break
        return candidate

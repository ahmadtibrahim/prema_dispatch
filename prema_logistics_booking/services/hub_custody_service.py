"""One configurable hub-custody policy for every wait at a hub.

Before this service three separate code paths each hard-coded "24 hours":
the calendar's plan_route, its _probe_legs twin, and the departure
resolver's _resolve_transfer. They agreed by accident, and none of them
could express the real operating week — the Friday GTA → Eastern Ontario
loop returns to the Mississauga hub on Friday evening, and its freight
rides the same truck onward on MONDAY's Windsor corridor. That gap is
about 60 hours, so the Friday → Monday connection could never be offered
even though the truck is parked at the hub all weekend.

This service is the single authority for:

  • how long freight may wait at a hub  (hold_hours / max_hold_hours /
    custody_hold_ok) — measured from the REAL arrival at the hub to the
    REAL onward departure, in the operational timezone, so a hold that
    spans a DST change is measured in real elapsed time rather than by
    subtracting two wall clocks;
  • whether that freight physically stays ON THE TRUCK or is unloaded at
    the hub (custody_mode) — which decides whether it keeps occupying
    the truck across the gap days.

Both consumers (the calendar's _probe_legs and the departure resolver's
_resolve_transfer) call the same methods, so the calendar and Get Price
can never disagree on an offered connection.

Configuration (Settings → Prema AI → Logistics Settings → Hub Custody
Policy), all with the historical 24h as the default:

  logistics.hub_custody_max_hours         24.0 — the ordinary cap
  logistics.hub_custody_weekend_max_hours 72.0 — only for a hold that
      arrives Fri/Sat/Sun and departs MONDAY (the weekend bridge)
  logistics.hub_custody_on_truck_max_hours 24.0 — above this the freight
      is assumed unloaded at the hub
"""

import logging
from datetime import datetime

from odoo import _

_logger = logging.getLogger(__name__)

# Operational timezone — the same parameter the routing service reads.
_OPS_TZ_PARAM = "prema_logistics_booking.operational_tz"
_DEFAULT_TZ = "America/Toronto"

MAX_HOLD_PARAM = "logistics.hub_custody_max_hours"
WEEKEND_MAX_HOLD_PARAM = "logistics.hub_custody_weekend_max_hours"
ON_TRUCK_MAX_HOLD_PARAM = "logistics.hub_custody_on_truck_max_hours"

DEFAULT_MAX_HOLD_HOURS = 24.0
DEFAULT_WEEKEND_MAX_HOLD_HOURS = 72.0
DEFAULT_ON_TRUCK_MAX_HOLD_HOURS = 24.0

CUSTODY_ON_TRUCK = "on_truck"
CUSTODY_UNLOADED = "unloaded_at_hub"


class HubCustodyService:
    """Stateless helper — constructed per operation with an env."""

    def __init__(self, env):
        self.env = env

    # ── Configuration ────────────────────────────────────────────────

    def _param_float(self, key, default):
        raw = self.env["ir.config_parameter"].sudo().get_param(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def max_hold_hours(self):
        """The ordinary custody cap (hours)."""
        return self._param_float(MAX_HOLD_PARAM, DEFAULT_MAX_HOLD_HOURS)

    def weekend_max_hold_hours(self):
        """The cap for a hold that bridges the weekend into Monday."""
        return self._param_float(
            WEEKEND_MAX_HOLD_PARAM, DEFAULT_WEEKEND_MAX_HOLD_HOURS)

    def on_truck_max_hold_hours(self):
        """Above this a hold is assumed to be unloaded at the hub."""
        return self._param_float(
            ON_TRUCK_MAX_HOLD_PARAM, DEFAULT_ON_TRUCK_MAX_HOLD_HOURS)

    # ── Timezone-aware hold measurement ──────────────────────────────

    def _ops_tz(self):
        import pytz
        name = self.env["ir.config_parameter"].sudo().get_param(
            _OPS_TZ_PARAM, _DEFAULT_TZ)
        try:
            return pytz.timezone(name)
        except Exception:
            return pytz.timezone(_DEFAULT_TZ)

    def localize(self, value):
        """Interpret a business-local datetime in the operational timezone.

        Every datetime this policy receives is built from a business-local
        calendar date plus a float clock hour (corridor departure_time,
        stop planned times) — never a stored UTC value. Localizing (rather
        than assuming UTC) is what makes the elapsed-hour math correct
        across a DST change. Naive values only; an aware value is
        converted as-is.
        """
        import pytz
        if value is None:
            return None
        if isinstance(value, str):
            value = self._parse(value)
            if value is None:
                return None
        zone = self._ops_tz()
        if value.tzinfo is not None:
            return value.astimezone(zone)
        try:
            return zone.localize(value, is_dst=None)
        except Exception:
            # Ambiguous or non-existent local hour (DST gap) — pytz picks
            # a valid offset; the hold stays measurable either way.
            return zone.localize(value)

    @staticmethod
    def _parse(value):
        """ISO string → naive datetime (corridor locals are ISO in the
        route snapshot)."""
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    def hold_hours(self, arrival, departure):
        """Real elapsed hours between arrival at the hub and the onward
        departure, measured in the operational timezone.

        Returns None when either side is unknown — callers treat an
        unknown hold as 'no custody cap applies', exactly as the previous
        `if leg1_arrival_dt and leg2_dep_dt` guards did.
        """
        arrival_local = self.localize(arrival)
        departure_local = self.localize(departure)
        if arrival_local is None or departure_local is None:
            return None
        return (departure_local - arrival_local).total_seconds() / 3600.0

    def allowed_hold_hours(self, arrival, departure):
        """The cap that applies to THIS hold.

        A hold that arrives Fri/Sat/Sun and departs on a MONDAY is the
        weekend bridge (the Friday loop returning to the hub, continuing
        on Monday's corridor): it gets the weekend cap. Everything else —
        including a Thursday arrival waiting for Monday, which is not the
        documented Friday→Monday connection — gets the ordinary cap.
        """
        arrival_local = self.localize(arrival)
        departure_local = self.localize(departure)
        if departure_local is not None and departure_local.weekday() == 0:
            if arrival_local is not None and arrival_local.weekday() in (4, 5, 6):
                return self.weekend_max_hold_hours()
        return self.max_hold_hours()

    def custody_hold_ok(self, arrival, departure):
        """True when the connection's hub wait is within policy."""
        hold = self.hold_hours(arrival, departure)
        if hold is None:
            return True
        allowed = self.allowed_hold_hours(arrival, departure)
        if hold > allowed:
            _logger.info(
                "Hub custody: %.1fh hold exceeds the %.1fh policy cap "
                "(arrival %s → departure %s)", hold, allowed, arrival, departure)
            return False
        return True

    # ── On-truck vs unloaded ─────────────────────────────────────────

    def custody_mode(self, arrival, departure, same_vehicle):
        """How the freight waits at the hub.

        'on_truck' only when the SAME vehicle carries the onward leg AND
        the wait is inside the on-truck threshold — a driver's truck is
        not a warehouse, and freight on a truck that leaves is not at the
        hub. Everything else is 'unloaded_at_hub', the safe default.
        """
        if not same_vehicle:
            return CUSTODY_UNLOADED
        hold = self.hold_hours(arrival, departure)
        if hold is not None and hold > self.on_truck_max_hold_hours():
            return CUSTODY_UNLOADED
        return CUSTODY_ON_TRUCK

    def describe_policy(self):
        """Human-readable policy line for the booking chatter."""
        return _(
            "Hub custody policy: %(base).0fh max hold (%(weekend).0fh when "
            "the hold bridges the weekend into Monday; on-truck up to "
            "%(on_truck).0fh)."
        ) % {
            "base": self.max_hold_hours(),
            "weekend": self.weekend_max_hold_hours(),
            "on_truck": self.on_truck_max_hold_hours(),
        }

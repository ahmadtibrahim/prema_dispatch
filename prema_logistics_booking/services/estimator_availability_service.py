"""Estimator availability — vehicle/driver occupancy over a FULL operational
interval (master §11.3).

The Estimator scenario cards must never assert "truck free" from pickup
time alone.  This service checks the whole pickup-through-delivery (plus
return-home) interval against the truck's committed dispatch work, exposes
the free-capacity authority (CapacityEngine) and normalizes ELD/duty data
sent by the AI side (EldAdapter) into availability statements.

Rules:
  - Occupancy source: prema.dispatch.job rows for the vehicle that are not
    cancelled/completed and whose scheduled span overlaps the interval.
    Weekly-plan reservations (planned occurrences on the same vehicle/date)
    are counted exactly like the planner board counts them.
  - No departure/booking mutation here — this is a read-only advisor.
"""

import datetime
from zoneinfo import ZoneInfo

_BUSINESS_TZ_DEFAULT = "America/Toronto"


def company_tz(env):
    """IANA zone for dispatch planning (company → user → Toronto)."""
    name = env.company.partner_id.tz or env.user.tz or _BUSINESS_TZ_DEFAULT
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(_BUSINESS_TZ_DEFAULT)


def to_utc_naive(local_dt, tz):
    """Naive local datetime → naive UTC (Odoo stores naive UTC).

    ``tz`` may be a zoneinfo.ZoneInfo (company_tz below) or a pytz tz;
    ZoneInfo has no ``.localize``, so attach the zone with ``replace``
    (pytz still accepts the attached tzinfo on astimezone)."""
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    return local_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def to_local_naive(utc_naive_dt, tz):
    """Naive UTC → naive local datetime."""
    if utc_naive_dt.tzinfo is not None:
        utc_naive_dt = utc_naive_dt.replace(tzinfo=None)
    return utc_naive_dt.replace(tzinfo=datetime.timezone.utc).astimezone(tz).replace(tzinfo=None)


def local_datetime(date, hour_float, tz):
    """Local naive datetime from date + float hour (e.g. 8.5 → 08:30)."""
    hours = int(hour_float or 0.0)
    minutes = int(round(((hour_float or 0.0) - hours) * 60))
    return datetime.datetime.combine(date, datetime.time.min) + datetime.timedelta(
        hours=hours, minutes=minutes)


def fmt_hours(hour_float):
    """Float hour → 'HH:MM' or '' (shared by scenario rows)."""
    if not hour_float:
        return ""
    return "%02d:%02d" % (int(hour_float), int(round((hour_float % 1) * 60)))


def fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class EstimatorAvailabilityService:
    """Occupancy + capacity advisory for one truck and one interval."""

    def __init__(self, env):
        self.env = env(su=True)

    def _param(self, key, default):
        try:
            return float(self.env["ir.config_parameter"].sudo().get_param(
                key, str(default)) or default)
        except Exception:
            return float(default)

    def hos_limits(self):
        return {
            "max_drive_hours_per_day": self._param(
                "logistics.hos_max_drive_hours_per_day", 11.0),
            "max_shift_hours_per_day": self._param(
                "logistics.hos_max_shift_hours_per_day", 14.0),
            "workday_start_hour": self._param(
                "logistics.workday_start_hour", 8.0),
        }

    # ── Occupancy ───────────────────────────────────────────────────

    def vehicle_jobs(self, vehicle_id, utc_start, utc_end):
        """Committed dispatch work overlapping the UTC interval."""
        Job = self.env["prema.dispatch.job"]
        if "prema.dispatch.job" not in self.env.registry:
            return Job
        return Job.search([
            ("vehicle_id", "=", vehicle_id),
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "!=", True),
            ("scheduled_pickup", "<=", utc_end),
            "|",
            ("scheduled_delivery", "=", False),
            ("scheduled_delivery", ">=", utc_start),
        ], order="scheduled_pickup asc, id asc")

    def planned_reservations(self, vehicle_id, local_start, local_end):
        """Weekly-planner planned occurrences on the truck in date range."""
        Reservation = self.env["logistics.weekly.plan.reservation"]
        if "logistics.weekly.plan.reservation" not in self.env.registry:
            return Reservation
        return Reservation.search([
            ("vehicle_id", "=", vehicle_id),
            ("state", "=", "planned"),
            ("plan_date", ">=", local_start),
            ("plan_date", "<=", local_end),
        ], order="plan_date asc, id asc")

    def occupancy_conflicts(self, vehicle_id, local_start, local_end,
                            tz=None):
        """Full-interval overlap report (read-only, never mutates).
        Times are naive LOCAL (business tz); they are converted to naive
        UTC for the Odoo-side interval comparison."""
        tz = tz or company_tz(self.env)
        utc_start = to_utc_naive(local_start, tz)
        utc_end = to_utc_naive(local_end, tz)
        jobs = self.vehicle_jobs(vehicle_id, utc_start, utc_end)
        reservations = self.planned_reservations(
            vehicle_id, local_start.date(), local_end.date())
        conflicts = []
        for job in jobs:
            conflicts.append({
                "kind": "job",
                "id": job.id,
                "label": job.name or job.tracking_number or "Job %s" % job.id,
                "reference": job.tracking_number or "",
                "start": fmt_date(job.scheduled_pickup),
                "end": fmt_date(job.scheduled_delivery),
                "stage": job.stage_id.name or "",
            })
        for res in reservations:
            conflicts.append({
                "kind": "planned",
                "id": res.id,
                "label": res.name or "Planned occurrence",
                "reference": res.recurring_job_id.name or res.agreement_id.name or "",
                "start": fmt_date(res.plan_date),
                "end": "",
                "stage": "planned",
                "pallets": res.pallets,
            })
        return conflicts

    def first_free_date(self, vehicle_id, from_date, max_days=21, tz=None,
                        interval_hours=12.0):
        """Earliest calendar date whose whole workday is free, scanning
        forward from from_date. Pure advisory (driver duty still to verify)."""
        tz = tz or company_tz(self.env)
        day = from_date
        for _ in range(max_days):
            start = local_datetime(day, 0.0, tz)
            end = local_datetime(day, 23.0, tz) + datetime.timedelta(minutes=59)
            if not self.occupancy_conflicts(vehicle_id, start, end, tz):
                return day
            day += datetime.timedelta(days=1)
        return False

    # ── Capacity warnings (§11.4/§11.5) ─────────────────────────────

    def capacity_report(self, vehicle, pallets, weight_lbs, equipment="dry",
                        liftgate_pickup=False, liftgate_delivery=False):
        """CapacityEngine verdict translated into honest warnings/blocks.

        Returns dict {ok, layout, max_pallets, payload_lbs, warnings,
        blocking, pinwheel_override} — never asserts a load fits when data
        is missing (the engine reads the vehicle's configured layouts and
        payload, not hardcoded defaults).
        """
        from .capacity_engine import CapacityEngine
        result = CapacityEngine(self.env).evaluate(
            pallets or 0, weight_lbs or 0.0,
            vehicle=vehicle,
            requires_reefer=equipment == "reefer",
            requires_liftgate=bool(liftgate_pickup or liftgate_delivery),
        )
        payload = vehicle.x_max_payload_lbs or 0.0
        max_pallets = int(vehicle.straight_pallet_capacity or 12)
        warnings = []
        blocking = []
        if not vehicle.x_operational_logistics:
            blocking.append("Truck is not flagged operational for logistics.")
        if result.reason_code == "reefer_required":
            blocking.append("Shipment needs a reefer — this truck has no reefer unit.")
        if result.reason_code == "liftgate_required":
            blocking.append("Liftgate requested — this truck has no liftgate.")
        if result.reason_code == "payload_exceeded":
            blocking.append(
                "Load weight %.0f lb exceeds payload capacity %.0f lb."
                % (weight_lbs or 0.0, payload or 0.0))
        if result.reason_code == "pallet_capacity_exceeded":
            blocking.append(
                "Shipment needs more than %d positions — exceeds the truck's "
                "maximum physical capacity." % max_pallets)
        if result.reason_code == "pinwheel_override_required":
            warnings.append(
                "%d positions needs the pin-wheel layout — dispatcher "
                "override required before booking." % (pallets or 0))
        if not pallets and not weight_lbs:
            warnings.append(
                "No pallet/weight quantities known — capacity cannot be "
                "verified (do not assume the load fits).")
        elif not weight_lbs:
            warnings.append(
                "Weight is unknown — payload feasibility is unverified.")
        if not payload:
            warnings.append(
                "This truck has no configured payload (x_max_payload_lbs) — "
                "weight feasibility unverified.")
        return {
            "ok": not blocking and not bool(result.reason_code in (
                "no_vehicle_provided", "equipment_not_operational")),
            "eligible": bool(result.eligible),
            "layout": result.layout,
            "max_pallets": max_pallets,
            "payload_lbs": payload,
            "pinwheel_override": bool(result.manual_review),
            "warnings": warnings,
            "blocking": blocking,
        }

    # ── Duty/ELD advisory (engine sends normalized EldAdapter output) ──

    def eld_advisory(self, driver_status, start_date=None):
        """driver_status = normalized dict from the engine EldAdapter.

        ``start_date`` is the proposed start date in the BUSINESS timezone
        (datetime.date).  The "currently driving/on duty" fact only gates a
        start TODAY — for a later start the driver will log off before it,
        so the current duty state downgrades to a warning (the duty at the
        future start moment is unknowable and must be re-verified)."""
        out = {"warnings": [], "blocking": []}
        if not driver_status:
            out["warnings"].append(
                "No ELD/driver status supplied by the AI side — duty "
                "availability unverified.")
            return out
        if not driver_status.get("driver_id"):
            out["warnings"].append(driver_status.get("warnings") and
                                   driver_status["warnings"][0] or
                                   "No driver assigned to this truck.")
        duty = driver_status.get("duty") or "unknown"
        out["duty"] = duty
        local_today = datetime.datetime.now(company_tz(self.env)).date()
        if duty in ("driving", "on_duty", "sleeper"):
            if start_date is None or start_date == local_today:
                out["blocking"].append(
                    "Driver is currently %s — a same-day start is not "
                    "commit-able until the driver logs off duty."
                    % duty.replace("_", " "))
            else:
                out["warnings"].append(
                    "Driver is currently %s — the duty state at a %s "
                    "start cannot be guaranteed; re-verify before "
                    "dispatch." % (duty.replace("_", " "),
                                   start_date.isoformat()))
        if not driver_status.get("fresh", False):
            out["warnings"].append(
                "ELD position/sync data is stale — verify the truck's "
                "current position before dispatch.")
        if not driver_status.get("maintenance_known", False):
            out["warnings"].append(
                "No maintenance-due data exists for this truck.")
        out["warnings"].extend(driver_status.get("warnings") or [])
        return out

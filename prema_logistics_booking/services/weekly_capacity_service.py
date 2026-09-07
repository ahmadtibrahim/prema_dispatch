"""WeeklyCapacityService — the read/plan side of the Weekly Capacity
board (§TODO 9-12 of the 18-section work order).

One service behind the board's single data RPC:
  • get_week_board(week_start, truck_id)   → 7 day columns × trucks with
    REAL prema.dispatch.job cards (per-stop P1/D1 cards positioned by
    computed stop times; un-timed cards at the top in date order),
    per-truck/day capacity status reusing the canonical planner numbers,
    rolling per-stop onboard with cross-date carryover, holiday dates
    from the active logistics.holiday.calendar lines.
  • rolling_capacity_for(truck, day)       → stop-by-stop running onboard
    for one truck/day with carryover + exceed stops (the TODO 11 API).
  • evaluate_load(payload)                 → per truck/day AVAILABLE /
    AVAILABLE WITH WARNING / UNAVAILABLE, reusing the canonical checks
    (DispatchAvailabilityService schedule math, CapacityEngine evaluate,
    departure occupation) — never a private capacity model.

Design contracts (read carefully before changing):
  • This service NEVER writes. The board's mutations (assign / unassign /
    job day-time move) go through the canonical prema.dispatch.job RPCs
    (assign_job_to_truck / unassign_truck / weekly_capacity_move_job).
  • Times are never fabricated: a card shows stored engine ETAs
    (travel_arrival_at / facility_service_start_at / planned_departure_at
    / waiting_minutes) when they exist; jobs the engine has not persisted
    ETAs for are projected in memory with EtaEngine.project_forward_times
    (the same walk, read-only); anything else is an un-timed card.
  • Capacity numbers that the Dispatch Planner already owns are read from
    DispatchAvailabilityService.get_truck_day_schedule (committed_pallets,
    available_capacity, status, load-plan layout capacity override) — the
    board header badges mirror the planner. The per-stop rolling function
    is a DIFFERENT, finer instrument (it answers "at which stop does this
    truck exceed?"), and its per-stop deltas mirror the canonical
    job-level onboard math (_compute_onboard_load semantics: a pickup
    with pallets_in = 0 is inferred from the job's following drop-offs;
    freight unloads only where recorded).
  • Cross-date carryover is computed from REAL stops/items only: a job
    whose canonical running onboard is still > 0 after its last stop of
    day D-1 physically stays on its truck overnight; when the SAME truck
    also runs the split continuation card (same logistics_booking_id /
    booking_leg_id — the booking-split convention in
    logistics_booking._create_dispatch_job) on later days, that leftover
    is the continuation day's starting onboard, minus whatever the
    continuation card already delivered on intermediate days. No linking
    records are ever invented; a pair split across two trucks surfaces as
    an underflow/exceed warning instead of a fabricated carryover (see
    _carryover_pallets).
  • Loose/mixed freight counts follow the w7 semantics: an item's
    capacity_equivalent (floor positions) is used when > 0, the pallet
    fields otherwise — the same rule booking lines carry into dispatch
    items.
  • risk_reasons on a card is CONSUMED only ({severity: soft|hard, code,
    message} list, from the parallel risk-reasons work); absent → the
    board falls back to risk_level (red/yellow/green).
"""
import logging
from datetime import date as _date
from datetime import datetime, time as _time, timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — py3.8 fallback
    ZoneInfo = None

FALLBACK_TZ_NAME = "America/Toronto"

# stop_type → card side (pickup-like loads, delivery-like unloads).
PICKUP_LIKE = ("pickup", "cross_dock_pickup")
DELIVERY_LIKE = ("dropoff", "return", "transfer", "cross_dock_drop")


class WeeklyCapacityService:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Time helpers (naive-UTC Odoo datetimes ↔ local dates) ───────

    def _tz_name(self):
        ctx_tz = self.env.context.get("tz") or self.env.user.tz
        if ctx_tz:
            return ctx_tz
        company = self.env.company
        if company:
            # Odoo 18 res.company carries no tz — use the company
            # partner's tz, then its working calendar, then the shared
            # default. (Web calls always carry context tz; request-less
            # XML-RPC/cron callers with a tz-less user land here.)
            tz_name = company.partner_id.tz
            if not tz_name:
                calendar = getattr(company, "resource_calendar_id", None)
                if calendar:
                    tz_name = calendar.tz
            if tz_name:
                return tz_name
        return FALLBACK_TZ_NAME

    @staticmethod
    def _zone(tz_name):
        if ZoneInfo is not None:
            try:
                return ZoneInfo(tz_name)
            except Exception:
                pass
        from pytz import timezone as _tz
        return _tz(tz_name or FALLBACK_TZ_NAME)

    @classmethod
    def _as_local(cls, tz_name, dt):
        """Naive-UTC Odoo datetime → aware datetime in `tz_name`."""
        if dt is None:
            return None
        utc = cls._zone("UTC")
        aware_utc = (utc.localize(dt) if hasattr(utc, "localize")
                     else dt.replace(tzinfo=utc))
        return aware_utc.astimezone(cls._zone(tz_name or FALLBACK_TZ_NAME))

    @classmethod
    def _local_date_of(cls, tz_name, dt):
        """Local calendar date of a naive-UTC datetime (None-safe)."""
        if dt is None:
            return None
        return cls._as_local(tz_name, dt).date()

    def _local_clock(self, tz_name, dt):
        """'HH:MM' (stop-local clock) of a naive-UTC datetime — display
        strings are computed server-side so the browser needs no tz db."""
        if dt is None:
            return None
        return self._as_local(tz_name, dt).strftime("%H:%M")

    @classmethod
    def _local_day_utc_range(cls, tz_name, check_date):
        """(day_start_utc, day_end_utc) naive-UTC bounds of a local date."""
        zone = cls._zone(tz_name)
        if hasattr(zone, "localize"):
            start = zone.localize(datetime.combine(check_date, _time.min))
            end = zone.localize(datetime.combine(check_date, _time.max))
        else:
            start = datetime.combine(check_date, _time.min, tzinfo=zone)
            end = datetime.combine(check_date, _time.max, tzinfo=zone)
        return (start.astimezone(cls._zone("UTC")).replace(tzinfo=None),
                end.astimezone(cls._zone("UTC")).replace(tzinfo=None))

    @staticmethod
    def _iso(dt):
        """Naive-UTC → '…Z' ISO string for the browser (same convention as
        the driver payloads: an explicit Z keeps `new Date()` UTC)."""
        return (dt.isoformat() + "Z") if dt else None

    # ── Classification helpers ───────────────────────────────────────

    @staticmethod
    def _stop_kind(stop):
        if stop.stop_type in PICKUP_LIKE or (
                stop.stop_type == "transfer" and stop.pallets_in):
            return "pickup"
        if stop.stop_type in DELIVERY_LIKE:
            return "delivery"
        return "other"

    @staticmethod
    def _active_job_domain():
        return [
            ("active", "=", True),
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
        ]

    def _job_active_in_range(self, job, day_start, day_end):
        """Same overlap rule as DispatchAvailabilityService._job_active_
        in_range: pickup..delivery window overlaps the local day."""
        pickup = job.scheduled_pickup
        delivery = job.scheduled_delivery
        if not delivery and job.planned_delivery_date:
            delivery = datetime.combine(job.planned_delivery_date, _time.max)
        if not pickup and not delivery:
            return False
        pickup = pickup or delivery
        delivery = delivery or pickup
        if delivery < pickup:
            delivery = pickup
        return pickup <= day_end and delivery >= day_start

    def _stop_day(self, stop, job):
        """Local calendar date a stop belongs to — same rule as the
        planner's _stop_belongs_to_day: the stop's own scheduled_time
        wins; a pickup without one falls back to the job's pickup day,
        everything else to the job's delivery day."""
        reference = stop.scheduled_time
        if not reference:
            reference = (
                job.scheduled_pickup if stop.stop_type == "pickup"
                else (job.scheduled_delivery or job.scheduled_pickup))
        return self._local_date_of(
            stop.tz_name or FALLBACK_TZ_NAME, reference)

    # ── Labels (mirror the driver serializers) ───────────────────────

    def _company_name(self, stop):
        job_model = self.env["prema.dispatch.job"]
        if hasattr(job_model, "_stop_company_name"):
            return job_model._stop_company_name(stop)
        loc = stop.saved_location_id
        if loc:
            return (loc.business_name or loc.name or loc.address or "")
        return (stop.contact_name
                or (stop.partner_id.name if stop.partner_id else "")
                or ((stop.address or "").split(",")[0].strip()))

    def _city_of(self, stop, company):
        loc = stop.saved_location_id
        if loc and loc.city:
            return loc.city
        parts = [p.strip() for p in (stop.address or "").split(",")]
        if len(parts) >= 2:
            # "…, CITY, ON …" — the city sits before the province token.
            for idx in range(2, len(parts)):
                if len(parts[idx]) <= 2 and parts[idx].isalpha():
                    return parts[idx - 1]
            return parts[-2]
        return ""

    # ── Quantity helpers (w7 loose/mixed semantics) ──────────────────

    @staticmethod
    def _item_quantity(item):
        """Floor-position equivalent of one item: capacity_equivalent when
        set (> 0), else its pallet count — the canonical w7 rule."""
        if item.capacity_equivalent:
            return round(float(item.capacity_equivalent), 3)
        return item.pallet_count or 1

    def _stop_item_quantity(self, stop, side):
        """Σ item quantities loaded/unloaded at a stop, or None when the
        job carries no items (callers then fall back to pallet fields)."""
        items = (stop._items_picked_here() if side == "in"
                 else stop._items_delivered_here())
        items = items.filtered(lambda i: i.status != "cancelled")
        if not items:
            return None
        return round(sum(self._item_quantity(i) for i in items), 3)

    def _quantity_text(self, stop, job):
        """Human qty for a card: item floor-position equivalents when the
        job has items, the stop pallet fields otherwise (no mixing)."""
        has_items = bool(job.item_ids.filtered(
            lambda i: i.status != "cancelled"))
        qty = None
        qty_kind = "delivery"
        if stop.stop_type in PICKUP_LIKE or (
                stop.stop_type == "transfer" and stop.pallets_in):
            qty = (self._stop_item_quantity(stop, "in") if has_items
                   else stop.pallets_in)
            qty_kind = "pickup"
        elif stop.stop_type in DELIVERY_LIKE:
            qty = (self._stop_item_quantity(stop, "out") if has_items
                   else stop.pallets_out)
            qty_kind = "delivery"
        if qty is None:
            return None, qty_kind
        return (qty if qty not in (None, 0) else None), qty_kind

    # ── Per-job canonical running onboard (read-only mirror of the
    #    stored _compute_onboard_load on prema.dispatch.stop) ─────────

    @classmethod
    def _job_running_onboard(cls, job):
        """Per-stop canonical running onboard for one job: pickups add
        (pallets_in, or inferred from the job's following drop-offs when
        0), drop-off-like stops subtract pallets_out, never negative.
        Returns {stop.id: {"before": int, "after": int, "add": int,
        "sub": int}}."""
        stops = job.stop_ids.filtered(
            lambda s: not s.planning_only and s.status != "cancelled"
        ).sorted("sequence")
        result = {}

        def _effective_pickup(i, seq):
            s = seq[i]
            if s.stop_type == "cross_dock_pickup":
                return s.pallets_in or 0
            if s.stop_type != "pickup":
                return 0
            if s.pallets_in:
                return s.pallets_in
            nxt = next(
                (j for j in range(i + 1, len(seq))
                 if seq[j].stop_type == "pickup"), len(seq))
            return sum(seq[j].pallets_out or 0
                       for j in range(i + 1, nxt))

        running = 0
        for i, s in enumerate(stops):
            add = sub = 0
            if s.stop_type in ("pickup", "cross_dock_pickup"):
                add = _effective_pickup(i, stops)
                before = running
                running += add
            elif s.stop_type in DELIVERY_LIKE:
                sub = s.pallets_out or 0
                before = running
                running = max(0, running - sub)
            else:
                before = running
            result[s.id] = {
                "before": before, "after": running, "add": add, "sub": sub,
            }
        return result

    # ── Rolling capacity (TODO 11) ───────────────────────────────────

    def _day_effective_capacity(self, truck, check_date):
        """Real capacity of the truck for the day: the day's Load Plan
        layout when one exists, else the vehicle's own layout capacity —
        the same override the planner availability service applies."""
        plan = self.env["prema.dispatch.load.plan"].sudo().search([
            ("vehicle_id", "=", truck.id),
            ("operating_date", "=", check_date),
            ("active", "=", True),
        ], limit=1)
        if plan and hasattr(plan, "_vehicle_layout_capacity"):
            try:
                return plan._vehicle_layout_capacity() or 0
            except Exception:
                _logger.warning("load plan %s layout capacity failed",
                                plan.id)
        getter = getattr(truck, "get_layout_capacity", False)
        if callable(getter):
            try:
                return getter() or 0
            except Exception:
                pass
        return truck.x_max_pallets or 0

    def _split_continuations(self, job, truck, all_truck_jobs):
        """Sibling split cards of `job` on the same truck (real rows, no
        invented links): same logistics_booking_id and same
        booking_leg_id (both False on legacy customs pairs the booking
        alone). Returns the sibling recordset."""
        siblings = all_truck_jobs.filtered(
            lambda j2: j2.id != job.id
            and j2.vehicle_id.id == truck.id
            and j2.logistics_booking_id.id == job.logistics_booking_id.id)
        if job.booking_leg_id:
            siblings = siblings.filtered(
                lambda j2: j2.booking_leg_id.id == job.booking_leg_id.id)
        return siblings

    def _carryover_pallets(self, truck, check_date, tz_name):
        """Pallets physically onboard at 00:00 of `check_date` for `truck`,
        from REAL stops of the previous days (never invented links).

        Rule (module docstring): every active job of this truck keeps its
        canonical running onboard across midnight. A job whose running
        onboard is still > 0 after its last stop strictly BEFORE the day
        carries that leftover onto the day when the freight demonstrably
        continues on the SAME truck:
          • the job itself has stops on/after the day (one multi-day
            route), or
          • the booking-split continuation card(s) on this truck
            (logistics_booking_id / booking_leg_id match, same vehicle)
            still have deliveries on/after the day — the leftover minus
            what those siblings already delivered on earlier days.
        A leftover whose continuation runs on ANOTHER truck is NOT
        carried — the receiving truck must show a real loading/transfer
        stop; its absence surfaces as an underflow/exceed on that truck's
        day (a true planning gap). Returns (pallets, notes)."""
        notes = []
        if not truck:
            return 0, notes
        Job = self.env["prema.dispatch.job"]
        jobs = Job.search(self._active_job_domain() + [
            ("vehicle_id", "=", truck.id),
        ])
        if not jobs:
            return 0, notes
        day_start, _ = self._local_day_utc_range(tz_name, check_date)
        carry = 0
        for job in jobs:
            running = self._job_running_onboard(job)
            stops = job.stop_ids.filtered(
                lambda s: not s.planning_only and s.status != "cancelled"
            ).sorted("sequence")
            if not stops:
                continue
            last_before = False
            for s in stops:
                ref = s.scheduled_time or job.scheduled_pickup
                if ref and ref < day_start:
                    last_before = s
            if not last_before:
                continue
            leftover = running.get(last_before.id, {}).get("after", 0)
            if leftover <= 0:
                continue
            later_own_stop = any(
                (s.scheduled_time or job.scheduled_pickup or False)
                and (s.scheduled_time or job.scheduled_pickup) >= day_start
                for s in stops)
            siblings = self._split_continuations(job, truck, jobs)
            if not later_own_stop and not siblings:
                notes.append(
                    "%s ends %s with %s on board and no same-truck "
                    "continuation on %s — relay handoff is not visible "
                    "on this board." % (
                        job.name, last_before.scheduled_time
                        and self._local_date_of(
                            tz_name, last_before.scheduled_time)
                        or "its last day", leftover, check_date))
                continue
            contribution = leftover
            if not later_own_stop:
                # Sibling deliveries on earlier days already removed part
                # of the leftover: subtract the siblings' recorded unloads
                # whose stops fall in [first sibling day, the day before].
                sib_stops = siblings.mapped("stop_ids").filtered(
                    lambda s: not s.planning_only and s.status != "cancelled"
                    and s.stop_type in DELIVERY_LIKE)
                delivered_before = 0
                for s in sib_stops:
                    d = self._local_date_of(
                        s.tz_name or tz_name, s.scheduled_time or False) \
                        if s.scheduled_time else None
                    if d is not None and d < check_date:
                        delivered_before += s.pallets_out or 0
                contribution = max(0, leftover - delivered_before)
                if contribution <= 0:
                    continue
            carry += contribution
        return carry, notes

    def _day_stop_rows(self, truck, check_date, tz_name):
        """(rows, running_map, projections) — real stops of the day on the
        truck (per custody segment), each with computed display times.
        Times: stored engine ETAs win; otherwise the read-only forward
        projection (same walk, nothing written) for vehicle-anchored
        jobs; otherwise the stop is un-timed."""
        from .eta_engine import EtaEngine
        day_start, day_end = self._local_day_utc_range(tz_name, check_date)
        Job = self.env["prema.dispatch.job"]
        jobs = Job.search(self._active_job_domain() + [
            ("vehicle_id", "=", truck.id),
        ])
        jobs = jobs.filtered(
            lambda j: self._job_active_in_range(j, day_start, day_end))
        segment_stops = {}
        for job in jobs:
            segments = job._job_segments() if hasattr(job, "_job_segments") \
                else [{"vehicle": job.vehicle_id,
                       "stops": [(s, None) for s in job.stop_ids]}]
            for seg in segments:
                veh = seg.get("vehicle")
                if not veh or veh.id != truck.id:
                    continue
                for stop, _role in seg.get("stops", []):
                    if stop.planning_only or stop.status == "cancelled":
                        continue
                    if self._stop_day(stop, job) != check_date:
                        continue
                    segment_stops[stop.id] = job

        running_map = {}
        for job in jobs:
            running_map.update(self._job_running_onboard(job))

        engine = EtaEngine(self.env)
        projections = {}
        for job in jobs:
            if not job.vehicle_id:
                continue
            needs = any(
                s.id in segment_stops and not s.facility_service_start_at
                and not s.travel_arrival_at
                and s.status not in ("completed", "arrived", "en_route")
                for s in job.stop_ids)
            if needs:
                try:
                    projections.update(engine.project_forward_times(job))
                except Exception:
                    _logger.exception(
                        "weekly board projection failed for job %s",
                        job.id)

        rows = []
        for stop_id, job in segment_stops.items():
            stop = job.stop_ids.browse(stop_id)
            times = self._stop_times_for_display(
                stop, projections.get(stop_id))
            qty, qty_kind = self._quantity_text(stop, job)
            run = running_map.get(stop.id, {})
            company = self._company_name(stop)
            rows.append({
                "stop_id": stop.id,
                "job_id": job.id,
                "kind": self._stop_kind(stop),
                "sequence": stop.sequence,
                "stop_type": stop.stop_type,
                "company": company,
                "city": self._city_of(stop, company),
                "status": stop.status,
                "timed": times["timed"],
                "arrival_at": self._iso(times.get("arrival")),
                "service_start_at": self._iso(times.get("service_start")),
                "departure_at": self._iso(times.get("departure")),
                "arrival_local": times.get("arrival_local"),
                "service_start_local": times.get("service_start_local"),
                "departure_local": times.get("departure_local"),
                "waiting_minutes": times.get("waiting_minutes"),
                "eta_source": times.get("eta_source"),
                "projected": times.get("projected", False),
                "add": run.get("add", 0),
                "sub": run.get("sub", 0),
                "qty_display": qty,
                "qty_kind": qty_kind,
                "requires_liftgate": stop.requires_liftgate,
                "appointment_required": stop.appointment_required,
                "hard_deadline": stop.hard_deadline,
                "label": None,
            })
        # P1/P2… / D1/D2… labels per JOB chain: pickup-like stops count
        # pickup positions, delivery-like count delivery positions, in the
        # job's own stop order.
        counters = {}
        for row in sorted(rows,
                          key=lambda r: (r["job_id"], r["sequence"])):
            if row["kind"] == "pickup":
                counters[row["job_id"]] = (
                    counters.get(row["job_id"], {}))
                counters[row["job_id"]]["p"] = \
                    counters[row["job_id"]].get("p", 0) + 1
                row["label"] = "P%d" % counters[row["job_id"]]["p"]
            elif row["kind"] == "delivery":
                counters[row["job_id"]] = counters.get(
                    row["job_id"], {})
                counters[row["job_id"]]["d"] = \
                    counters[row["job_id"]].get("d", 0) + 1
                row["label"] = "D%d" % counters[row["job_id"]]["d"]
        return rows, running_map

    def rolling_capacity_for(self, truck, check_date):
        """TODO 11 API — stop-by-stop rolling capacity for one truck/day.

        Returns: capacity, onboard_start (overnight carryover), rows
        (un-timed cards first in date order, then timed cards by computed
        arrival), peak_onboard, exceed_stops, notes. Each row carries
        onboard_before/onboard_after/underflow/exceed flags.
        """
        if isinstance(check_date, str):
            check_date = _date.fromisoformat(check_date)
        tz_name = self._tz_name()
        capacity = self._day_effective_capacity(truck, check_date) or 0
        start_onboard, notes = self._carryover_pallets(
            truck, check_date, tz_name)
        rows, _ = self._day_stop_rows(truck, check_date, tz_name)
        timed_rows = [r for r in rows if r["timed"]]
        untimed_rows = [r for r in rows if not r["timed"]]

        def _time_sort_key(r):
            return (r["arrival_at"] or r["service_start_at"]
                    or r["departure_at"] or "", r["sequence"])

        def _untimed_sort_key(r):
            stop = self.env["prema.dispatch.stop"].browse(r["stop_id"])
            return (self._iso(stop.scheduled_time) or "", r["sequence"])

        timed_rows.sort(key=_time_sort_key)
        untimed_rows.sort(key=_untimed_sort_key)
        ordered_rows = untimed_rows + timed_rows

        onboard = start_onboard
        exceed_stops = []
        for row in ordered_rows:
            add = row["add"] if row["kind"] == "pickup" else 0
            sub = row["sub"] if row["kind"] == "delivery" else 0
            before = onboard
            underflow = False
            if sub and before < sub:
                underflow = True
                notes.append(
                    "stop %s (job %s) unloads %s with only %s onboard — "
                    "freight source not visible on this truck/day (relay "
                    "without a loading stop?)." % (
                        row["stop_id"], row["job_id"], sub, before))
            onboard = max(0, onboard + add - sub)
            row["onboard_before"] = before
            row["onboard_after"] = onboard
            row["underflow"] = underflow
            row["exceed"] = bool(capacity and onboard > capacity)
            if row["exceed"]:
                exceed_stops.append(row["stop_id"])
        peak = max([start_onboard] + [
            r["onboard_after"] for r in ordered_rows] or [0])
        return {
            "capacity": capacity,
            "onboard_start": start_onboard,
            "peak_onboard": peak,
            "rows": ordered_rows,
            "exceed_stops": exceed_stops,
            "notes": notes,
        }

    def _stop_times_for_display(self, stop, projected=None):
        """Effective arrival/service/departure/wait for a card row.

        Authority order (never fabricated): actuals for executed stops →
        stored engine ETA fields → the read-only projection → un-timed.
        projected only ever comes from EtaEngine.project_forward_times
        (the engine's own walk, read-only)."""
        tz_name = stop.tz_name or FALLBACK_TZ_NAME
        out = {"timed": False, "arrival": None, "service_start": None,
               "departure": None, "waiting_minutes": None,
               "eta_source": None, "projected": False}
        if stop.status == "completed":
            arr = stop.actual_arrival_time
            dep = stop.actual_departure_time or stop.planned_departure_at
            out.update({"timed": bool(arr or dep), "arrival": arr,
                        "service_start": arr, "departure": dep,
                        "eta_source": "actual"})
        elif stop.status == "arrived":
            arr = stop.actual_arrival_time
            ss = stop.facility_service_start_at or arr
            out.update({"timed": bool(arr or ss), "arrival": arr,
                        "service_start": ss,
                        "departure": (stop.actual_departure_time
                                      or stop.planned_departure_at),
                        "eta_source": "actual"})
        elif stop.status == "en_route":
            ss = stop.facility_service_start_at or stop.eta_live
            out.update({"timed": bool(ss), "arrival": stop.travel_arrival_at,
                        "service_start": ss,
                        "departure": stop.planned_departure_at,
                        "waiting_minutes": stop.waiting_minutes or 0.0,
                        "eta_source": stop.eta_source or "live"})
        if not out["timed"]:
            if stop.travel_arrival_at or stop.facility_service_start_at:
                out.update({
                    "timed": True,
                    "arrival": stop.travel_arrival_at,
                    "service_start": stop.facility_service_start_at,
                    "departure": stop.planned_departure_at,
                    "waiting_minutes": stop.waiting_minutes or 0.0,
                    "eta_source": stop.eta_source,
                })
            elif projected:
                out.update({
                    "timed": True,
                    "arrival": projected.get("travel_arrival_at"),
                    "service_start": projected.get(
                        "facility_service_start_at"),
                    "departure": projected.get("planned_departure_at"),
                    "waiting_minutes": projected.get("waiting_minutes", 0.0),
                    "eta_source": projected.get("eta_source", "scheduled"),
                    "projected": True,
                })
        if out["timed"]:
            out["arrival_local"] = self._local_clock(
                tz_name, out["arrival"])
            out["service_start_local"] = self._local_clock(
                tz_name, out["service_start"])
            out["departure_local"] = self._local_clock(
                tz_name, out["departure"])
        return out

    # ── Job card serialization ───────────────────────────────────────

    def _job_card(self, job):
        """One job card dict — the data the board shows per job/day
        (mirrors the planner's unassigned-card fields plus what this
        board consumes: temperature, risk, source document)."""
        job = job.sudo()
        stops = job.stop_ids.filtered(
            lambda s: not s.planning_only and s.status != "cancelled")
        pickups = [s for s in stops if s.stop_type in PICKUP_LIKE]
        deliveries = [s for s in stops if s.stop_type in DELIVERY_LIKE]
        booking = job.logistics_booking_id if hasattr(
            job, "logistics_booking_id") else False
        departure = job.corridor_departure_id if hasattr(
            job, "corridor_departure_id") else False
        # risk_reasons is CONSUMED only (parallel work owns the producer);
        # absent → the board falls back to risk_level colours.
        risk_reasons = []
        if "risk_reasons" in job._fields:
            value = getattr(job, "risk_reasons", False) or []
            if isinstance(value, (list, tuple)):
                risk_reasons = [
                    {"severity": r.get("severity"),
                     "code": r.get("code"),
                     "message": r.get("message") or ""}
                    for r in value if isinstance(r, dict)
                    and (r.get("severity") in ("soft", "hard")
                         or r.get("message"))
                ][:5]
        required_temp = False
        if hasattr(job, "_driver_required_temperature_c"):
            required_temp = job._driver_required_temperature_c(job)
        pallets = job.max_onboard_pallets or job.approximate_skids or 0
        if booking:
            source_document = booking.booking_number or ""
        elif departure:
            source_document = departure.display_name or ""
        elif job.source_document_name:
            source_document = job.source_document_name
        else:
            source_document = ""
        pickup_city = ""
        if pickups:
            company = self._company_name(pickups[0])
            pickup_city = self._city_of(pickups[0], company)
        delivery_city = ""
        if deliveries:
            company = self._company_name(deliveries[-1])
            delivery_city = self._city_of(deliveries[-1], company)
        operation_date = getattr(job, "operation_date", False)
        return {
            "job_id": job.id,
            "name": job.name,
            "vehicle_id": job.vehicle_id.id if job.vehicle_id else False,
            "partner": job.partner_id.name if job.partner_id else "",
            "pickup_city": pickup_city,
            "delivery_city": delivery_city,
            "pallets": pallets,
            "requires_reefer": bool(job.requires_reefer),
            "required_temperature_c": required_temp,
            "liftgate": bool(job.requires_liftgate),
            "priority": job.priority,
            "scheduled_pickup": self._iso(job.scheduled_pickup),
            "operation_date": str(operation_date) if operation_date
            else False,
            "risk_level": job.risk_level or "green",
            "risk_reasons": risk_reasons,
            "corridor_tag": job.corridor_tag or "",
            "service_type": job.service_type or "",
            "source_document_name": source_document,
            "booking_id": booking.id if booking else False,
            "departure_id": departure.id if departure else False,
            "all_stops_completed": bool(job.all_stops_completed),
            "stop_count": len(stops),
            "pickup_count": len(pickups),
            "delivery_count": len(deliveries),
        }

    def _holiday_dates(self):
        """Dates of active holiday calendars (global across calendars) —
        the board shades those days; corridor-specific calendar linkage
        stays with the corridor forms."""
        Cal = self.env["logistics.holiday.calendar"]
        Line = self.env["logistics.holiday.calendar.line"]
        calendars = Cal.sudo().search([("active", "=", True)])
        if not calendars:
            return []
        lines = Line.sudo().search(
            [("calendar_id", "in", calendars.ids)])
        return sorted({str(line.date) for line in lines if line.date})

    # ── Week board payload (TODO 9) ──────────────────────────────────

    def get_week_board(self, week_start=None, truck_id=None):
        """The board's single data RPC payload.

        week_start: local Monday date string (any weekday normalizes to
        its Monday). Returns week_start/today/business_tz/holidays/
        trucks/days — days['YYYY-MM-DD'] = {"by_truck": {truck_id: cell},
        "unassigned": [job cards with no truck on that pickup date]}.
        Each cell = planner header numbers + status + the day's rolling
        timeline (see _truck_day_cell / rolling_capacity_for)."""
        tz_name = self._tz_name()
        week_start = week_start or str(_date.today())
        if isinstance(week_start, str):
            start_date = _date.fromisoformat(week_start)
        else:
            start_date = week_start
        start_date = start_date - timedelta(days=start_date.weekday())
        today_local = self._as_local(tz_name, fields.Datetime.now()).date()
        week_dates = [start_date + timedelta(days=i) for i in range(7)]

        Vehicle = self.env["fleet.vehicle"]
        trucks = Vehicle.search([("active", "=", True)])
        if truck_id:
            try:
                trucks = trucks.filtered(lambda t: t.id == int(truck_id))
            except (TypeError, ValueError):
                pass

        from odoo.addons.prema_dispatch.services.availability_service \
            import DispatchAvailabilityService
        availability = DispatchAvailabilityService(self.env)
        schedule_by_date = {}
        for d in week_dates:
            try:
                schedule_by_date[d] = {
                    s["truck_id"]: s
                    for s in availability.get_truck_day_schedule(d)
                }
            except Exception:
                _logger.exception(
                    "availability schedule failed for %s", d)
                schedule_by_date[d] = {}

        Job = self.env["prema.dispatch.job"]
        days_payload = {}
        for d in week_dates:
            date_str = str(d)
            schedule = schedule_by_date.get(d, {})
            by_truck = {}
            unassigned_cards = []
            # Active corridor departures occupying a truck+date — the SAME
            # domain as the assign guard in dispatch_job_extension: an
            # active scheduled departure OCCUPIES the day even before its
            # jobs materialize (w1 retirement semantics: only cancelled /
            # completed departures release the day).
            departures = self.env["logistics.corridor.departure"].sudo() \
                .search([
                    ("departure_date", "=", d),
                    ("active", "=", True),
                    ("status", "not in", ("cancelled", "completed")),
                ])
            departure_by_truck = {}
            for dep in departures:
                if dep.vehicle_id:
                    departure_by_truck.setdefault(
                        dep.vehicle_id.id, []).append(dep)

            for truck in trucks:
                by_truck[truck.id] = self._truck_day_cell(
                    truck, d, schedule.get(truck.id),
                    departure_by_truck.get(truck.id, []))
            day_start, day_end = self._local_day_utc_range(tz_name, d)
            unassigned = Job.search(self._active_job_domain() + [
                ("vehicle_id", "=", False),
                ("scheduled_pickup", "<=", day_end),
            ]).filtered(
                lambda j: self._job_active_in_range(j, day_start, day_end))
            for j in unassigned:
                unassigned_cards.append(self._job_card(j))
            unassigned_cards.sort(
                key=lambda c: c["scheduled_pickup"] or "")
            days_payload[date_str] = {
                "by_truck": by_truck,
                "unassigned": unassigned_cards,
            }

        # Job metadata map (one card per JOB, looked up by the day rows):
        # booking/source links, temperature, risk, drag meta. Built from
        # the real rows only — a job not on this week's board is absent.
        job_ids = set()
        for payload_day in days_payload.values():
            job_ids.update(c["job_id"] for cell in
                           payload_day["by_truck"].values()
                           for c in cell.get("rolling", {}).get(
                               "rows", []))
            job_ids.update(c["job_id"]
                           for c in payload_day["unassigned"])
        jobs_payload = {}
        if job_ids:
            for j in Job.browse(sorted(job_ids)).sudo():
                jobs_payload[j.id] = self._job_card(j)

        truck_list = []
        for t in trucks:
            driver = t.driver_id or t.x_current_driver_contact_id
            truck_list.append({
                "truck_id": t.id,
                "name": t.name,
                "driver_name": driver.name if driver else "",
                "has_reefer": bool(t.x_reefer),
                "has_liftgate": bool(t.x_liftgate),
                "pallet_capacity":
                    self._day_effective_capacity(t, start_date)
                    or (t.x_max_pallets or 0),
            })
        return {
            "week_start": str(start_date),
            "today": str(today_local),
            "business_tz": tz_name,
            "holidays": self._holiday_dates(),
            "trucks": truck_list,
            "days": days_payload,
            "jobs": jobs_payload,
        }

    def _truck_day_cell(self, truck, check_date, schedule_row=None,
                        departures=None):
        """One truck/day cell: planner header numbers + status + the day's
        rolling timeline (the board's cards come from rolling rows).

        Status semantics (canonical reuse):
          UNAVAILABLE — equipment not operational (the CapacityEngine
                        gate), planner day busy (committed ≥ capacity),
                        or an active scheduled departure occupies
                        truck+day (the extension's truck_day_blocked
                        rule).
          AVAILABLE WITH WARNING — planner day partial.
          AVAILABLE — planner day available with room.
        """
        departures = departures or []
        cap = self._day_effective_capacity(truck, check_date) or 0
        comm = avail = 0
        status = "available"
        note = ""
        planner_status = "available"
        if schedule_row:
            comm = schedule_row.get("committed_pallets") or 0
            avail = schedule_row.get("available_capacity") or 0
            planner_status = schedule_row.get("status") or "available"
        dep = departures[0] if departures else False
        if not truck.x_operational_logistics:
            status = "unavailable"
            note = "Truck not operational for logistics."
        elif dep:
            status = "unavailable"
            note = ("Day belongs to scheduled departure %s — add freight "
                    "to the departure." % dep.display_name)
        elif planner_status == "busy" or (cap and comm >= cap):
            status = "unavailable"
            note = "Day is full (%s/%s positions committed)." % (comm, cap)
        elif planner_status == "partial":
            status = "warning"
            note = "Limited space: %s of %s positions free." % (avail, cap)
        rolling = {}
        try:
            rolling = self.rolling_capacity_for(truck, check_date)
        except Exception:
            _logger.exception("rolling capacity failed for truck %s on %s",
                              truck.id, check_date)
        return {
            "truck_id": truck.id,
            "status": status,
            "status_note": note,
            "capacity": cap,
            "committed_pallets": comm,
            "available_capacity": avail,
            "departure_id": dep.id if dep else False,
            "departure_name": dep.display_name if dep else "",
            "rolling": rolling,
        }

    # ── Evaluate load (TODO 12) ──────────────────────────────────────

    def evaluate_load(self, payload):
        """TODO 12 — per truck/day statuses for a proposed load.

        payload: {date, pallets, weight_lbs, reefer (bool), liftgate
        (bool), pickup (str), delivery (str)}. pickup/delivery are
        advisory labels — pricing/lane resolution is the phone wizard's
        job; the board answers CAPACITY only.

        Every check is canonical:
          • CapacityEngine.evaluate — equipment operational / reefer /
            liftgate / pallet count / payload / layout (pin-wheel needs a
            dispatcher override → soft warning).
          • DispatchAvailabilityService.get_truck_day_schedule — the
            planner's committed numbers (load-plan layout capacity).
          • corridor departure occupation — the assign guard's domain.
          • VehicleCapacityService.evaluate on the departure when the day
            is departure-controlled (remaining sellable positions).

        Returns {"date", "results": [...]} — rows the UI renders as
        AVAILABLE / AVAILABLE WITH WARNING / UNAVAILABLE with free-
        capacity numbers. No pricing, no records, nothing reserved —
        the board's Create booking button opens the canonical phone-
        booking wizard for the real quote/confirm flow."""
        from .capacity_engine import CapacityEngine
        from .vehicle_capacity_service import VehicleCapacityService
        from odoo.addons.prema_dispatch.services.availability_service \
            import DispatchAvailabilityService
        pallets = int(payload.get("pallets") or 0)
        weight = float(payload.get("weight_lbs") or 0.0)
        reefer = bool(payload.get("reefer"))
        liftgate = bool(payload.get("liftgate"))
        check_date = payload.get("date")
        if isinstance(check_date, str):
            check_date = _date.fromisoformat(check_date)
        if pallets <= 0:
            return {"date": str(check_date), "results": [],
                    "error": "Enter a pallet count to evaluate."}
        capacity_engine = CapacityEngine(self.env)
        vcs = VehicleCapacityService(self.env)
        availability = DispatchAvailabilityService(self.env)
        try:
            schedule = {
                s["truck_id"]: s
                for s in availability.get_truck_day_schedule(check_date)
            }
        except Exception:
            _logger.exception("availability failed for %s", check_date)
            schedule = {}
        departures = self.env["logistics.corridor.departure"].sudo().search([
            ("departure_date", "=", check_date),
            ("active", "=", True),
            ("status", "not in", ("cancelled", "completed")),
        ])
        results = []
        Vehicle = self.env["fleet.vehicle"]
        for truck in Vehicle.search([("active", "=", True)]):
            reasons = []
            severity = "ok"
            ce = capacity_engine.evaluate(
                pallets, weight, vehicle=truck,
                requires_reefer=reefer, requires_liftgate=liftgate)
            if not ce.eligible:
                severity = "hard"
                reasons.append({
                    "code": ce.reason_code or "not_eligible",
                    "message": self._reason_message(
                        ce.reason_code, truck),
                })
            elif ce.manual_review:
                severity = "warning"
                reasons.append({
                    "code": ce.reason_code or "manual_layout_review",
                    "message": self._reason_message(
                        ce.reason_code or "manual_layout_review", truck),
                })
            dep = next((d for d in departures if d.vehicle_id
                        and d.vehicle_id.id == truck.id), False)
            row = schedule.get(truck.id)
            comm = row.get("committed_pallets") if row else 0
            cap = self._day_effective_capacity(truck, check_date)
            if dep:
                try:
                    eval_dep = vcs.evaluate(
                        truck, departure=dep, proposed_pallets=pallets)
                except Exception:
                    eval_dep = None
                remaining = ((eval_dep or {}).get(
                    "remaining_sellable_capacity", 0) if eval_dep else 0)
                capacity_valid = ((eval_dep or {}).get(
                    "capacity_valid", False) if eval_dep else False)
                if severity != "hard":
                    if not capacity_valid:
                        severity = "hard"
                        reasons.append({
                            "code": "departure_full",
                            "message": ((eval_dep or {}).get("reason")
                                        or "No sellable capacity left on "
                                        "the departure."),
                        })
                    elif remaining < pallets:
                        severity = "warning"
                        reasons.append({
                            "code": "departure_limited",
                            "message": "Departure has %s free positions "
                                       "left." % remaining,
                        })
                results.append({
                    "truck_id": truck.id,
                    "name": truck.name,
                    "status": severity,
                    "free_capacity": remaining,
                    "capacity": cap,
                    "committed_pallets": comm,
                    "departure_id": dep.id,
                    "departure_name": dep.display_name,
                    "reasons": reasons,
                })
                continue
            free = max(cap - comm, 0)
            if not truck.x_operational_logistics and severity == "ok":
                severity = "hard"
                reasons.append({
                    "code": "equipment_not_operational",
                    "message": "Truck is not operational for logistics.",
                })
            if severity == "ok":
                if free <= 0 or pallets > free:
                    severity = "hard"
                    reasons.append({
                        "code": "day_full",
                        "message": "Only %s of %s positions free on %s."
                                   % (free, cap, check_date),
                    })
                elif free - pallets < max(2, round(cap * 0.25)):
                    severity = "warning"
                    reasons.append({
                        "code": "day_limited",
                        "message": "Adding %s leaves only %s of %s "
                                   "positions free." % (
                                       pallets, max(free - pallets, 0),
                                       cap),
                    })
            results.append({
                "truck_id": truck.id,
                "name": truck.name,
                "status": severity,
                "free_capacity": max(free - pallets, 0),
                "capacity": cap,
                "committed_pallets": comm,
                "departure_id": False,
                "departure_name": "",
                "reasons": reasons,
            })
        results.sort(key=lambda r: (
            r["status"] != "ok", r["status"] != "warning",
            -r["free_capacity"]))
        return {"date": str(check_date), "results": results}

    @staticmethod
    def _reason_message(reason_code, truck):
        messages = {
            "no_vehicle_provided": "No vehicle provided.",
            "equipment_not_operational": "Truck is not operational for "
                                         "logistics.",
            "reefer_required": "Truck is not reefer-equipped.",
            "liftgate_required": "Truck has no liftgate.",
            "invalid_pallet_count": "Invalid pallet count.",
            "pallet_capacity_exceeded": "Exceeds this truck's physical "
                                        "capacity.",
            "payload_exceeded": "Exceeds this truck's payload.",
            "manual_layout_review": "Layout requires dispatcher review.",
            "pinwheel_override_required": "Pin-wheel layout — dispatcher "
                                          "override required.",
        }
        return messages.get(
            reason_code, "Not eligible on %s." % (truck.name or ""))

"""
Truck availability engine.
Answers: which trucks can take a new load on a given date/time?
"""
import logging
import pytz
from datetime import datetime, timedelta, date

_logger = logging.getLogger(__name__)


class DispatchAvailabilityService:
    def __init__(self, env):
        self.env = env
        tz_name = env.context.get("tz") or env.user.tz or "UTC"
        try:
            self._user_tz = pytz.timezone(tz_name)
        except Exception:
            self._user_tz = pytz.utc

    def _to_local_hhmm(self, dt):
        """Convert naive UTC datetime → user local timezone → 'HH:MM' string."""
        if not dt:
            return None
        utc_dt = pytz.utc.localize(dt) if dt.tzinfo is None else dt
        local_dt = utc_dt.astimezone(self._user_tz)
        return local_dt.strftime("%H:%M")

    def _job_active_in_range(self, job, day_start, day_end):
        """True if job's pickup..delivery window overlaps [day_start, day_end]
        (both UTC-naive, matching how Datetime fields are stored). Falls back
        to planned_delivery_date (end of day) when scheduled_delivery isn't
        set, and treats a job with no delivery info as a single-day job on
        its pickup date."""
        pickup = job.scheduled_pickup
        delivery = job.scheduled_delivery
        if not delivery and job.planned_delivery_date:
            delivery = datetime.combine(job.planned_delivery_date, datetime.max.time())
        if not pickup and not delivery:
            return False
        pickup = pickup or delivery
        delivery = delivery or pickup
        if delivery < pickup:
            delivery = pickup
        return pickup <= day_end and delivery >= day_start

    def _stop_belongs_to_day(self, stop, job, check_date):
        """Which calendar day (local tz) a stop should render on in per-day
        views. A stop with its own scheduled_time always wins; otherwise a
        pickup stop defaults to the job's pickup day and every other stop
        type (dropoff/return/transfer/cross-dock) defaults to the job's
        delivery day — so a multi-day job's legs split across the right days
        instead of both appearing on every day the job spans."""
        if stop.scheduled_time:
            reference = stop.scheduled_time
        else:
            reference = (
                job.scheduled_pickup if stop.stop_type == "pickup"
                else (job.scheduled_delivery or job.scheduled_pickup)
            )
        if not reference:
            return True
        return pytz.utc.localize(reference).astimezone(self._user_tz).date() == check_date

    def _local_day_utc_range(self, check_date):
        """Return (day_start_utc, day_end_utc) for a user-local calendar date."""
        local_start = self._user_tz.localize(
            datetime.combine(check_date, datetime.min.time())
        )
        local_end = self._user_tz.localize(
            datetime.combine(check_date, datetime.max.time())
        )
        return (
            local_start.astimezone(pytz.utc).replace(tzinfo=None),
            local_end.astimezone(pytz.utc).replace(tzinfo=None),
        )

    # ── Public API ────────────────────────────────────────────────

    def get_truck_day_schedule(self, check_date, exclude_job_id=None):
        """
        Return every active truck with its schedule for check_date.

        Returns list of dicts (one per truck):
          truck_id, name, driver_name, has_reefer, has_liftgate,
          pallet_capacity, lat, lng, gps_age_minutes,
          status: available|busy|partial,
          jobs: [{job_id, job_name, pickup_time, eta_done, pallets, route_summary}],
          available_from: datetime|None,   # earliest free slot today
          busy_until: datetime|None,
          committed_pallets: int,
          available_capacity: int,
          cross_dock_flex: bool,           # a job already on this truck today
                                            # touches an Allow Cross-Dock location

        exclude_job_id: when checking feasibility for a job that already has
        a vehicle_id (e.g. re-checking an existing assignment), pass its id
        here so it isn't counted as one of the truck's "other" jobs — without
        this, a job's own presence on the truck inflates its own busy_until.
        """
        if isinstance(check_date, str):
            check_date = date.fromisoformat(check_date)

        day_start, day_end = self._local_day_utc_range(check_date)

        vehicles = self.env["fleet.vehicle"].search([("active", "=", True)])
        # A job belongs on this day if its pickup..delivery window overlaps
        # the day at all — not just if scheduled_pickup happens to fall in
        # it. That was the bug behind multi-day jobs (pickup one day,
        # delivery the next) showing only on the pickup day and never on
        # the delivery day, or rendering as if they ran 0-24h same-day.
        candidate_jobs = self.env["prema.dispatch.job"].search([
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
            ("vehicle_id", "!=", False),
            ("scheduled_pickup", "<=", day_end),
        ])
        if exclude_job_id:
            candidate_jobs = candidate_jobs.filtered(lambda j: j.id != exclude_job_id)
        jobs = candidate_jobs.filtered(
            lambda j: self._job_active_in_range(j, day_start, day_end)
        )

        jobs_by_vehicle = {}
        for job in jobs:
            if job.vehicle_id:
                jobs_by_vehicle.setdefault(job.vehicle_id.id, []).append(job)

        # Custody segments (see prema.dispatch.job._job_segments): a job that
        # changes trucks mid-route via a Driver Transfer / Cross-Dock Drop
        # stop needs its stops attributed to whichever truck actually ran
        # them, not lumped under the job's single current vehicle_id — that
        # was making an already-completed pickup appear to belong to
        # whichever truck the job was LAST reassigned to.
        job_segments_map = {j.id: j._job_segments() for j in jobs}
        # A job's OTHER segment(s) — the ones that don't match its current
        # vehicle_id — still belong on that truck's board, purely for
        # display/reporting, never counted toward that truck's capacity
        # (the freight is only physically on one truck at a time). Which
        # side of "now" they're on matters for how they should look:
        # a segment BEFORE the current one already happened ("past" — you
        # handed this off already); a segment AFTER it hasn't happened yet
        # ("future" — staged to receive, not yet in your possession).
        extra_by_vehicle = {}
        for job in jobs:
            segs = job_segments_map[job.id]
            if len(segs) <= 1:
                continue
            current_id = job.vehicle_id.id if job.vehicle_id else False
            current_idx = next(
                (i for i, s in enumerate(segs) if s["vehicle"] and s["vehicle"].id == current_id),
                None,
            )
            for i, seg in enumerate(segs):
                veh = seg["vehicle"]
                if not veh or veh.id == current_id:
                    continue
                leg_kind = "past" if (current_idx is not None and i < current_idx) else "future"
                extra_by_vehicle.setdefault(veh.id, []).append((job, seg, leg_kind))

        now = datetime.utcnow()
        results = []

        for vehicle in vehicles:
            vehicle_driver = vehicle.driver_id or vehicle.x_current_driver_contact_id
            v_jobs = sorted(
                jobs_by_vehicle.get(vehicle.id, []),
                key=lambda j: j.scheduled_pickup or day_start,
            )

            committed = sum(
                j.max_onboard_pallets or j.approximate_skids or 0 for j in v_jobs
            )
            plan = self.env["prema.dispatch.load.plan"].search([
                ("vehicle_id", "=", vehicle.id),
                ("operating_date", "=", check_date),
                ("active", "=", True),
            ], limit=1)
            if plan:
                cap = plan._vehicle_layout_capacity()
                layout_type = plan.layout_template_id.layout_type
            else:
                cap = vehicle.get_layout_capacity() if hasattr(vehicle, "get_layout_capacity") else (vehicle.x_max_pallets or 0)
                layout_type = getattr(vehicle, "default_pallet_layout", "straight")
            avail_cap = max(0, cap - committed)

            # GPS age
            gps_age = None
            if vehicle.x_last_location_at:
                delta = now - vehicle.x_last_location_at.replace(tzinfo=None)
                gps_age = int(delta.total_seconds() / 60)

            # Latest estimated departure across all jobs today
            busy_until = None
            for j in v_jobs:
                last_stop = j.stop_ids.sorted("sequence")[-1:] if j.stop_ids else None
                if last_stop and last_stop.estimated_departure:
                    t = last_stop.estimated_departure
                    if busy_until is None or t > busy_until:
                        busy_until = t
                elif j.scheduled_pickup:
                    # Rough estimate: pickup + 4 hours default if no ETA
                    t = j.scheduled_pickup + timedelta(hours=4)
                    if busy_until is None or t > busy_until:
                        busy_until = t

            available_from = busy_until + timedelta(minutes=15) if busy_until else day_start

            # True if a job already on this truck today stops at a location
            # flagged Allow Cross-Dock — signals the dispatcher/optimizer may
            # legitimately interleave another job's stops through that same
            # location today, so the flat busy_until block above shouldn't
            # be treated as a hard wall by the feasibility checker.
            cross_dock_flex = any(
                stop.saved_location_id.allow_cross_dock
                for j in v_jobs for stop in j.stop_ids
            )

            if not v_jobs:
                status = "available"
            elif avail_cap <= 0:
                status = "busy"
            else:
                status = "partial"

            job_summaries = []
            truck_stop_payloads = []
            for j in v_jobs:
                segs = job_segments_map[j.id]
                my_seg = next(
                    (s for s in segs if s["vehicle"] and s["vehicle"].id == vehicle.id), None
                )
                role_by_stop_id = {st.id: role for st, role in my_seg["stops"]} if my_seg else {}
                seg_stop_ids = set(role_by_stop_id) if my_seg else None

                last = j.stop_ids.sorted("sequence")[-1:] if j.stop_ids else None
                stops_data = []
                for st in j.stop_ids.sorted("sequence"):
                    sd = self.env["prema.dispatch.job"]._driver_stop_dict(st)
                    sd["job_id"] = j.id
                    sd["scheduled_time"] = st.scheduled_time.isoformat() if st.scheduled_time else None
                    role = role_by_stop_id.get(st.id)
                    if role:
                        sd["transfer_role"] = role
                    # Onboard-pallet running totals need every stop on the truck
                    # today regardless of which leg of a multi-day job they
                    # belong to, so this list stays unfiltered.
                    truck_stop_payloads.append(sd)
                    # But the Planner's per-stop marker row (Stop View) should
                    # only show stops that (a) belong to check_date — a pickup
                    # stop with no scheduled_time defaults to the job's pickup
                    # day, everything else defaults to the delivery day — and
                    # (b) belong to THIS truck's custody segment, so a truck
                    # that took over after a Driver Transfer doesn't display
                    # (or get credited for) a pickup another truck already did.
                    in_my_segment = seg_stop_ids is None or st.id in seg_stop_ids
                    if in_my_segment and self._stop_belongs_to_day(st, j, check_date):
                        stops_data.append(sd)

                delivery_dt = j.scheduled_delivery or (
                    datetime.combine(j.planned_delivery_date, datetime.max.time())
                    if j.planned_delivery_date else j.scheduled_pickup
                )
                # Multi-day job (pickup one day, delivery another): flag so the
                # planner draws the block clipped to this day's timeline with
                # a "continues" indicator instead of stretching a same-day bar
                # past midnight using date-less HH:MM math.
                spans_before = bool(j.scheduled_pickup and j.scheduled_pickup < day_start)
                spans_after  = bool(delivery_dt and delivery_dt > day_end)

                job_summaries.append({
                    "job_id":      j.id,
                    "job_name":    j.name,
                    "pickup_time": self._to_local_hhmm(j.scheduled_pickup) if not spans_before else None,
                    "eta_done":    self._to_local_hhmm(
                        last.estimated_departure if last and last.estimated_departure else None
                    ) if not spans_after else None,
                    "spans_before": spans_before,
                    "spans_after":  spans_after,
                    "pallets":     j.max_onboard_pallets or j.approximate_skids or 0,
                    "route":       f"{j.pickup_city} → {j.delivery_cities}" if j.pickup_city else j.name,
                    "stop_count":  j.stop_count,
                    "partner":     j.partner_id.name or "",
                    "stops":       stops_data,
                    "leg_kind":    "primary",
                })

            # Other legs of a split job touching this truck: "past" (already
            # handed off — shown for continuity/reporting) or "future"
            # (staged to receive, not yet in custody) — either way excluded
            # from this truck's capacity math above, which only counts what
            # it's the CURRENT (primary) owner of.
            for job, seg, leg_kind in extra_by_vehicle.get(vehicle.id, []):
                hist_stops = []
                for st, role in seg["stops"]:
                    if not self._stop_belongs_to_day(st, job, check_date):
                        continue
                    sd = self.env["prema.dispatch.job"]._driver_stop_dict(st)
                    sd["job_id"] = job.id
                    sd["scheduled_time"] = st.scheduled_time.isoformat() if st.scheduled_time else None
                    if role:
                        sd["transfer_role"] = role
                    hist_stops.append(sd)
                if not hist_stops:
                    continue
                self.env["prema.dispatch.job"]._apply_truck_onboard_counts(hist_stops)
                job_summaries.append({
                    "job_id":      job.id,
                    "job_name":    job.name,
                    "pickup_time": None,
                    "eta_done":    None,
                    "spans_before": False,
                    "spans_after":  False,
                    "pallets":     0,
                    "route":       f"{job.pickup_city} → {job.delivery_cities}" if job.pickup_city else job.name,
                    "stop_count":  len(hist_stops),
                    "partner":     job.partner_id.name or "",
                    "stops":       hist_stops,
                    "leg_kind":    leg_kind,
                })

            self.env["prema.dispatch.job"]._apply_truck_onboard_counts(truck_stop_payloads)

            results.append({
                "truck_id": vehicle.id,
                "name": vehicle.name,
                "license_plate": vehicle.license_plate or "",
                "driver_id": vehicle_driver.id if vehicle_driver else False,
                "driver_name": (
                    vehicle.driver_id.name
                    or vehicle.x_current_driver_contact_id.name
                    or ""
                ),
                "has_reefer": bool(vehicle.x_reefer),
                "has_liftgate": bool(vehicle.x_liftgate),
                "pallet_capacity": cap,
                "layout_type": layout_type,
                "lat": vehicle.x_last_location_lat or 0,
                "lng": vehicle.x_last_location_lng or 0,
                "gps_age_minutes": gps_age,
                "status": status,
                "jobs": job_summaries,
                "available_from": self._to_local_hhmm(available_from),
                "busy_until": self._to_local_hhmm(busy_until),
                "committed_pallets": committed,
                "available_capacity": avail_cap,
                "cross_dock_flex": cross_dock_flex,
            })

        results.sort(key=lambda x: (x["status"] == "busy", x["name"]))
        return results

    def get_available_trucks(self, check_date, requires_reefer=False,
                             requires_liftgate=False, pallets=0):
        """Filter get_truck_day_schedule by equipment and capacity."""
        all_trucks = self.get_truck_day_schedule(check_date)
        out = []
        for t in all_trucks:
            if requires_reefer and not t["has_reefer"]:
                continue
            if requires_liftgate and not t["has_liftgate"]:
                continue
            if pallets and t["pallet_capacity"] and t["pallet_capacity"] < pallets:
                continue
            if t["status"] != "busy":
                out.append(t)
        return out

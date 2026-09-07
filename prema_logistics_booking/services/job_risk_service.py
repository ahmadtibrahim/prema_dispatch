"""Engine-risk service (TODO 14): deterministic, persisted risk rows.

``JobRiskService.evaluate_and_persist(job, vehicle, at=None,
extra_reasons=None)`` is the single evaluation entry. It reproduces — with
the SAME conditions and message texts — every assignment guard that can
block or warn for a (job, candidate truck) pair, so the persisted
``prema.dispatch.job.risk`` set is exactly the "why" of what the guards
would do:

    code                         severity  mirrors
    ───────────────────────────  ────────  ───────────────────────────────────
    equipment_mismatch           hard      _check_vehicle_compatibility
                                           hard block 1 (requires_reefer and
                                           not vehicle.x_reefer) — carries
                                           temperature_c when the job carries
                                           a canonical supplied setpoint
    liftgate_missing             hard      _check_vehicle_compatibility
                                           hard block 2 (requires_liftgate and
                                           not vehicle.x_liftgate)
    feasibility_blocked          hard      base assign_job_to_truck
                                           not_feasible verdict (can_override
                                           semantics untouched)
    departure_controlled         hard      booking assign override gate: job
                                           bound to a departure whose truck
                                           differs from the candidate
    truck_day_blocked            hard      E1: an ACTIVE scheduled departure
                                           already holds (candidate, day) —
                                           booking assign override search + the
                                           departure-side conflict guards
    window_conflict              hard      E1 restated when the job carries
                                           fixed-window appointment stops that
                                           cannot flex around the corridor day
    departure_conflict           hard      E2: another ACTIVE scheduled LTL
                                           planner operation holds (candidate,
                                           day) — booking assign override
                                           search / _check_ltl_operation_day
    capacity_exceeded            soft      _check_vehicle_compatibility soft
                                           (combined same-day peak vs truck
                                           capacity); also surfaces the
                                           loose-freight pallet-equivalent
                                           floor (item.capacity_equivalent)
                                           that has no guard of its own yet
    payload_exceeded             soft      _check_vehicle_compatibility weight
                                           soft
    appointment_outside_hours    soft      stop appointment anchor (exact /
                                           window close) vs the facility's
                                           frozen operating-hours snapshot
    (hos_violation)              (never)   no HOS/ELD data source on this
                                           build — a dimension with no data
                                           never invents rows
    (temperature_setpoint_mismatch)(never) no per-vehicle setpoint-capability
                                           data exists — only the reefer
                                           boolean, which equipment_mismatch
                                           already covers

Determinism rules (invariants):
* no set() anywhere — ordering is (severity hard first, code, id), and
  state reads are ordered searches (id asc) so the FIRST matching record
  picked for a message is stable;
* a conflict row is only ever generated from a departure that is
  ``status == 'scheduled'`` AND whose corridor is ``active`` — a
  deactivated corridor or a non-scheduled departure never resurfaces as a
  risk reason;
* evaluation REPLACES the job's engine rows atomically (delete all old
  engine rows for the job, insert the new set in one transaction), so the
  persisted set always IS the latest evaluation;
* guard-derived reasons passed via ``extra_reasons`` (e.g. the live
  feasibility verdict of an assign attempt) REPLACE state-derived rows
  with the same code — the guard stays authoritative.

Never runs a live feasibility/Google-Maps check and never mutates anything
but the risk rows of the job itself.
"""
from datetime import datetime as _dt

import pytz

from odoo import fields

# Deterministic ordering: hard rows first, then code, then insertion order
# (which equals the persisted id order).
_SEVERITY_RANK = {"hard": 0, "soft": 1}

_DEFAULT_TZ = "America/Toronto"


class JobRiskService:
    """Evaluate + persist engine-risk rows for (job, candidate truck)."""

    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Public entry ────────────────────────────────────────────────────

    def evaluate_and_persist(self, job, vehicle, at=None, extra_reasons=None):
        """Deterministically (re)evaluate + persist + return the job's rows.

        Returns the board payload form ``[{severity, code, message}, ...]``
        (deterministic order: hard first, then code). Atomic: the job's
        prior engine rows are deleted and the new set inserted in a single
        transaction. ``extra_reasons`` entries ({severity, code, message})
        are merged guard-authoritatively — an extra reason with the same
        code as a state-derived row REPLACES it.
        """
        job = job.with_env(self.env) if job else job
        vehicle = vehicle.with_env(self.env) if vehicle and vehicle._name else vehicle
        if not job or not job.exists():
            return []
        stage = job.stage_id
        if stage and (stage.is_cancelled or stage.is_completed):
            # Nothing to explain for finished jobs — keep the historical rows.
            return self._persisted_payload(job)
        at = at or fields.Datetime.now()

        rows = self._state_rows(job, vehicle)
        extras = list(extra_reasons or [])
        # Guard-derived extras REPLACE every state row with the same code
        # (the guard stays authoritative); state rows with codes the extras
        # do not carry are kept as-is — including several rows sharing one
        # code (e.g. one appointment_outside_hours per affected stop).
        extra_codes = {r["code"] for r in extras}
        final_rows = [r for r in rows if r["code"] not in extra_codes]
        final_rows += extras
        ordered = sorted(
            final_rows,
            key=lambda r: (_SEVERITY_RANK.get(r["severity"], 1),
                           r["code"] or "", r["_seq"]))

        Risk = self.env["prema.dispatch.job.risk"]
        old = Risk.search([
            ("job_id", "=", job.id),
            ("source", "=", "engine"),
        ])
        old.unlink()
        for row in ordered:
            Risk.create(self._row_vals(job, vehicle, at, row))
        return [
            {"severity": r["severity"], "code": r["code"],
             "message": r["message"]}
            for r in ordered
        ]

    # ── State-derived rows (each mirrors one guard) ─────────────────────

    def _state_rows(self, job, vehicle):
        rows = []
        seq = [0]

        def add(code, severity, message, **extra):
            seq[0] += 1
            row = {"code": code, "severity": severity, "message": message,
                   "_seq": seq[0]}
            row.update(extra)
            rows.append(row)

        has_vehicle = bool(vehicle and vehicle.exists())

        # ── equipment_mismatch / liftgate_missing — mirror compat hard 1/2
        if has_vehicle:
            vehicle_reefer = bool(getattr(vehicle, "x_reefer", False))
            vehicle_liftgate = bool(getattr(vehicle, "x_liftgate", False))
            if job.requires_reefer and not vehicle_reefer:
                row_extra = {}
                if getattr(job, "temperature_supplied", False):
                    row_extra["temperature_c"] = job.temperature_instruction_c
                add("equipment_mismatch", "hard",
                    "Reefer required — this truck is not refrigerated.",
                    equipment="reefer", **row_extra)
            if job.requires_liftgate and not vehicle_liftgate:
                add("liftgate_missing", "hard",
                    "Liftgate required — this truck has no liftgate.",
                    equipment="liftgate")

        # ── feasibility_blocked — stored verdict (base assign guard). The
        # live verdict of an assign attempt arrives via extra_reasons and
        # replaces this state-derived row (same code).
        if (has_vehicle and job.vehicle_id and job.vehicle_id.id == vehicle.id
                and job.feasibility_status == "not_feasible"):
            add("feasibility_blocked", "hard",
                job.feasibility_notes
                or "This truck can't feasibly complete this job.")

        # ── Day-conflict dimensions (E1/E2/E3) — mirror the booking assign
        # override + the departure-side conflict guards. Only ever cite a
        # departure that is scheduled on an ACTIVE corridor: deactivated
        # corridors / non-scheduled departures never resurface here.
        if has_vehicle and job.operation_date:
            self._day_conflict_rows(job, vehicle, add)

        # ── capacity / payload — mirror compat softs. Needs a truck.
        if has_vehicle:
            self._capacity_rows(job, vehicle, add)
            if (job.total_weight_lbs
                    and getattr(vehicle, "x_max_payload_lbs", False)
                    and job.total_weight_lbs > vehicle.x_max_payload_lbs):
                add("payload_exceeded", "soft",
                    "Load weight (%s lbs) exceeds truck payload (%s lbs)."
                    % ("%.0f" % job.total_weight_lbs,
                       "%.0f" % vehicle.x_max_payload_lbs))

        # ── appointment_outside_hours — per operational stop.
        self._hours_rows(job, add)

        # (No HOS dimension: no ELD/driver-log data source exists on this
        # build, so hos_violation is intentionally never generated.)
        return rows

    def _live_departures_on(self, vehicle, day, exclude_departure=None):
        """ACTIVE scheduled departures holding (vehicle, day) — the only
        departures this service may cite (deactivated corridors and
        non-scheduled departures never count)."""
        if "logistics.corridor.departure" not in self.env.registry.models:
            return self.env["logistics.corridor.departure"]
        domain = [
            ("vehicle_id", "=", vehicle.id),
            ("departure_date", "=", day),
            ("active", "=", True),
            ("status", "=", "scheduled"),
            ("corridor_id.active", "=", True),
        ]
        if exclude_departure:
            domain.append(("id", "!=", exclude_departure.id))
        return self.env["logistics.corridor.departure"].sudo().search(
            domain, order="id asc")

    def _job_has_fixed_window_stops(self, job):
        """Appointment stops whose times pin the day (window/exact/deadline),
        i.e. stops the corridor hold cannot absorb by re-timing."""
        for stop in job.stop_ids:
            if stop.planning_only or stop.status in ("cancelled", "completed"):
                continue
            if stop.appointment_required:
                return True
            if (stop.time_window_type in ("window", "exact", "deadline")
                    and (stop.exact_time or stop.earliest_time
                         or stop.latest_time or stop.deadline_time)):
                return True
        return False

    def _day_conflict_rows(self, job, vehicle, add):
        day = job.operation_date
        own_departure = job.corridor_departure_id or self.env[
            "logistics.corridor.departure"]
        own_dep_live = bool(
            own_departure
            and own_departure.corridor_id.active
            and own_departure.status == "scheduled")

        # ── E3 departure_controlled — job bound to a departure whose own
        # truck differs from the candidate (mirror of the assign gate).
        if own_departure and vehicle.id != own_departure.vehicle_id.id:
            if own_dep_live:
                add("departure_controlled", "hard",
                    "This LTL load belongs to %s. Reassign the Truck from "
                    "Open: Departure." % own_departure.display_name)

        # ── E1 another live departure holds (candidate, day) — mirror of
        # the booking assign override search / _check_ltl_operation_day.
        # Never cites the job's own departure.
        conflicts = self._live_departures_on(
            vehicle, day,
            exclude_departure=own_departure if own_departure else None)
        if conflicts:
            conflict = conflicts[0]
            message = (
                "This truck is booked for %s on %s. Add freight to that LTL "
                "departure or choose another truck."
                % (conflict.corridor_id.display_name, day))
            if self._job_has_fixed_window_stops(job):
                add("window_conflict", "hard", message + " This job carries "
                    "fixed-window appointment stops that cannot flex around "
                    "that corridor departure.")
            else:
                add("truck_day_blocked", "hard", message)

        # ── E2 another live LTL planner operation holds (candidate, day) —
        # mirror of the assign override search / _check_ltl_operation_day.
        # Excludes self and operations serving the job's own departure.
        operation_domain = [
            ("id", "!=", job.id),
            ("vehicle_id", "=", vehicle.id),
            ("operation_date", "=", day),
            ("auto_scheduled_ltl", "=", True),
            ("stage_id.stage_type", "not in", ("cancelled", "completed")),
        ]
        operations = self.env["prema.dispatch.job"].sudo().search(
            operation_domain, order="id asc")
        for operation in operations:
            if (own_departure and operation.corridor_departure_id
                    and operation.corridor_departure_id.id
                    == own_departure.id):
                continue
            add("departure_conflict", "hard",
                "This truck is reserved for LTL operation %s on %s. Choose "
                "another truck." % (operation.display_name, day))
            break  # one row: the guard reports the first reservation

    # ── Capacity/payload — mirrors _check_vehicle_compatibility ─────────

    def _same_day_truck_jobs(self, job, vehicle):
        """Other same-pickup-day active jobs on the truck (guard mirror)."""
        pickup = job.scheduled_pickup
        if not pickup:
            return self.env["prema.dispatch.job"]
        day_start = _dt.combine(pickup.date(), _dt.min.time())
        day_end = _dt.combine(pickup.date(), _dt.max.time())
        return self.env["prema.dispatch.job"].search([
            ("vehicle_id", "=", vehicle.id),
            ("id", "!=", job.id),
            ("stage_id.is_cancelled", "=", False),
            ("stage_id.is_completed", "=", False),
            ("scheduled_pickup", ">=", day_start),
            ("scheduled_pickup", "<=", day_end),
        ])

    @staticmethod
    def _job_floor_equivalents(job):
        """Sum of the job's loose-freight pallet-equivalent floor
        (prema.dispatch.item.capacity_equivalent > 0 = pallet-equivalent
        floor space; 0 = unset / regular pallet)."""
        if not job.item_ids:
            return 0.0
        return sum(
            it.capacity_equivalent
            for it in job.item_ids
            if it.capacity_equivalent and it.capacity_equivalent > 0)

    def _capacity_rows(self, job, vehicle, add):
        # Pallet requirement — mirror guard label/fallback logic.
        if job.max_onboard_pallets > 0:
            check_pallets = job.max_onboard_pallets
            pallet_label = "Max onboard (%s pallets peak)" % check_pallets
        else:
            check_pallets = job.approximate_skids or 0
            pallet_label = "Estimated skids (%s)" % check_pallets

        # Capacity resolution — mirror guard chain.
        cap = 0
        if hasattr(vehicle, "get_layout_capacity"):
            if job.scheduled_pickup:
                plan = self.env["prema.dispatch.load.plan"].search([
                    ("vehicle_id", "=", vehicle.id),
                    ("operating_date", "=", fields.Date.to_date(
                        job.scheduled_pickup)),
                    ("active", "=", True),
                ], limit=1)
                if plan:
                    cap = plan._vehicle_layout_capacity()
            cap = cap or vehicle.get_layout_capacity()
        cap = cap or getattr(vehicle, "x_max_pallets", False) or 0
        if not cap:
            return

        other_jobs = self._same_day_truck_jobs(job, vehicle)
        other_jobs_pallets = sum(
            j.max_onboard_pallets or j.approximate_skids or 0
            for j in other_jobs)

        # Loose-freight refinement: the guards only see pallets; the item
        # floor space (capacity_equivalent) also occupies the truck. When
        # no equivalents exist anywhere the numbers below are exactly the
        # guard's, so the warning row and the guard never disagree.
        own_equiv = self._job_floor_equivalents(job)
        others_equiv = sum(self._job_floor_equivalents(j) for j in other_jobs)
        combined_guard = check_pallets + other_jobs_pallets
        combined_floor = (
            max(check_pallets, own_equiv) if own_equiv else check_pallets
        ) + (other_jobs_pallets + others_equiv if others_equiv
             else other_jobs_pallets)

        if combined_floor <= cap:
            return
        if not own_equiv and not others_equiv:
            # Byte-identical to the guard's soft warning.
            if other_jobs_pallets and check_pallets <= cap:
                message = (
                    "%s (%sp) combined with other truck jobs (%sp) gives "
                    "%sp estimated peak — truck capacity is %sp. If "
                    "deliveries happen before next pickup, actual peak may "
                    "be lower."
                    % (pallet_label, check_pallets, other_jobs_pallets,
                       combined_guard, cap))
            else:
                message = (
                    "%s exceed truck capacity (%s pallets)."
                    % (pallet_label, cap))
            add("capacity_exceeded", "soft", message,
                capacity_used_equiv=combined_guard,
                capacity_available_equiv=cap)
            return
        # Equivalents are involved (no guard message to mirror) — state the
        # footprint math explicitly. The job's own footprint is the max of
        # its pallet peak and its loose-freight floor (both occupy the same
        # truck), never their sum.
        if check_pallets:
            own_desc = "%s (%sp)" % (pallet_label, check_pallets)
        else:
            own_desc = "loose-freight pallet-equivalents"
        if own_equiv:
            own_desc += " plus loose-freight floor %.0fp" % own_equiv
        if other_jobs_pallets or others_equiv:
            others_footprint = other_jobs_pallets + others_equiv
            message = (
                "%s gives a running peak of ~%.0fp including other same-day "
                "truck jobs (%.0fp%s) — truck capacity is %sp. If deliveries "
                "happen before next pickup, actual peak may be lower."
                % (own_desc, combined_floor, others_footprint,
                   " + %.0fp loose freight" % others_equiv if others_equiv
                   else "", cap))
        else:
            message = (
                "%s gives a running peak of ~%.0fp — truck capacity is %sp."
                % (own_desc, combined_floor, cap))
        add("capacity_exceeded", "soft", message,
            capacity_used_equiv=combined_floor,
            capacity_available_equiv=cap)

    # ── Appointment vs facility hours ───────────────────────────────────

    def _hours_rows(self, job, add):
        tz = None
        for stop in job.stop_ids:
            if stop.planning_only or stop.status in ("cancelled", "completed"):
                continue
            anchor = (stop.exact_time or stop.latest_time
                      or stop.deadline_time or False)
            if not anchor:
                continue  # no fixed arrival commitment — drivers can wait
            if tz is None:
                tz = pytz.timezone(getattr(stop, "tz_name", False)
                                   or _DEFAULT_TZ)
            local = pytz.utc.localize(anchor).astimezone(tz)
            snapshot = stop.operating_hours_snapshot or {}
            day_hours = snapshot.get(str(local.weekday()))
            hour_f = local.hour + local.minute / 60.0
            if day_hours:
                open_f, close_f = day_hours[0], day_hours[1]
                if hour_f > close_f or hour_f < open_f:
                    add(
                        "appointment_outside_hours", "soft",
                        "Stop %s has an appointment at %s (%s local) — "
                        "outside the facility's operating hours %s."
                        % (stop.display_name,
                           local.strftime("%H:%M"),
                           local.strftime("%A"),
                           "%s-%s" % ("%.0f" % open_f, "%.0f" % close_f)),
                        stop_id=stop.id,
                        hours_summary="%s %s-%s" % (
                            local.strftime("%a"),
                            "%.0f" % open_f, "%.0f" % close_f))
            else:
                add(
                    "appointment_outside_hours", "soft",
                    "Stop %s has an appointment at %s (%s local) but the "
                    "facility is closed that day (no operating hours on "
                    "record)." % (stop.display_name,
                                  local.strftime("%H:%M"),
                                  local.strftime("%A")),
                    stop_id=stop.id,
                    hours_summary="%s closed" % local.strftime("%a"))

    # ── Helpers ─────────────────────────────────────────────────────────

    def _persisted_payload(self, job):
        Risk = self.env["prema.dispatch.job.risk"]
        rows = Risk.search([
            ("job_id", "=", job.id),
            ("source", "=", "engine"),
        ])
        return [
            {"severity": r.severity, "code": r.code, "message": r.message}
            for r in sorted(
                rows, key=lambda r: (_SEVERITY_RANK.get(r.severity, 1),
                                     r.code or "", r.id))
        ]

    def _row_vals(self, job, vehicle, at, row):
        """Map an ordered row dict to risk-model create values. Float
        display values stay PRESENT when legitimately zero (a 0°C reefer
        setpoint) — only absent keys map to False."""
        vals = {
            "job_id": job.id,
            "vehicle_id": vehicle.id if vehicle and vehicle.exists() else False,
            "severity": row["severity"],
            "code": row["code"],
            "message": row["message"],
            "event_at": at,
            "source": "engine",
            "capacity_used_equiv": (
                row["capacity_used_equiv"] if "capacity_used_equiv" in row
                else 0.0),
            "capacity_available_equiv": (
                row["capacity_available_equiv"]
                if "capacity_available_equiv" in row else 0.0),
            "equipment": row["equipment"] if "equipment" in row else False,
            "temperature_c": (
                row["temperature_c"] if "temperature_c" in row else False),
            "hours_summary": (
                row["hours_summary"] if "hours_summary" in row else False),
            "hos_summary": False,
        }
        if "stop_id" in row and row["stop_id"]:
            vals["stop_id"] = row["stop_id"]
        return vals

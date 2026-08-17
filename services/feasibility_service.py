"""
Feasibility checker — "Can we do this today?"

Answers customer phone call questions in real time:
  "Can you pick up between 9–11 AM and deliver by 3:30 PM?"
"""
import logging
import pytz
from datetime import datetime, timedelta, date

_logger = logging.getLogger(__name__)


class DispatchFeasibilityService:
    def __init__(self, env):
        self.env = env
        self._user_tz = pytz.timezone(env.user.tz or "America/Toronto")

    def _fmt12(self, dt):
        """Naive UTC -> 12-hour AM/PM in the dispatcher's own timezone."""
        return pytz.utc.localize(dt).astimezone(self._user_tz).strftime("%I:%M %p").lstrip("0")

    # ── Public API ────────────────────────────────────────────────

    def check(self, payload):
        """
        Evaluate all active trucks for a proposed new pickup+delivery.

        payload keys (all optional except pickup_address + dropoff_address):
            pickup_address      str
            dropoff_address     str  (or list for multi-stop)
            pickup_earliest     "HH:MM" or datetime  (None = flexible)
            pickup_latest       "HH:MM" or datetime  (None = flexible)
            delivery_deadline   "HH:MM" or datetime  (None = flexible)
            pallets             int
            weight_lbs          float
            requires_reefer     bool
            requires_liftgate   bool
            service_time_pickup_min    int (default 20)
            service_time_delivery_min  int (default 15)
            check_date          "YYYY-MM-DD" (default today)
            exclude_job_id      int (don't count this job among a truck's own
                                 already-assigned jobs — pass the job's own id
                                 when re-checking an existing assignment)
            job_has_cross_dock_stop  bool (this job touches a Saved Location
                                 with Allow Cross-Dock — see below)

        Returns dict:
            verdict:  feasible | risky | not_feasible
            reason:   str
            options:  list of truck option dicts (best first)
            best:     best truck option or None
        """
        from odoo.addons.prema_dispatch.services.availability_service import DispatchAvailabilityService
        from odoo.addons.prema_dispatch.services.route_service import DispatchRouteService

        pickup_addr = payload.get("pickup_address", "")
        dropoff_addr = payload.get("dropoff_address", "")
        if not pickup_addr or not dropoff_addr:
            return {"verdict": "not_feasible", "reason": "Pickup and delivery addresses are required.", "options": [], "best": None}

        check_date = payload.get("check_date")
        if isinstance(check_date, str):
            check_date = date.fromisoformat(check_date)
        elif not check_date:
            check_date = datetime.now(pytz.utc).astimezone(self._user_tz).date()

        pallets = int(payload.get("pallets") or 0)
        requires_reefer = bool(payload.get("requires_reefer"))
        requires_liftgate = bool(payload.get("requires_liftgate"))
        svc_pickup = int(payload.get("service_time_pickup_min") or 20)
        svc_delivery = int(payload.get("service_time_delivery_min") or 15)

        pickup_earliest = self._parse_time(payload.get("pickup_earliest"), check_date)
        pickup_latest = self._parse_time(payload.get("pickup_latest"), check_date)
        delivery_deadline = self._parse_time(payload.get("delivery_deadline"), check_date)

        now = datetime.utcnow()
        # The "no earlier than now" clamp below (eta_at_pickup =
        # max(eta_at_pickup, now)) only makes sense when the check is for
        # TODAY. For a future or past check_date the real wall clock would
        # clamp the ETA to the wrong instant — a re-check on an old date
        # (e.g. a July 6 route re-validated in August) would get every ETA
        # shifted to the actual current date and falsely report "misses
        # pickup window by ~40,000 min". Anchor now to the start of
        # check_date so the clamp is a no-op for non-today checks.
        real_today = datetime.now(pytz.utc).date()
        if check_date != real_today:
            now = datetime.combine(check_date, datetime.min.time())

        avail_svc = DispatchAvailabilityService(self.env)
        route_svc = DispatchRouteService(self.env)

        exclude_job_id = payload.get("exclude_job_id")
        job_has_cross_dock_stop = bool(payload.get("job_has_cross_dock_stop"))
        all_trucks = avail_svc.get_truck_day_schedule(check_date, exclude_job_id=exclude_job_id)
        options = []

        for truck in all_trucks:
            # Equipment filter
            if requires_reefer and not truck["has_reefer"]:
                continue
            if requires_liftgate and not truck["has_liftgate"]:
                continue
            # Capacity filter
            if pallets and truck["pallet_capacity"] and truck["pallet_capacity"] < pallets:
                continue

            # Determine truck's current/available position
            truck_loc = None
            if truck["lat"] and truck["lng"]:
                truck_loc = (truck["lat"], truck["lng"])

            # Compute drive time: truck → pickup
            origin = truck_loc or pickup_addr  # fallback: assume at pickup
            legs = route_svc.get_sequential_travel([origin, pickup_addr, dropoff_addr])
            drive_to_pickup = legs[0]["drive_minutes"] if legs else 0
            pickup_to_delivery = legs[1]["drive_minutes"] if len(legs) > 1 else 0
            dist_to_pickup_km = legs[0]["distance_km"] if legs else 0
            dist_pickup_delivery_km = legs[1]["distance_km"] if len(legs) > 1 else 0

            # Truck available from time — availability_service reports this
            # as a bare "HH:MM" local-time string, not a full ISO datetime,
            # so it needs the same HH:MM+check_date combine _parse_time
            # already does for the wizard's own time fields.
            #
            # Exception: if this job or another job already on the truck
            # today touches an Allow Cross-Dock location, don't treat the
            # truck as blocked until its other job's estimated finish time —
            # a shared cross-dock stop means the dispatcher/optimizer can
            # legitimately interleave both jobs' stops through it today, so
            # rejecting on "truck busy with the other job until X" would be
            # a false feasibility error, not a real one. Equipment/capacity/
            # deadline checks below still apply in full.
            interleave_via_cross_dock = bool(
                truck["jobs"] and (job_has_cross_dock_stop or truck.get("cross_dock_flex"))
            )
            avail_raw = None if interleave_via_cross_dock else truck.get("available_from")
            avail_from = self._parse_time(avail_raw, check_date) or now

            # ETA at pickup = max(available_from + drive_to_pickup, pickup_earliest, now)
            eta_at_pickup = avail_from + timedelta(minutes=drive_to_pickup)
            if pickup_earliest:
                eta_at_pickup = max(eta_at_pickup, pickup_earliest)
            eta_at_pickup = max(eta_at_pickup, now)

            # Check pickup window
            pickup_window_ok = True
            pickup_miss_reason = ""
            if pickup_latest and eta_at_pickup > pickup_latest:
                pickup_window_ok = False
                over_min = int((eta_at_pickup - pickup_latest).total_seconds() / 60)
                pickup_miss_reason = f"Pickup ETA {self._fmt12(eta_at_pickup)} misses pickup window by {over_min} min."

            # ETA at delivery
            eta_at_delivery = eta_at_pickup + timedelta(minutes=svc_pickup + pickup_to_delivery)

            # Check delivery deadline
            deadline_ok = True
            buffer_minutes = None
            deadline_miss_reason = ""
            if delivery_deadline:
                buffer_minutes = int((delivery_deadline - eta_at_delivery).total_seconds() / 60)
                if buffer_minutes < 0:
                    deadline_ok = False
                    deadline_miss_reason = f"Delivery ETA {self._fmt12(eta_at_delivery)} misses deadline by {-buffer_minutes} min."

            # Determine option verdict
            if not pickup_window_ok:
                option_verdict = "not_feasible"
                reason = pickup_miss_reason
            elif not deadline_ok:
                option_verdict = "not_feasible"
                reason = deadline_miss_reason
            elif buffer_minutes is not None and buffer_minutes < 30:
                option_verdict = "risky"
                reason = f"Only {buffer_minutes} min buffer before deadline."
            else:
                option_verdict = "feasible"
                reason = ""

            if interleave_via_cross_dock and option_verdict == "feasible":
                reason = "Interleaves with another job on this truck via a shared cross-dock stop."

            options.append({
                "truck_id": truck["truck_id"],
                "truck_name": truck["name"],
                "driver_name": truck["driver_name"],
                "verdict": option_verdict,
                "reason": reason,
                "pickup_eta": self._fmt12(eta_at_pickup),
                "delivery_eta": self._fmt12(eta_at_delivery),
                "deadline_buffer_minutes": buffer_minutes,
                "distance_to_pickup_km": dist_to_pickup_km,
                "distance_pickup_delivery_km": dist_pickup_delivery_km,
                "extra_distance_km": dist_to_pickup_km + dist_pickup_delivery_km,
                "has_reefer": truck["has_reefer"],
                "has_liftgate": truck["has_liftgate"],
                "available_capacity": truck["available_capacity"],
                "existing_jobs": len(truck["jobs"]),
                "gps_age_minutes": truck.get("gps_age_minutes"),
            })

        # Sort: feasible first, then risky, then not_feasible; within each group by distance
        def _sort_key(o):
            order = {"feasible": 0, "risky": 1, "not_feasible": 2}
            return (order.get(o["verdict"], 3), o["distance_to_pickup_km"])

        options.sort(key=_sort_key)

        feasible_options = [o for o in options if o["verdict"] == "feasible"]
        risky_options = [o for o in options if o["verdict"] == "risky"]

        if feasible_options:
            verdict = "feasible"
            best = feasible_options[0]
            reason = ""
        elif risky_options:
            verdict = "risky"
            best = risky_options[0]
            reason = best["reason"]
        elif options:
            verdict = "not_feasible"
            best = None
            reasons = list({o["reason"] for o in options if o["reason"]})
            reason = reasons[0] if reasons else "No truck meets the requirements."
        else:
            verdict = "not_feasible"
            best = None
            reason = "No trucks qualify (equipment / capacity mismatch)."

        return {
            "verdict": verdict,
            "reason": reason,
            "best": best,
            "options": options[:5],  # top 5
        }

    # ── Helpers ──────────────────────────────────────────────────

    def _parse_time(self, val, check_date):
        """Parse 'HH:MM' or datetime string into a datetime on check_date."""
        if not val:
            return None
        if isinstance(val, datetime):
            return val
        val = str(val).strip()
        if "T" in val or " " in val:
            try:
                return datetime.fromisoformat(val)
            except Exception:
                pass
        if ":" in val:
            try:
                h, m = val.split(":")[:2]
                return datetime.combine(check_date, datetime.min.time()).replace(
                    hour=int(h), minute=int(m)
                )
            except Exception:
                pass
        return None

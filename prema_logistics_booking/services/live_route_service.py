# ════════════════════════════════════════════════════════════════════
# Phase 11 — Dynamic Actual Route + Live ETA.
#   CORRIDOR = framework (service authority); CONFIRMED STOPS = the actual
#   route (RULE 4). The actual route is built from confirmed dispatch
#   stops only — corridor endpoints nobody confirmed are never forced onto
#   the truck (corridor extent trim). Locked stops (completed / arrived /
#   en_route / route_locked) never move during live reoptimization; only
#   unlocked pending stops are reordered.
#
#   Start position hierarchy: manual/GPS override → prior-day end (last
#   completed stop of this truck's previous day) → hub → depot. A pickup
#   scheduled early the NEXT day is a recommendation (pre-positioning
#   note), never forced travel.
#
#   Live ETA: anchor = actual start (or now), propagate stop-by-stop with
#   drive + service minutes; delay = ETA − scheduled time, carried
#   forward. Dispatcher ETA override wins for its stop.
# ════════════════════════════════════════════════════════════════════
import logging
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

# Customer-visible milestone vocabulary (Phase 11.4) — the ONLY labels a
# customer ever sees. Derivation lives here so every consumer (portal,
# tracking, driver app) reads the same authority.
CUSTOMER_VISIBLE = (
    "estimated_arrival",   # pending, ETA known from schedule/live
    "updated_eta",         # ETA differs from the scheduled estimate
    "in_transit",          # en route between stops / linehaul
    "out_for_delivery",    # arrived the delivery region / final stop
    "delivered",           # delivered — POD/evidence captured
)


class LiveRouteService:
    def __init__(self, env):
        self.env = env

    # ── Start position (11.1.c) ──────────────────────────────────────

    def start_position(self, job):
        """(lat, lng, source) for the actual route — manual/GPS override
        → prior-day end → hub → depot. Never forces corridor-endpoint
        travel; this is only where the truck IS."""
        job.ensure_one()
        if job.start_position_override == "gps" and (
                job.start_position_override_lat and
                job.start_position_override_lng):
            return (job.start_position_override_lat,
                    job.start_position_override_lng, "manual")
        vehicle = job.vehicle_id
        if job.start_position_override == "prior_day_end" or \
                not job.start_position_override or \
                job.start_position_override == "auto":
            prior = self._prior_day_end(job)
            if prior:
                return (prior.latitude, prior.longitude, "prior_day_end")
        if vehicle:
            if vehicle.x_home_base_lat and vehicle.x_home_base_lng:
                return (vehicle.x_home_base_lat, vehicle.x_home_base_lng,
                        "depot")
            hub = job.hub_id if "hub_id" in job._fields else False
            if hub:
                lat = hub.latitude if "latitude" in hub._fields else False
                lng = hub.longitude if "longitude" in hub._fields else False
                if lat and lng:
                    return (lat, lng, "hub")
        return (43.648621, -79.659983, "depot")

    def _prior_day_end(self, job):
        """Last completed stop of this truck's previous day — cross-day
        positioning (11.1.d)."""
        if not job.vehicle_id or not job.scheduled_pickup:
            return False
        Stop = self.env["prema.dispatch.stop"]
        stops = Stop.search([
            ("job_id.vehicle_id", "=", job.vehicle_id.id),
            ("job_id.scheduled_pickup", "<", job.scheduled_pickup),
            ("status", "=", "completed"),
        ], order="actual_departure_time desc", limit=1)
        return stops[:1]

    # ── Actual route (11.1.a/b) ──────────────────────────────────────

    def build_actual_route(self, job):
        """Order the job's confirmed stops into the ACTUAL route.

        Rules:
          • locked stops (completed / arrived / en_route / route_locked)
            keep their relative order — never reordered;
          • unlocked pending stops are sorted by scheduled_time, with
            pallet precedence: a delivery and a pickup at the same window
            order delivery-first (unloading reduces onboard load);
          • corridor-extent trim: nothing is added beyond confirmed stops;
          • early next-day pickups are returned as a recommendation list,
            never inserted as forced travel.

        Writes stop.sequence (renumbered 10/20/… to reflect the ACTUAL
        order — the actual route is a fresh sequencing, not the planned
        one) and returns the ordered recordset.
        """
        job.ensure_one()
        stops = job.stop_ids.filtered(lambda s: not s.planning_only)
        locked = stops.filtered(
            lambda s: s.status in ("completed", "arrived", "en_route")
            or s.route_locked).sorted("sequence")
        free = stops - locked
        # Pallet precedence: deliveries before pickups within the same
        # scheduled window.
        def _order_key(stop):
            def _pallet_rank(stop):
                return 0 if stop.stop_type == "delivery" else 1
            return (stop.scheduled_time or stop.earliest_time
                    or fields.Datetime.now(), _pallet_rank(stop),
                    stop.id)
        free = free.sorted(key=_order_key)
        ordered = locked + free
        seq = 10
        for stop in ordered:
            stop.write({"sequence": seq})
            seq += 10

        # Early next-day pickup = recommendation only (never forced).
        recs = [s for s in free
                if s.stop_type == "pickup" and s.earliest_time
                and s.earliest_time.date() > job.scheduled_pickup.date()]
        if recs:
            job.write({
                "route_recommendation_log":
                self._log_recommendation(job, recs),
            })
        return ordered

    def _log_recommendation(self, job, stops):
        """Append a dated recommendation entry (audit trail, 11.4)."""
        entry = {
            "at": fields.Datetime.now().isoformat(),
            "kind": "early_next_day_pickup",
            "stops": [{"id": s.id, "address": s.address,
                       "earliest": s.earliest_time.isoformat()
                       if s.earliest_time else False}
                      for s in stops],
            "note": "Recommendation only — dispatcher decides.",
        }
        log = list(job.route_recommendation_log or [])
        log.append(entry)
        return log

    # ── Live ETA (11.1.e) ────────────────────────────────────────────

    def recompute_live_eta(self, job, anchor_time=None):
        """Propagate delays stop-by-stop. Completed/arrived stops use
        actual times (delay measured against scheduled); en_route stops
        hold their ETA and pass the delay forward; dispatcher ETA
        overrides win for their stop. Drives the customer-visible
        milestone derivation."""
        job.ensure_one()
        ordered = job.stop_ids.filtered(
            lambda s: not s.planning_only).sorted("sequence")
        anchor = anchor_time or fields.Datetime.now()
        for stop in ordered:
            if stop.eta_override:
                # Dispatcher override wins WHOLE — no drive/service drift.
                eta = stop.eta_override
                delay = (eta - (stop.scheduled_time or eta)).total_seconds() / 60.0
                source = "override"
            elif stop.status in ("completed", "arrived"):
                actual = stop.actual_arrival_time or stop.scheduled_time
                eta = actual
                delay = (actual - (stop.scheduled_time or actual)).total_seconds() / 60.0
                source = "scheduled"
            elif stop.status == "en_route":
                # en_route stops HOLD their ETA — the delay they carry
                # passes forward unchanged (11.1.e).
                eta = stop.eta_live or anchor
                delay = (eta - (stop.scheduled_time or eta)).total_seconds() / 60.0
                source = "live"
            else:
                # Pending: advance from the previous anchor by drive +
                # service minutes.
                eta = anchor
                drive = stop.drive_time_from_prev_minutes or 0
                service = stop.service_time_minutes or 15
                eta = eta + timedelta(minutes=drive + service)
                delay = (eta - (stop.scheduled_time or eta)).total_seconds() / 60.0
                source = "live"
            stop.write({
                "eta_live": eta,
                "eta_delay_minutes": round(delay, 1),
                "eta_source": source,
            })
            anchor = eta
        return ordered

    # ── Customer-visible milestone (11.4) ────────────────────────────

    def customer_visible_status(self, job):
        """ONE authoritative derivation of what the customer sees —
        never carrier details, never buy rates, never vendor bills."""
        job.ensure_one()
        if job.all_stops_completed:
            return "delivered"
        stops = job.stop_ids.filtered(lambda s: not s.planning_only)
        if not stops:
            return "estimated_arrival"
        if any(s.status == "en_route" for s in stops):
            return "in_transit"
        deliveries = stops.filtered(lambda s: s.stop_type == "delivery")
        if deliveries and deliveries[:1].status in ("en_route", "arrived"):
            return "out_for_delivery"
        last = stops.sorted("sequence")[-1:]
        if last and last.status in ("en_route", "arrived"):
            return "out_for_delivery"
        for s in stops:
            if s.eta_delay_minutes and abs(s.eta_delay_minutes) >= 5.0:
                return "updated_eta"
        return "estimated_arrival"

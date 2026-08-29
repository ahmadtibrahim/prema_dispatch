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
        """Delegates to the unified EtaEngine (Section C) — ONE authority.

        The Phase-11 pipeline this class once owned is superseded by
        eta_engine.EtaEngine.compute_job_eta (same anchor semantics:
        actuals for executed stops, held ETAs for en_route, override wins,
        delay propagated stop-by-stop). Kept as a compatibility entry
        point; all production callers go through the engine."""
        from ..services.eta_engine import EtaEngine
        return EtaEngine(self.env).compute_job_eta(
            job, anchor=anchor_time, source="live")

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

"""Load-pairing advisory for the estimator window (master §13).

When a dedicated truck or a corridor run serves a customer move, freight
already in the pipeline near the same corridor/timing can fill the empty
return leg or consolidate the outbound.  This service lists those
candidates so the AI-side advice and the dispatcher see the same pool.

Authority rules:
  - Candidates are REAL pipeline freight: bookings in draft/quoted/planned
    (not yet assigned to a vehicle → still combinable) whose corridor
    region pair matches legs of the customer route, within
    logistics.pairing_window_days of the proposed move.
  - Committed work (dispatched jobs) and weekly-plan reservations are
    reported as corridor CONTEXT (fill-rate), never as pairing candidates —
    the scheduler owns those; the estimator must not suggest stealing them.
  - Matching is region/route-level (corridor distances), not road-detour —
    every suggestion says "verify routing at dispatch time".
  - Advisory only.  Nothing books, moves, quotes or mutates anything here.
"""

import datetime

import logging

_logger = logging.getLogger(__name__)


class LoadPairingService:
    def __init__(self, env):
        self.env = env(su=True)

    def _window_days(self):
        try:
            return max(1, int(self.env["ir.config_parameter"].sudo().get_param(
                "logistics.pairing_window_days", "3") or 3))
        except (TypeError, ValueError):
            return 3

    # ── Public surface ──────────────────────────────────────────────

    def pairing_report(self, payload):
        """payload = normalized estimator request (stops w/ fsa_code, dates,
        distance).  Returns an advisory structure, never raises."""
        stops = [s for s in (payload.get("stops") or []) if isinstance(s, dict)]
        payload_regions = self._request_region_codes(stops)
        origin_code, dest_code = payload_regions
        out = {
            "scope": "no_route",
            "message": "Load pairing needs at least a region pair and a "
                       "date window.",
            "backhauls": [], "consolidations": [], "corridor_context": [],
        }
        if not origin_code or not dest_code:
            return out
        d0 = self._as_date(payload.get("pickup_date")) or \
            datetime.date.today() + datetime.timedelta(days=1)
        window = self._window_days()
        start, end = d0 - datetime.timedelta(days=window), \
            d0 + datetime.timedelta(days=window + 3)

        backhauls, consolidations = [], []
        for booking in self._pipeline_bookings(start, end):
            pair = self._booking_region_codes(booking)
            if not pair or pair == ("", ""):
                continue
            if pair == (dest_code, origin_code):
                backhauls.append(self._row(booking, "backhaul",
                                           "Return-leg load: %s → %s runs "
                                           "opposite to the customer move and "
                                           "fits the truck's empty return "
                                           "leg." % pair))
            elif pair == (origin_code, dest_code):
                consolidations.append(self._row(
                    booking, "consolidation",
                    "Same-direction freight %s → %s on the same corridor — "
                    "candidate to ride the outbound day." % pair))
            elif pair[0] == dest_code:
                backhauls.append(self._row(
                    booking, "partial_backhaul",
                    "Load starting in the delivery area (%s → %s) could "
                    "start the return leg." % pair))

        corridor_context = self._corridor_context(
            origin_code, dest_code, start, end)
        out = {
            "scope": "corridor" if (backhauls or consolidations) else
            ("context_only" if corridor_context else "empty_pipeline"),
            "message": "",
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "window_days": window,
            "request_region_pair": "%s → %s" % (origin_code, dest_code),
            "backhauls": backhauls,
            "consolidations": consolidations,
            "corridor_context": corridor_context,
        }
        return out

    # ── Evidence pools ──────────────────────────────────────────────

    def _pipeline_bookings(self, start, end):
        """Freight not yet assigned to a truck and open for combining,
        whose pickup falls INSIDE the pairing window (the window computed by
        the caller was never applied before — the whole pipeline came back)."""
        Booking = self.env["logistics.booking"]
        if "logistics.booking" not in self.env.registry:
            return Booking
        return Booking.search([
            ("state", "in", ("draft", "quoted", "planned")),
            ("pickup_date", ">=", start),
            ("pickup_date", "<=", end),
        ], order="pickup_date asc, id asc", limit=400)

    def _row(self, booking, kind, note):
        pallets = booking.pallets or 0
        weight = booking.weight_lbs or 0.0
        price = booking.calculated_price or 0.0
        pair = self._booking_region_codes(booking)
        pair_text = "%s → %s" % pair if pair and pair[0] and pair[1] else ""
        return {
            "kind": kind,
            "reference": booking.name or "Booking %s" % booking.id,
            "id": booking.id,
            "state": booking.state,
            "partner": booking.commercial_partner_id.name or "",
            "pallets": pallets,
            "weight_lbs": weight,
            "pickup_date": self._iso(booking.pickup_date),
            "estimated_price": round(price, 2),
            "note": note,
            "region_pair": pair_text,
        }

    # ── Corridor fill context for the found dates ───────────────────

    def _corridor_context(self, origin_code, dest_code, start, end):
        """Committed corridor occupancy around the dates (weekly plan +
        dispatched jobs on matching corridor departures)."""
        Reservation = self.env["logistics.weekly.plan.reservation"]
        Job = self.env["prema.dispatch.job"]
        origin_region = self._region_by_code(origin_code)
        dest_region = self._region_by_code(dest_code)
        rows = []
        if not origin_region or not dest_region:
            return rows
        if "logistics.weekly.plan.reservation" in self.env.registry:
            for res in Reservation.search([
                ("state", "=", "planned"),
                ("plan_date", ">=", start),
                ("plan_date", "<=", end),
            ], order="plan_date asc, id asc", limit=120):
                dep = res.corridor_departure_id
                if not dep or not dep.corridor_id:
                    continue
                if not dep.corridor_id.resolve_region_segment(
                        origin_region, dest_region):
                    continue
                rows.append({
                    "date": self._iso(dep.departure_date or res.plan_date),
                    "kind": "planned_run",
                    "label": (res.name or "Weekly occurrence"),
                    "vehicle": dep.vehicle_id.name or dep.vehicle_id.license_plate
                    or "",
                    "pallets": res.pallets,
                })
        if "prema.dispatch.job" in self.env.registry:
            # Window + completed filter: without them this scanned the OLDEST
            # 400 non-cancelled jobs in history, not the committed work in
            # the pairing window.
            utc_end = datetime.datetime.combine(
                end + datetime.timedelta(days=1), datetime.time.min)
            for job in Job.search([
                ("stage_id.is_cancelled", "=", False),
                ("stage_id.is_completed", "!=", True),
                ("scheduled_pickup", ">=",
                 datetime.datetime.combine(start, datetime.time.min)),
                ("scheduled_pickup", "<=", utc_end),
            ], order="scheduled_pickup asc, id asc", limit=400):
                span = self._job_region_codes(job)
                if not span:
                    continue
                code_matches = span in (
                    (origin_code, dest_code), (dest_code, origin_code))
                if not code_matches:
                    continue
                rows.append({
                    "date": self._iso(job.scheduled_pickup),
                    "kind": "committed",
                    "label": job.name or job.tracking_number or "",
                    "vehicle": job.vehicle_id.name or job.vehicle_id.license_plate
                    or "",
                    "pallets": getattr(job, "pallets", 0) or 0,
                })
        rows.sort(key=lambda r: r["date"] or "")
        return rows[:30]

    # ── Region helpers ──────────────────────────────────────────────

    def _request_region_codes(self, stops):
        first_pickup = next((s for s in stops if s.get("type") == "pickup"),
                            stops[0] if stops else {})
        last_delivery = next((s for s in reversed(stops)
                              if s.get("type") == "delivery"),
                             stops[-1] if stops else {})
        return (self._postal_to_region_code(first_pickup.get("fsa_code") or ""),
                self._postal_to_region_code(last_delivery.get("fsa_code") or ""))

    def _postal_to_region_code(self, code):
        code = str(code or "").strip().upper()
        if not code:
            return ""
        fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(code)
        if not fsa or not fsa.region_id:
            return ""
        try:
            from .region_resolver import RegionResolver
            region = RegionResolver(self.env).canonical_region(fsa.region_id)
        except Exception:
            region = fsa.region_id
        return region.code if region else ""

    def _booking_region_codes(self, booking):
        legs = booking.leg_ids
        first, last = legs[:1], legs[-1:]
        if not first:
            return ()
        return ((first.origin_region_id.code if first.origin_region_id else ""),
                (last.destination_region_id.code
                 if last and last.destination_region_id else ""))

    def _job_region_codes(self, job):
        """First-pickup → last-delivery region codes for a dispatched job,
        via the job's saved locations (location postal → FSA → canonical
        region). Unknown stays unknown — honesty over guessing."""
        stops = job.stop_ids.sorted(key=lambda s: (s.sequence or 0, s.id))
        if not stops:
            return False
        first, last = stops[0], stops[-1]
        codes = []
        for stop in (first, last):
            loc = stop.saved_location_id
            code = ""
            if loc:
                postal = ""
                if "postal_code" in loc._fields and loc.postal_code:
                    postal = str(loc.postal_code)
                code = self._postal_to_region_code(postal)
            codes.append(code)
        return tuple(codes)

    def _region_by_code(self, code):
        if not code:
            return False
        return self.env["logistics.region"].search(
            [("code", "=", code), ("active", "=", True)], limit=1)

    @staticmethod
    def _as_date(value):
        if not value:
            return False
        if hasattr(value, "date") and hasattr(value, "hour"):
            return value.date()
        if hasattr(value, "isoformat") and not hasattr(value, "hour"):
            return value
        try:
            return datetime.date.fromisoformat(str(value)[:10])
        except ValueError:
            return False

    @staticmethod
    def _iso(value):
        if not value:
            return ""
        try:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return datetime.date.fromisoformat(str(value)[:10]).isoformat()
        except (TypeError, ValueError):
            return ""

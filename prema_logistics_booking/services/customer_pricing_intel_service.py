"""Customer-specific pricing intelligence (master §12) — read-only, built
on the booking history the CUSTOMER actually paid.

Prema AI drafts advice and comparisons; the booking history below is the
evidence base and it is interpreted here, on the Dispatch side, so both
sides read the same numbers.

Rules:
  - Isolation: the customer scope is the lead's commercial entity and its
    contact children (commercial_partner_id root), never the whole ledger.
  - Comparables: confirmed/completed bookings in the same corridor region
    pair and a pallet band around this request; manual price overrides are
    FLAGGED and de-weighted (they carry the human's explicit override and
    are not "the market"), never silently blended in.
  - Honesty: 0-2 comparables produce a "thin history" verdict, never a
    confident band.  No booking is ever sent out of this service — rows are
    labelled by booking reference for the AI's internal reasoning only.
  - Floor: a comparable whose net rate falls below operating cost ×
    (1 + logistics.minimum_margin_pct) is flagged "below floor" — the same
    minimum the execution scenario engine enforces at conversion.
  - Nothing here prices a corridor departure or mutes Dispatch authority;
    numbers below are evidence for a human/AI recommendation, not a quote.
"""

import datetime
import math

INTEL_RECENCY_HALF_LIFE_DAYS = 90.0  # weight halves every 90 days
MAX_DISTANCE_MISMATCH_PCT = 25.0
MAX_COMPARABLE_ROWS = 40


def _r(value, digits=2):
    if value is None:
        return False
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return False


class CustomerPricingIntelService:
    """Booking-history intelligence for one customer and one move shape."""

    def __init__(self, env):
        self.env = env(su=True)

    # ── Public surface ──────────────────────────────────────────────

    def intel_report(self, partner_id, payload=None):
        """One evidence block: comparable rows + stats + flags + lanes.

        payload mirrors the scenario request (stops with fsa_code, pallets,
        weight_lbs, equipment, distance_km) so the report lines up with the
        three cards the UI shows next to it.
        """
        payload = payload or {}
        partner = self.env["res.partner"].browse(int(partner_id or 0))
        base = {
            "scope": "no_customer",
            "message": "No customer is attached to this estimate — customer "
                       "history is not available (generic fleet history "
                       "only, where the AI shows it).",
            "comparables": [], "lanes": [], "stats": {}, "flags": [],
            "evidence_date": datetime.date.today().isoformat(),
        }
        if not partner.exists():
            return base

        partner_root = partner.commercial_partner_id or partner
        customers = partner_root
        customers |= self.env["res.partner"].search([
            ("commercial_partner_id", "=", partner_root.id),
            ("active", "in", (True, False))]) if partner_root else customers

        moves = self._candidate_bookings(customers)
        if not moves:
            return dict(base, scope="customer_no_history",
                        message="No past bookings found for this customer — "
                                "there is no history to compare against yet.")
        pallets = max(int(payload.get("pallets") or 0), 0)
        weight_lbs = max(float(payload.get("weight_lbs") or 0.0), 0.0)
        request_km = payload.get("distance_km") or 0.0
        equip = str(payload.get("equipment") or "dry")
        origin_code, dest_code = self._request_region_codes(payload)

        comparables = self._compare(moves, origin_code, dest_code, pallets,
                                    weight_lbs, equip, request_km)
        stats = self._stats(comparables)
        if origin_code and dest_code:
            comparables = comparables[:MAX_COMPARABLE_ROWS]
            rows = [self._row_view(c) for c in comparables]
            flags = self._flags(comparables, stats)
            lanes = self._customer_lanes(moves)
            return {
                "scope": "customer",
                "partner_id": partner_root.id,
                "partner_name": partner_root.name,
                "request_region_pair": "%s → %s" % (origin_code, dest_code)
                if origin_code and dest_code else "",
                "request_shape": {
                    "pallets": pallets, "weight_lbs": weight_lbs,
                    "equipment": equip, "distance_km": request_km,
                },
                "comparables": rows,
                "stats": stats,
                "flags": flags,
                "lanes": lanes,
                "evidence_date": datetime.date.today().isoformat(),
            }
        return dict(base, scope="customer_no_region",
                    message="This move does not map to a known corridor "
                            "region pair yet — comparable history is shown "
                            "by pallet band only on the AI side.")

    # ── Evidence base ───────────────────────────────────────────────

    def _candidate_bookings(self, partners):
        Booking = self.env["logistics.booking"]
        if "logistics.booking" not in self.env.registry:
            return Booking
        return Booking.search([
            ("commercial_partner_id", "in", partners.ids),
            ("state", "in", ("confirmed", "planned", "in_execution",
                             "delivered", "completed")),
        ], order="confirmed_at desc, id desc", limit=300)

    def _compare(self, moves, origin_code, dest_code, pallets, weight_lbs,
                 equip, request_km):
        rows = []
        for b in moves:
            legs = b.leg_ids
            first, last = legs[:1], legs[-1:]
            o_code = first.origin_region_id.code if first and \
                first.origin_region_id else ""
            d_code = last.destination_region_id.code if last and \
                last.destination_region_id else ""
            region_match = bool(
                origin_code and dest_code and o_code == origin_code
                and d_code == dest_code)
            if not region_match and not (origin_code and dest_code):
                # Region pair unknown on the request — band-only fallback.
                region_match = True
            if not region_match:
                continue
            est_km = self._booking_span_km(b)
            if request_km and est_km:
                mismatch = abs(est_km - request_km) / max(request_km, 1.0) * 100
                if mismatch > MAX_DISTANCE_MISMATCH_PCT:
                    continue
            if pallets and b.pallets and abs(b.pallets - pallets) > max(
                    4, int(pallets * 0.5)):
                continue
            rows.append(self._row(b, est_km, region_match))
        return sorted(rows, key=lambda r: r["confirmed_at"] or "",
                      reverse=True)

    def _row(self, booking, est_km, region_match):
        price = booking.calculated_price or 0.0
        override = bool(booking.manual_price_override)
        net_price = booking.calculated_price or booking.final_quoted_price or 0.0
        age_days = 0
        if booking.confirmed_at:
            try:
                age_days = (datetime.datetime.utcnow() -
                            self._naive(booking.confirmed_at)).days
            except (TypeError, ValueError):
                age_days = 0
        recency = math.exp(-age_days / INTEL_RECENCY_HALF_LIFE_DAYS)
        return {
            "id": booking.id,
            "reference": booking.name or "Booking %s" % booking.id,
            "state": booking.state,
            "confirmed_at": self._iso(booking.confirmed_at),
            "age_days": age_days,
            "pallets": booking.pallets or 0,
            "weight_lbs": booking.weight_lbs or 0.0,
            "est_distance_km": est_km,
            "region_pair": region_match,
            "calculated_price": _r(net_price),
            "rate_per_km": _r(net_price / est_km, 3) if est_km else False,
            "price_per_pallet": _r(net_price / max(booking.pallets or 1, 1)),
            "margin_pct": booking.margin_pct,
            "manual_override": override,
            "manual_reason": booking.manual_price_reason or "",
            "recency_weight": round(recency, 4),
            "currency": booking.currency_id.name if booking.currency_id else "CAD",
        }

    def _row_view(self, c):
        view = dict(c)
        for key in ("calculated_price", "rate_per_km", "price_per_pallet"):
            view[key] = _r(view.get(key))
        return view

    # ── Statistics (manual overrides de-weighted, never removed) ────

    def _stats(self, rows):
        normal = [r for r in rows if not r["manual_override"]]
        if not rows:
            return {"n": 0, "verdict": "thin"}
        def median(vals):
            if not vals:
                return False
            vals = sorted(vals)
            mid = len(vals) // 2
            return vals[mid] if len(vals) % 2 else \
                (vals[mid - 1] + vals[mid]) / 2.0
        base_rows = normal or rows
        return {
            "n": len(rows),
            "n_normal": len(normal),
            "n_overridden": len(rows) - len(normal),
            "verdict": "thin" if len(base_rows) < 3 else "adequate",
            "median_price": _r(median([r["calculated_price"]
                                       for r in base_rows])),
            "median_rate_per_km": _r(median(
                [r["rate_per_km"] for r in base_rows
                 if r.get("rate_per_km")]), 3),
            "median_price_per_pallet": _r(median(
                [r["price_per_pallet"] for r in base_rows])),
            "band_low": _r(min(r["calculated_price"] for r in base_rows)),
            "band_high": _r(max(r["calculated_price"] for r in base_rows)),
            "price_pct_over_60d": self._recent_vs_all(base_rows),
        }

    def _recent_vs_all(self, rows):
        """Median price of ≤60-day bookings vs all — trend hint (False when
        the recent set is empty)."""
        recent = [r["calculated_price"] for r in rows
                  if r["age_days"] <= 60]
        older = [r["calculated_price"] for r in rows
                 if r["age_days"] > 60]
        if not recent or not older:
            return False
        avg = lambda v: sum(v) / len(v)  # noqa
        older_avg = avg(older)
        if older_avg <= 0:
            return False
        return _r((avg(recent) - older_avg) / older_avg * 100.0, 1)

    # ── Flags ───────────────────────────────────────────────────────

    def _flags(self, rows, stats):
        """Facts the AI/UI must surface next to any recommendation."""
        flags = []
        over = [r for r in rows if r["manual_override"]]
        if over:
            flags.append({
                "type": "manual_override",
                "level": "warn",
                "text": "%d comparable booking(s) carry a manual price "
                        "override — treat them as deliberate exceptions, "
                        "not market evidence (%s)."
                        % (len(over), ", ".join(
                            (r["reference"] + (": " + r["manual_reason"][:80])
                             if r["manual_reason"] else r["reference"])
                            for r in over[:3]))})
        if stats.get("verdict") == "thin":
            flags.append({
                "type": "thin_history", "level": "info",
                "text": "Only %d comparable booking(s) found — the band "
                        "below is indicative, not a market verdict."
                        % stats.get("n", 0)})
        floor_rows = [r for r in rows if r.get("rate_per_km") and
                      self._below_floor(r)]
        if floor_rows:
            flags.append({
                "type": "below_floor", "level": "warn",
                "text": "%d comparable(s) sit at/below the minimum-margin "
                        "operating floor (logistics.minimum_margin_pct) — "
                        "re-pricing those lanes needs a human decision: %s."
                        % (len(floor_rows),
                           ", ".join(r["reference"]
                                     for r in floor_rows[:4]))})
        return flags

    def _below_floor(self, row):
        """Estimate the own-fleet operating cost for the row's distance and
        compare against what the customer actually paid."""
        from .estimator_scenario_service import EstimatorScenarioService
        km = row.get("est_distance_km") or 1.0
        est = EstimatorScenarioService(self.env)._cost_for(
            self._default_vehicle(), km, km / 80.0,  # 80 kph planning speed
            row.get("weight_lbs") or 0.0, {})
        if est[0] is False:
            return False
        minimum_margin = float(self.env["ir.config_parameter"].sudo()
                               .get_param("logistics.minimum_margin_pct", 10.0)
                               or 10.0)
        floor = est[0] * (1 + minimum_margin / 100.0)
        return (row.get("rate_per_km") or 0.0) * (row.get("est_distance_km")
                                                  or 1.0) < floor

    def _default_vehicle(self):
        vehicle = self.env["fleet.vehicle"].search(
            [("active", "=", True), ("x_operational_logistics", "=", True)],
            order="id asc", limit=1)
        return vehicle or self.env["fleet.vehicle"].search(
            [("active", "=", True)], order="id asc", limit=1)

    # ── Customer lanes (top pairs by recency × price) ───────────────

    def _customer_lanes(self, moves):
        lanes = {}
        for b in moves:
            legs = b.leg_ids
            first, last = legs[:1], legs[-1:]
            key = ((first.origin_region_id.code if first and
                    first.origin_region_id else "?") + "→" +
                   (last.destination_region_id.code if last and
                    last.destination_region_id else "?"))
            lane = lanes.setdefault(key, {
                "pair": key, "count": 0, "pallets": 0,
                "price_total": 0.0, "last": ""})
            lane["count"] += 1
            lane["pallets"] += b.pallets or 0
            lane["price_total"] += b.calculated_price or 0.0
            lane["last"] = self._iso(b.confirmed_at) or lane["last"]
        out = []
        for lane in lanes.values():
            out.append({
                "pair": lane["pair"], "count": lane["count"],
                "pallets": lane["pallets"],
                "avg_price": _r(lane["price_total"] / lane["count"]),
                "last_date": lane["last"],
            })
        return sorted(out, key=lambda l: (l["count"], l["last_date"] or ""),
                      reverse=True)[:8]

    # ── Small helpers ───────────────────────────────────────────────

    def _booking_span_km(self, booking):
        """Great-circle distance (km) between the FIRST leg's origin stop
        and the LAST leg's destination stop, straight from the booking-leg
        stop snapshots (logistics.booking.stop.latitude/longitude).

        No logistics model carries a stored leg distance, so the span is
        derived here; False when either endpoint lacks coordinates (the row
        then never claims a distance or a rate-per-km).
        """
        legs = booking.leg_ids
        first = legs[:1].origin_stop_id if legs[:1] else False
        last = legs[-1:].destination_stop_id if legs[-1:] else False
        if not first or not last:
            return False
        lat1, lng1 = first.latitude, first.longitude
        lat2, lng2 = last.latitude, last.longitude
        if not lat1 or not lng1 or not lat2 or not lng2:
            return False
        radius = 6371.0  # km
        dlat = math.radians(lat2 - lat1)
        dlng = math.radians(lng2 - lng1)
        a = (math.sin(dlat / 2.0) ** 2
             + math.cos(math.radians(lat1))
             * math.cos(math.radians(lat2))
             * math.sin(dlng / 2.0) ** 2)
        return _r(2 * radius * math.asin(min(1.0, math.sqrt(a))))


    def _request_region_codes(self, payload):
        stops = [s for s in (payload.get("stops") or [])
                 if isinstance(s, dict)]
        first_pickup = next((s for s in stops if s.get("type") == "pickup"),
                            stops[0] if stops else {})
        last_delivery = next((s for s in reversed(stops)
                              if s.get("type") == "delivery"),
                             stops[-1] if stops else {})
        codes = []
        for stop in (first_pickup, last_delivery):
            code = str(stop.get("fsa_code") or "").strip().upper()
            fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(code) \
                if code else False
            if not fsa or not fsa.region_id:
                codes.append("")
                continue
            region = fsa.region_id
            try:
                from .region_resolver import RegionResolver
                region = RegionResolver(self.env).canonical_region(region)
            except Exception:
                pass
            codes.append(region.code if region else "")
        return tuple(codes[:2]) if len(codes) == 2 else ("", "")

    @staticmethod
    def _naive(value):
        if hasattr(value, "replace"):
            return value.replace(tzinfo=None)
        return datetime.datetime.fromisoformat(str(value))

    @staticmethod
    def _iso(value):
        if not value:
            return ""
        try:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return datetime.datetime.fromisoformat(str(value)).isoformat()
        except (TypeError, ValueError):
            return ""

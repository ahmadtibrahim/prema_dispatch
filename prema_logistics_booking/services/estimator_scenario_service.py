"""Estimator scenario engine — THREE comparable, non-mutating scenario
cards from one canonical response (master §10).

Authority rules:
  - Trucks/capacity: CapacityEngine + the vehicle's configured layouts.
  - Occupancy/ELD: EstimatorAvailabilityService (full operational interval,
    §11.3-11.5) — never pickup-time alone.
  - Own-fleet operating cost: the SAME Prema AI PricingEngine the booking
    pipeline calls (in-process engine import, mirroring
    logistics.booking._estimate_cost_from_request), evaluated per card on
    that card's own distance/duration/service time.  When the engine cannot
    cost the move the card says "requires estimate" — a $0 figure is never
    invented.
  - Customer sell authority: cost × (1 + margin_pct/100) exactly like
    PricingEngine.suggested_rate.  Markup-on-cost vs gross-margin-on-revenue
    are computed from the same two numbers and labelled distinctly (§10.4).
  - Scheduled LTL corridor: PricingService.calculate(resolve_departures)
    is the ONLY sanctioned search — this service never queries departures
    directly for quoting.  Alternative feasible days come from that same
    resolver scanning forward (§10.7).
  - Nothing here sends, confirms, creates bookings/quotes, or mutates a
    plan.  Every figure is advisory; a human action creates the document.

The engine-side orchestrator persists request/response snapshots (audit).
"""

import datetime

MAX_NETWORK_SCAN_DAYS = 14
MAX_ALT_NETWORK_DATES = 3
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


def _r(value, digits=2):
    if value is None or value is False:
        return False
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return False


def _money(value):
    return _r(value) if value is not False else False


def _num(value, digits=1):
    """Round to digits or False — mirrors the engine helper."""
    return _r(value, digits=digits)


class EstimatorScenarioService:
    """Build the three scenario cards from a normalized estimator payload."""

    def __init__(self, env):
        self.env = env(su=True)
        from .estimator_availability_service import (
            EstimatorAvailabilityService, company_tz)
        self.availability = EstimatorAvailabilityService(env)
        self.tz = company_tz(env)

    # ── Entry point ─────────────────────────────────────────────────

    def build_cards(self, payload):
        """payload is the normalized engine-side request (never trusted
        verbatim; every number is re-derived or re-validated here)."""
        payload = payload or {}
        vehicle = self.env["fleet.vehicle"].browse(
            int(payload.get("vehicle_id") or 0))
        if not vehicle.exists() or not vehicle.active:
            return {"scenarios": [], "fatal": "vehicle_not_found",
                    "message": "The selected truck no longer exists."}
        vehicle = vehicle.sudo()

        warnings = list(payload.get("data_warnings") or [])
        stops = [s for s in (payload.get("stops") or []) if isinstance(s, dict)]
        legs = [l for l in (payload.get("route_legs") or []) if isinstance(l, dict)]
        equipment = str(payload.get("equipment") or "dry")
        if equipment not in ("dry", "reefer"):
            equipment = "dry"
        pallets = max(int(payload.get("pallets") or 0), 0)
        weight_lbs = max(float(payload.get("weight_lbs") or 0.0), 0.0)
        cases = max(int(payload.get("total_cases") or 0), 0)
        required_temperature_c = payload.get("required_temperature_c")

        if not stops:
            return {"scenarios": [], "fatal": "no_stops",
                    "message": "No operational stops were extracted."}
        if not legs:
            warnings.append(
                "No routable stop-to-stop legs were returned (missing "
                "coordinates) — dedicated drive times and costs cannot be "
                "computed; only the scheduled network option can be priced.")

        # MP2: capacity validation uses the peak load aboard each route
        # segment (pickup → unloads → second pickup …), not merely the
        # grand total of all deliveries.
        peak_onboard = max(int(payload.get("peak_onboard_pallets") or 0), 0)
        capacity_pallets = max(pallets, peak_onboard)
        capacity = self.availability.capacity_report(
            vehicle, capacity_pallets, weight_lbs, equipment=equipment,
            liftgate_pickup=bool(payload.get("liftgate_pickup")),
            liftgate_delivery=bool(payload.get("liftgate_delivery")),
            cases=cases)
        # §9 dedupe: the engine already says "weights were not extracted"
        # when the extraction found none — the capacity report's own
        # unknown-weight line must not repeat it.
        if any("weights were not extracted" in (w or "").lower()
               for w in warnings) and weight_lbs <= 0:
            capacity["warnings"] = [w for w in capacity["warnings"]
                                    if "weight is unknown" not in
                                    (w or "").lower()]
        margin_pct = float(payload.get("margin_pct")
                           or (payload.get("params") or {}).get("margin_pct")
                           or 20.0)
        overrides = payload.get("overrides") or {}
        if overrides and not isinstance(overrides, dict):
            overrides = {}

        route_km = _r(sum(float(l.get("distance_km") or 0.0) for l in legs), 1)
        route_hrs = sum(float(l.get("duration_hrs") or 0.0) for l in legs)
        service_min = int(payload.get("service_minutes") or 0)
        reposition_km = _r(payload.get("reposition_to_first_km"))
        reposition_hrs = float(payload.get("reposition_to_first_hrs") or 0.0)
        return_km = _r(payload.get("return_leg_km"))
        return_hrs = float(payload.get("return_leg_hrs") or 0.0)
        home = payload.get("truck_home") or False

        # Requested / suggested date resolution (§10.7).
        requested_raw = payload.get("pickup_date")
        requested = self._as_date(requested_raw)
        today = datetime.date.today()
        if requested and requested < today:
            warnings.append(
                "Requested pickup date is in the past — the earliest start "
                "shown is today.")
            requested = today
        d0, date_warnings = self._resolve_start_date(vehicle, requested)
        warnings.extend(date_warnings)

        # Duty gate depends on the proposed start: "currently driving" only
        # blocks a same-day start; future starts downgrade to a warning.
        eld = self.availability.eld_advisory(
            payload.get("driver"), start_date=d0)

        service_context = {
            "vehicle": vehicle, "capacity": capacity, "eld": eld,
            "stops": stops, "legs": legs, "route_km": route_km,
            "route_hrs": route_hrs, "service_min": service_min,
            "reposition_km": reposition_km, "reposition_hrs": reposition_hrs,
            "return_km": return_km, "return_hrs": return_hrs,
            "home": home, "margin_pct": margin_pct,
            "overrides": overrides, "weight_lbs": weight_lbs,
            "pallets": pallets, "equipment": equipment,
            "cases": cases,
            "required_temperature_c": required_temperature_c,
            "d0": d0, "date_requested": bool(requested_raw),
            "warnings": warnings,
        }

        scenarios = [self._card_dedicated(service_context, round_trip=False)]
        if payload.get("return_to_home"):
            # The round-trip card is only offered when the request asks for
            # an empty return to base — never invented for a one-way ask.
            round_trip = self._card_dedicated(service_context, round_trip=True)
            if round_trip is not None:
                scenarios.append(round_trip)
        scenarios.append(self._card_scheduled_corridor(service_context))

        return {
            "scenarios": scenarios,
            "fatal": False,
            "warnings": warnings,
            "capacity": capacity,
            "d0": self._iso(d0),
            "requested_date": self._iso(requested),
        }

    # ── Dedicated cards (1 one-way / 2 round trip) ──────────────────

    def _card_dedicated(self, ctx, round_trip):
        vehicle = ctx["vehicle"]
        if round_trip:
            # A round trip is only cost-able when the truck has a configured
            # home AND the empty return leg was actually routed — the card
            # never asserts a home return it did not price.
            if not ctx["home"]:
                round_block = [
                    "Truck home coordinates are not configured "
                    "(x_home_base_lat/lng) — the Round Trip variant cannot "
                    "be costed. Use the One Way card or configure the "
                    "truck's home."]
            elif not (ctx["return_km"] is not False and ctx["return_km"]):
                round_block = [
                    "The empty return leg from the final delivery back to "
                    "the truck's home could not be routed — the Round Trip "
                    "variant cannot be costed. Use the One Way card."]
            else:
                round_block = None
            if round_block:
                return {
                    "key": "dedicated_round_trip",
                    "title": "Round Trip",
                    "badge": "infeasible",
                    "feasible": False,
                    "blocking": round_block,
                    "assumptions": [], "warnings": list(ctx["warnings"]),
                    "confidence": CONFIDENCE_LOW,
                    "distance_km": False, "drive_hrs": False,
                    "cost": False, "suggested_sell": False,
                    "pickup_date": self._iso(ctx["d0"]),
                    "delivery_date": False,
                    "availability": {"status": "n/a", "conflicts": []},
                }
        label = "Round Trip" if round_trip else "One Way"
        key = "dedicated_round_trip" if round_trip else "dedicated_one_way"

        customer_km = ctx["route_km"] or 0.0
        customer_hrs = ctx["route_hrs"]
        total_km = customer_km
        total_hrs = customer_hrs
        incremental_km = 0.0
        incremental_note = "Direct stop-to-stop route (no reposition)."
        if ctx["reposition_km"] is not False and ctx["reposition_km"]:
            total_km += ctx["reposition_km"]
            total_hrs += ctx["reposition_hrs"]
            incremental_km += ctx["reposition_km"]
        if round_trip and ctx["return_km"] is not False and ctx["return_km"]:
            total_km += ctx["return_km"]
            total_hrs += ctx["return_hrs"]
            incremental_km += ctx["return_km"]
        if ctx["reposition_km"] is not False and ctx["reposition_km"]:
            incremental_note = (
                "Includes %.0f km empty reposition from truck home; %s"
                % (ctx["reposition_km"] or 0.0,
                   "plus %.0f km empty return home."
                   % (ctx["return_km"] or 0.0)
                   if round_trip and ctx["return_km"]
                   else "ends at the final delivery (one-way)."))
        elif round_trip and ctx["return_km"] is not False and ctx["return_km"]:
            incremental_note = (
                "Includes %.0f km empty return leg to truck home."
                % (ctx["return_km"] or 0.0))
        total_hrs += ctx["service_min"] / 60.0
        total_km = _r(total_km, 1)
        total_hrs = _r(total_hrs, 1)

        blocking = list(ctx["capacity"]["blocking"]) + list(ctx["eld"]["blocking"])
        warnings = list(ctx["warnings"]) + list(ctx["capacity"]["warnings"])
        warnings.extend(ctx["eld"]["warnings"])
        if not round_trip and ctx["return_km"] is not False and ctx["return_km"]:
            # One-way ends away from home — real fleet mileage.
            warnings.append(
                "One-way trip ends away from truck home — plan the "
                "return/reposition before the next commitment.")

        # Cost authority: the SAME PricingEngine the booking pipeline uses,
        # evaluated on this card's own distance/duration/service.
        cost = cost_source = cost_error = False
        cost_breakdown = False
        if total_km and total_hrs:
            cost, cost_error = self._cost_for(
                vehicle, total_km, total_hrs, ctx["weight_lbs"],
                ctx["overrides"])
            cost_source = "PricingEngine (booking estimator authority)"
        else:
            cost_error = "stop-to-stop distance/drive time is unknown"
        if cost is not False:
            cost_breakdown = cost
            cost = cost.get("total")
            sell = _money(cost * (1.0 + ctx["margin_pct"] / 100.0))
            profit = _r(sell - cost)
            markup_on_cost = ctx["margin_pct"]
            gross_on_revenue = _r(profit / sell * 100.0, 1) if sell else False
        else:
            sell = profit = markup_on_cost = gross_on_revenue = False
            if cost_error:
                blocking.append(
                    "Operating cost unavailable (%s) — the card reads "
                    "'requires estimate'; no $0-cost scenario is shown."
                    % cost_error)

        sim = self._simulate_days(ctx, total_hrs, round_trip=round_trip)
        conflicts = self.availability.occupancy_conflicts(
            vehicle.id, sim["interval_start"], sim["interval_end"])
        if conflicts:
            blocking.append(
                "Truck already has committed work overlapping this move — "
                "the truck cannot do both; pick an alternative date shown "
                "on the card.")
            warnings.append(
                "Requested/planned timing overlaps %d existing commitment(s) "
                "on this truck." % len(conflicts))
        if not ctx["eld"].get("driver_id"):
            warnings.append("No driver assigned to the truck.")

        feasible = not blocking
        confidence = CONFIDENCE_HIGH
        if ctx["route_hrs"] <= 0 or ctx["service_min"] <= 0:
            confidence = CONFIDENCE_MEDIUM
        if not ctx["pallets"] and not ctx["weight_lbs"]:
            confidence = CONFIDENCE_LOW

        # §9 feasibility classes: VERIFIED only when every key check
        # actually ran; incomplete evidence downgrades to CONDITIONAL
        # with the specific reasons — never an unconditional FEASIBLE.
        conditional_reasons = []
        if cost is False:
            conditional_reasons.append(
                "Operating cost could not be computed (%s)."
                % (cost_error or "no route distance/duration"))
        if not (ctx["pallets"] or ctx["weight_lbs"] or ctx.get("cases")):
            conditional_reasons.append(
                "No freight quantities are known — capacity is "
                "unverified.")
        elif not ctx["weight_lbs"] and not ctx.get("cases"):
            conditional_reasons.append(
                "Weight is unknown — payload feasibility is unverified.")
        if ctx["route_hrs"] <= 0:
            conditional_reasons.append(
                "Drive times could not be computed for the stop set.")
        if not ctx["eld"].get("driver_id"):
            conditional_reasons.append(
                "No driver assigned to the truck — driver-hours "
                "compliance is unverified.")
        if not ctx["capacity"].get("payload_lbs"):
            conditional_reasons.append(
                "The truck has no configured payload — weight "
                "feasibility is unverified.")
        feasibility_class = ("infeasible" if blocking else
                             "conditional" if conditional_reasons
                             else "verified")

        assumptions = [
            "Drive times are %s driving-route estimates; service time is "
            "%d min in total%s."
            % ("Mapbox" if ctx["route_hrs"] else "planning-speed",
               ctx["service_min"],
               " (defaulted — no service durations were extracted)"
               if ctx["service_min"] <= 0 else ""),
            "Suggested sell = operating cost × (1 + %.1f%% margin on cost) "
            "— the same formula the booking estimator uses." % ctx["margin_pct"],
        ]
        if round_trip:
            assumptions.append(
                "Truck returns to its home base after the final delivery.")

        return {
            "key": key, "title": label,
            "badge": (feasibility_class
                      if feasibility_class != "infeasible"
                      else ("requires_estimate"
                            if cost is False and total_km
                            else "infeasible")),
            "feasible": feasible,
            "feasibility_class": feasibility_class,
            "conditional_reasons": conditional_reasons,
            "truck": vehicle.name or vehicle.license_plate or "",
            "pickup_date": self._iso(sim["pickup_date"]),
            "delivery_date": self._iso(sim["delivery_date"]),
            "return_date": self._iso(sim["return_date"]) if round_trip else False,
            "date_requested": bool(ctx["date_requested"]),
            "distance_km": total_km,
            "drive_hrs": total_hrs,
            "service_minutes": ctx["service_min"],
            "incremental_km": _r(incremental_km, 1),
            "incremental_note": incremental_note,
            "capacity": ctx["capacity"],
            "cost": cost, "cost_source": cost_source,
            "cost_breakdown": cost_breakdown,
            "suggested_sell": sell,
            "markup_pct_on_cost": markup_on_cost,
            "gross_margin_pct": gross_on_revenue,
            "profit": profit,
            "schedule": self._stop_schedule(ctx),
            "availability": {
                "status": ("no_conflicts_found" if not conflicts
                           else "conflicts"),
                "checked": bool(ctx["route_hrs"] or conflicts
                                or ctx["eld"].get("driver_id")),
                "note": ("Checked against this truck's planned bookings "
                         "for the pickup window; a driver duty advisory "
                         "is %s." % ("shown above"
                                     if ctx["eld"].get("driver_id")
                                     else "not available (no driver)")),
                "conflicts": conflicts,
                "next_free_date": self._iso(
                    self.availability.first_free_date(
                        vehicle.id,
                        (sim["pickup_date"] or datetime.date.today())
                        + datetime.timedelta(days=1)))
                if conflicts else False,
                "duty": ctx["eld"].get("duty"),
            },
            "assumptions": assumptions,
            "warnings": warnings,
            "blocking": blocking,
            "confidence": confidence,
        }

    # ── Corridor card (3 scheduled LTL / weekly corridor) ───────────

    def _stop_schedule(self, ctx):
        """§6 — stop-by-stop schedule from dispatch scheduling rules.

        Planned schedules only (truck home + planned bookings, never stale
        GPS): travel from the preceding stop, facility-hours/appointment
        waiting, saved-location service durations, driver-hours budget
        with overnight rolls, remaining onboard per stop. Existing
        bookings are read — never moved or altered."""
        import pytz
        from .estimator_availability_service import local_datetime
        from .itinerary_planner import ItineraryPlanner
        planner = ItineraryPlanner(self.env)
        limits = self.availability.hos_limits()
        vehicle = ctx["vehicle"]
        stops = ctx["stops"]
        legs = ctx["legs"]
        rows = []
        notes = []
        start = local_datetime(ctx["d0"], limits["workday_start_hour"],
                              self.tz)
        current = start
        drive_budget_min = limits["max_drive_hours_per_day"] * 60.0
        drive_left = drive_budget_min
        onboard_pallets = onboard_cases = 0

        for i, stop in enumerate(stops):
            if i == 0:
                travel_min = (float(ctx.get("reposition_hrs") or 0.0) * 60.0
                              if ctx.get("reposition_hrs") else 0.0)
            else:
                leg = legs[i - 1] if i - 1 < len(legs) else {}
                travel_min = float(leg.get("duration_hrs") or 0.0) * 60.0
            if travel_min > drive_left:
                rolled = start + datetime.timedelta(days=1)
                notes.append(
                    "Overnight: driving to stop %d would exceed the %sh "
                    "driver-day budget — the schedule rolls to %s."
                    % (i + 1, limits["max_drive_hours_per_day"],
                       rolled.date().isoformat()))
                current = max(current, rolled)
                drive_left = drive_budget_min
            arrival = current + datetime.timedelta(minutes=travel_min)
            drive_left -= travel_min

            snap = stop.get("operating_hours_snapshot") or {}
            tz_name = stop.get("tz_name") or "America/Toronto"
            window_kind = str(stop.get("time_window_type") or "any")
            plan_stop = {
                "timezone": tz_name,
                "operating_hours_snapshot": snap,
                "timing_type": ("exact_appointment" if window_kind == "exact"
                                else "time_window"
                                if window_kind == "window" else "flexible"),
                "appointment_time": float(stop.get("exact_time") or 0.0),
                "window_start": float(stop.get("window_start") or 0.0),
                "window_end": float(stop.get("window_end") or 0.0),
                "service_time_minutes":
                    int(stop.get("service_time_minutes") or 15),
            }
            # Only consult the planner when a constraint actually exists:
            # an appointment/window, or facility hours for the arrival
            # weekday. A flexible stop with no hours data is NOT a
            # "cannot meet" — it services on arrival.
            wd_key = str(arrival.astimezone(
                __import__("pytz").timezone(tz_name)).weekday())
            has_hours = bool(isinstance(
                snap.get(wd_key), list) and snap.get(wd_key))
            constrained = (plan_stop["timing_type"] in (
                "exact_appointment", "time_window") or has_hours)
            if constrained:
                ok, waiting, service_start, departure = \
                    planner.arrival_plan(plan_stop, arrival)
            else:
                ok, waiting, service_start, departure = \
                    True, 0.0, arrival, arrival
            svc_min = plan_stop["service_time_minutes"]
            # The planner returns departure=arrival on a missed window —
            # the truck still services the stop once on site.
            if not ok:
                service_start = departure = arrival
            departure = service_start + datetime.timedelta(
                minutes=svc_min)
            note = ""
            if waiting > 0:
                opens = None
                wd = arrival.astimezone(
                    pytz.timezone(tz_name)).weekday()
                day_hours = snap.get(str(wd))
                if isinstance(day_hours, list) and day_hours:
                    opens = "%02d:%02d" % (
                        int(day_hours[0]), int((day_hours[0] % 1) * 60))
                note = "Waits %.0f min for the facility %s." % (
                    waiting,
                    "opening at %s" % opens if opens
                    else "appointment window")
            elif not ok:
                note = ("Cannot meet the requested %s — earliest service "
                        "would be %s." % (
                            window_kind, service_start.strftime("%H:%M")))

            kind = str(stop.get("type") or "delivery").lower()
            qty_p = int(stop.get("pallets") or 0)
            qty_c = int(stop.get("cases") or 0)
            if kind in ("pickup", "origin"):
                onboard_pallets += qty_p
                onboard_cases += qty_c
            else:
                onboard_pallets = max(0, onboard_pallets - qty_p)
                onboard_cases = max(0, onboard_cases - qty_c)
            rows.append({
                "seq": i + 1,
                "stop_type": "pickup" if kind in ("pickup", "origin")
                else "delivery",
                "name": stop.get("company_name") or "",
                "address": stop.get("address") or "",
                "travel_min": round(travel_min),
                "arrival": arrival.strftime("%Y-%m-%d %H:%M"),
                "wait_min": round(waiting),
                "service_min": svc_min,
                "departure": departure.strftime("%Y-%m-%d %H:%M"),
                "onboard_pallets": onboard_pallets,
                "onboard_cases": onboard_cases,
                "note": note,
            })
            current = departure

        commitments = []
        available_from = busy_until = False
        try:
            sched = self.availability.get_truck_day_schedule(ctx["d0"])
            for t in sched:
                if t.get("truck_id") == vehicle.id:
                    commitments = [{
                        "job": j.get("job_name"),
                        "pickup": str(j.get("pickup_time") or "")[:16],
                        "eta_done": str(j.get("eta_done") or "")[:16],
                        "pallets": j.get("pallets") or 0,
                    } for j in (t.get("jobs") or [])]
                    available_from = str(t.get("available_from") or "")[:16]
                    busy_until = str(t.get("busy_until") or "")[:16]
                    break
        except Exception:
            pass
        return {
            "start_at": start.strftime("%Y-%m-%d %H:%M"),
            "stops": rows,
            "truck_commitments": commitments,
            "available_from": available_from,
            "busy_until": busy_until,
            "notes": notes,
            "source": ("Planned bookings and saved-location settings — "
                       "existing work is never moved or altered."),
        }

    def _card_scheduled_corridor(self, ctx):
        first_pickup = next((s for s in ctx["stops"]
                             if s.get("type") == "pickup"), ctx["stops"][0])
        last_delivery = next((s for s in reversed(ctx["stops"])
                              if s.get("type") == "delivery"), ctx["stops"][-1])

        pickup_fsa = delivery_fsa = None
        fsa_problems = []
        for role, stop in (("pickup", first_pickup),
                           ("delivery", last_delivery)):
            code = str(stop.get("fsa_code") or "").strip().upper()
            if not code:
                fsa_problems.append(
                    "No postal code was extracted for the %s stop — the "
                    "scheduled network option needs origin and destination "
                    "postal/FSA codes." % role)
                continue
            fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(code)
            if not fsa:
                fsa_problems.append(
                    "Postal prefix %s is not in the FSA directory." % code)
                continue
            if role == "pickup":
                pickup_fsa = fsa
            else:
                delivery_fsa = fsa

        base = {
            "key": "scheduled_ltl",
            "title": "Scheduled LTL / weekly corridor",
            "badge": "infeasible", "feasible": False,
            "truck": "Network truck (assigned by the corridor schedule)",
            "pickup_date": False, "delivery_date": False,
            "date_requested": (self._iso(ctx["d0"])
                               if ctx.get("d0") else False),
            "distance_km": False, "drive_hrs": False,
            "cost": False, "cost_source": "network_list_price",
            "suggested_sell": False,
            "markup_pct_on_cost": False, "gross_margin_pct": False,
            "profit": False, "incremental_km": 0.0,
            "incremental_note": "Shares an already-scheduled corridor run.",
            "capacity": dict(ctx["capacity"]),
            "assumptions": [], "warnings": list(ctx["warnings"]),
            "blocking": [], "confidence": CONFIDENCE_MEDIUM,
            "alternatives": [], "availability": {"status": "n/a",
                                                 "conflicts": []},
        }
        if fsa_problems:
            base["blocking"] = fsa_problems
            base["warnings"].append(
                "Until complete verified origin/destination addresses are "
                "available the corridor option stays blocked (city/postal-"
                "area estimates are allowed on the dedicated cards only).")
            return base
        if not pickup_fsa.pickup_supported:
            base["blocking"].append("Pickup FSA does not support pickups.")
            return base
        if not ctx["pallets"] and not ctx["weight_lbs"] and not ctx.get("cases"):
            base["blocking"].append(
                "No pallet/weight quantities are known — the network cannot "
                "quote or reserve space without a shipment size.")
            return base

        # §4 — evaluate EVERY destination in a multi-drop request; the
        # network option is only presented when a single departure (or a
        # clearly explained split) serves all of them.
        from .pricing_service import PricingService
        pricing = PricingService(self.env)
        start_day = ctx["d0"] or datetime.date.today() + datetime.timedelta(days=1)
        delivery_targets = []
        for s in ctx["stops"]:
            if str(s.get("type") or "").lower() not in ("delivery", "dropoff"):
                continue
            code = str(s.get("fsa_code") or "").strip().upper()
            target_fsa = None
            if code:
                target_fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(code)
            delivery_targets.append({
                "name": s.get("company_name") or s.get("address") or
                        "Delivery stop",
                "pallets": int(s.get("pallets") or 0),
                "weight_lbs": float(s.get("weight_lbs") or 0.0),
                "fsa": target_fsa if target_fsa else False,
                "fsa_code": code or False,
            })
        if not delivery_targets:
            delivery_targets = [{"name": "Delivery stop", "pallets": 0,
                                 "weight_lbs": 0.0, "fsa": delivery_fsa,
                                 "fsa_code": ""}]

        found = []
        per_destination = []
        last_reason = "no_scheduled_departure_in_window"
        day = start_day
        # Scan the window once per destination and merge by pickup date.
        dest_by_date = {}
        all_served_reason = []
        for dt in delivery_targets:
            d_found = []
            d_reason = last_reason
            d_day = start_day
            for _ in range(MAX_NETWORK_SCAN_DAYS):
                if not dt["fsa"]:
                    d_reason = "no_postal_for_destination"
                    break
                if not dt["fsa"].delivery_supported:
                    d_reason = "fsa_does_not_support_deliveries"
                    break
                result = pricing.calculate(
                    pickup_fsa, dt["fsa"], "ltl", ctx["equipment"],
                    max(dt["pallets"] or ctx["pallets"] or 1, 1),
                    dt["weight_lbs"] or ctx["weight_lbs"],
                    required_temperature_c=ctx.get("required_temperature_c"),
                    resolve_departures=True, reference_dt=d_day)
                if not result.available:
                    d_reason = getattr(result, "reason", None) or d_reason
                    d_day += datetime.timedelta(days=1)
                    continue
                d_found.append({
                    "date": (result.pickup_date.isoformat()
                             if result.pickup_date else d_day.isoformat()),
                    "delivery_date":
                        (result.delivery_date_estimate.isoformat()
                         if result.delivery_date_estimate else ""),
                    "price": _money(result.calculated_price),
                    "corridor": (result.corridor.name
                                 if result.corridor else ""),
                    "routing": ("hub transfer"
                                if (result.route_snapshot or {})
                                .get("leg_count", 1) > 1 else "direct"),
                })
                if result.pickup_date:
                    d_day = result.pickup_date + datetime.timedelta(days=1)
                break
            if not d_found:
                all_served_reason.append(
                    "%s: %s" % (dt["name"], d_reason or "no_departure"))
            per_destination.append({
                "name": dt["name"],
                "pallets": dt["pallets"],
                "fsa_code": dt["fsa_code"] or False,
                "served_date": d_found[0]["date"] if d_found else False,
                "delivery_date": d_found[0]["delivery_date"]
                if d_found else False,
                "corridor": d_found[0]["corridor"] if d_found else False,
                "reason": (False if d_found else
                           d_reason or "no_departure"),
            })

        if all(not d["served_date"] for d in per_destination):
            base["badge"] = "infeasible"
            base["blocking"] = [
                "No scheduled corridor departure serves any destination "
                "within %d days: %s. The dedicated cards above remain the "
                "available options."
                % (MAX_NETWORK_SCAN_DAYS,
                   "; ".join(all_served_reason[:4]) or "no_departure")]
            base["per_destination"] = per_destination
            return base

        # Combined option: price the full load against the pickup FSA and
        # the LAST destination (network quote authority) but show every
        # destination's own service day/window.
        combined = []
        day = start_day
        for _ in range(MAX_NETWORK_SCAN_DAYS):
            result = pricing.calculate(
                pickup_fsa, delivery_fsa, "ltl", ctx["equipment"],
                max(ctx["pallets"], 1), ctx["weight_lbs"],
                required_temperature_c=ctx.get("required_temperature_c"),
                resolve_departures=True, reference_dt=day)
            if not result.available:
                last_reason = getattr(result, "reason", None) or last_reason
                day += datetime.timedelta(days=1)
                continue
            dep_ids = [leg.get("departure_id") for leg in
                       (result.route_snapshot or {}).get("legs") or []
                       if leg.get("departure_id")]
            free = self._free_pallets_on_departures(dep_ids)
            capacity_note = ""
            if free is not False and free < max(ctx["pallets"], 1):
                capacity_note = (
                    "The found departure shows only %d free positions — "
                    "verify capacity at booking time." % free)
            combined.append({
                "date": result.pickup_date.isoformat()
                if result.pickup_date else day.isoformat(),
                "delivery_date": (result.delivery_date_estimate.isoformat()
                                  if result.delivery_date_estimate else ""),
                "price": _money(result.calculated_price),
                "corridor": result.corridor.name if result.corridor else "",
                "corridor_id": result.corridor.id
                if result.corridor else False,
                "free_pallets": free,
                "routing": ("hub transfer" if (result.route_snapshot or {})
                            .get("leg_count", 1) > 1 else "direct"),
                "ftl_priced": bool((result.route_snapshot or {})
                                   .get("ftl_priced")),
                "note": capacity_note,
            })
            if len(combined) >= MAX_ALT_NETWORK_DATES:
                break
            if result.pickup_date:
                day = result.pickup_date + datetime.timedelta(days=1)

        if not combined:
            base["badge"] = "infeasible"
            base["blocking"] = [
                "No scheduled corridor departure fits within %d days "
                "(last resolver reason: %s). The dedicated cards above are "
                "the available options for this timing."
                % (MAX_NETWORK_SCAN_DAYS, last_reason or "no_departure")]
            base["per_destination"] = per_destination
            return base

        best = combined[0]
        unserved = [d for d in per_destination if not d["served_date"]]
        if unserved:
            base["warnings"].append(
                "No single network service covers every destination on "
                "one departure: %s. Each listed destination row shows its "
                "own next available service — the split must be confirmed "
                "at booking."
                % "; ".join("%s (%s)" % (d["name"], d["reason"])
                            for d in unserved[:4]))
        base.update({
            "badge": "feasible", "feasible": True,
            "pickup_date": best["date"],
            "delivery_date": best["delivery_date"],
            "suggested_sell": best["price"],
            "corridor": best["corridor"],
            "alternatives": combined[1:],
            "per_destination": per_destination,
            "capacity": dict(ctx["capacity"], note=(
                "Corridor capacity is validated by the departure resolver "
                "against the assigned truck; live free positions are shown "
                "per found date.")),
            "assumptions": [
                "Corridor list pricing from PricingService (the booking "
                "pricing authority).",
                "Operating cost and margin for a network run depend on the "
                "vehicle the corridor schedule assigns — execution-scenario "
                "costing runs at booking conversion, not in this preview.",
                "Capacity is validated per scheduled departure on each "
                "found day (resolver against the assigned truck).",
            ],
        })
        if best.get("ftl_priced"):
            base["warnings"].append(
                "This corridor auto-prices the load as Full Truckload at "
                "the configured threshold — the date shown is FTL-priced.")
        if best.get("note"):
            base["warnings"].append(best["note"])
        return base

    # ── Shared helpers ──────────────────────────────────────────────

    def _cost_for(self, vehicle, distance_km, duration_hrs, weight_lbs,
                  overrides):
        """Own-fleet cost authority — returns (breakdown_dict, error) or
        (False, reason) when the estimator cannot price the move.

        The breakdown reconciles EXACTLY with the displayed total: the
        total shown on the card is the sum of the displayed component
        amounts (each component is the engine's rounded value, so no
        rounding drift between what the user sees and what was charged)."""
        try:
            from odoo.addons.premafirm_ai_engine.services.pricing_engine \
                import PricingEngine
            costs = PricingEngine(self.env).calculate(
                vehicle.id, max(distance_km or 0.0, 0.0),
                max(duration_hrs or 0.0, 0.0),
                overrides=overrides or None,
                load_weight_lbs=weight_lbs or 0.0)
            total = costs.get("total_cost") or 0.0
            if total <= 0:
                return False, "estimator returned a zero cost"
            components = [{
                "label": "Fuel",
                "qty": "%s L" % _num(costs.get("fuel_liters")),
                "rate": "$%s/L" % _num(costs.get("fuel_price_per_l"), 4),
                "amount": _r(costs.get("fuel_cost") or 0.0, 2),
                "note": ("%.1f km at %s km/L effective (load factor %s)"
                         % (max(distance_km or 0.0, 0.0),
                            _num(costs.get("effective_km_per_l"), 2),
                            _num(costs.get("fuel_load_factor"), 3))),
            }, {
                "label": "Maintenance",
                "qty": "%.1f km" % max(distance_km or 0.0, 0.0),
                "rate": "$%s/km" % _num(costs.get("maintenance_per_km"), 4),
                "amount": _r(costs.get("maintenance_cost") or 0.0, 2),
                "note": "Vehicle costing tab per-km rate",
            }, {
                "label": "Insurance / allocated fixed costs",
                "qty": "%.1f km" % max(distance_km or 0.0, 0.0),
                "rate": "$%s/km" % _num(costs.get("insurance_per_km"), 4),
                "amount": _r(costs.get("insurance_cost") or 0.0, 2),
                "note": ("monthly budget %s"
                         % _money(costs.get("insurance_budget") or 0.0)
                         if (costs.get("insurance_budget") or 0.0)
                         else "Vehicle costing tab per-km rate"),
            }, {
                "label": "Driver / labour",
                "qty": "%.1f h" % max(duration_hrs or 0.0, 0.0),
                "rate": "$%s/h" % _num(costs.get("driver_rate_per_hr"), 2),
                "amount": _r(costs.get("driver_cost") or 0.0, 2),
                "note": "Drive + service duration",
            }]
            if (costs.get("weight_surcharge") or 0.0) > 0:
                components.append({
                    "label": "Weight surcharge",
                    "qty": "%.0f lb over %s lb threshold" % (
                        max(weight_lbs or 0.0, 0.0),
                        costs.get("weight_threshold_lbs") or 0),
                    "rate": "$%s/cwt" % _num(
                        costs.get("weight_surcharge_per_cwt"), 2),
                    "amount": _r(costs.get("weight_surcharge") or 0.0, 2),
                    "note": "Configured heavy-load surcharge",
                })
            reconciled = _r(sum(float(c.get("amount") or 0.0)
                                for c in components), 2)
            return {
                "total": reconciled,
                "components": components,
                "engine_total": _r(total, 2),
                "margin_pct": costs.get("margin_pct") or 0.0,
                "suggested_rate": costs.get("suggested_rate"),
            }, False
        except Exception as exc:
            return False, str(exc)[:160]

    def _simulate_days(self, ctx, total_hrs, round_trip=False):
        """Date-walk simulation from the truck workday start (§10.6)."""
        from .estimator_availability_service import local_datetime
        limits = self.availability.hos_limits()
        start_hour = limits["workday_start_hour"]
        day = ctx["d0"]
        pickup_date = day
        delivery_date = day
        remaining = float(total_hrs or 0.0)
        hours_today = 0.0
        drive_budget = limits["max_drive_hours_per_day"]
        while remaining > 0.001:
            take = min(remaining, drive_budget - hours_today)
            if take <= 0:
                day += datetime.timedelta(days=1)
                hours_today = 0.0
                take = min(remaining, drive_budget)
            remaining -= take
            hours_today += take
            delivery_date = day
        return_date = delivery_date if round_trip else False
        interval_start = local_datetime(pickup_date, start_hour, self.tz)
        interval_end = interval_start + datetime.timedelta(
            hours=max(total_hrs or 8.0, 8.0) + 2.0)
        return {
            "pickup_date": pickup_date, "delivery_date": delivery_date,
            "return_date": return_date,
            "interval_start": interval_start, "interval_end": interval_end,
        }

    def _free_pallets_on_departures(self, departure_ids):
        """Residual positions on the found departure(s), via CapacityEngine
        peaks exactly as the booking pipeline counts them."""
        if not departure_ids:
            return False
        from .capacity_engine import CapacityEngine
        engine = CapacityEngine(self.env)
        free_list = []
        for dep_id in departure_ids:
            departure = self.env["logistics.corridor.departure"].browse(dep_id)
            if not departure.exists() or not departure.vehicle_id:
                return False
            peak = engine.compute_departure_peak(departure)
            free_list.append(max(
                0, int(departure.vehicle_id.straight_pallet_capacity or 12)
                - peak.get("peak_pallets", 0)))
        return min(free_list) if free_list else False

    def _resolve_start_date(self, vehicle, requested):
        """Requested date, or the earliest fully-free day (advisory)."""
        warnings = []
        today = datetime.date.today()
        if requested:
            return requested, warnings
        free = self.availability.first_free_date(
            vehicle.id, today + datetime.timedelta(days=1))
        if not free:
            free = today + datetime.timedelta(days=1)
            warnings.append(
                "No free day was found in the 21-day scan — tomorrow is "
                "shown with its conflicts listed.")
        else:
            warnings.append(
                "No pickup date was requested — the earliest free day for "
                "this truck is shown.")
        return free, warnings

    @staticmethod
    def _as_date(value):
        if not value:
            return False
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value)[:10])
        except ValueError:
            return False

    @staticmethod
    def _iso(value):
        return value.isoformat() if value else False

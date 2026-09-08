"""Estimator bridge — the Dispatch-side authority surface the Prema AI
estimator calls (master §1 cross-repo contract, §9-13).

Prema Dispatch owns pricing/availability/capacity/scheduling authority;
Prema AI extracts, drafts and advises.  This AbstractModel is the ONLY
entry point the AI side uses for scenario, availability, pricing-intel,
pairing and route-development data.  Everything it returns is read-only
advisory — creating quotes, bookings or plan changes stays behind explicit
access-controlled actions on the Dispatch side.

Rules:
  - Every public method returns a dict and never raises: failures are
    {"error": code, "message": ...} so the AI side can show the reason.
  - Engine identity checks: the engine only calls this when
    'logistics.estimator.bridge' is in its registry (guarded env lookup).
"""

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class LogisticsEstimatorBridge(models.AbstractModel):
    _name = "logistics.estimator.bridge"
    _description = "Prema AI Estimator Bridge (dispatch authority)"

    # ── Scenario cards (§10) + intel (§12) + pairing (§13) ──────────

    def estimate_request(self, payload):
        """One canonical estimator response: the three scenario cards with
        the customer-intel and pairing evidence attached.  Read-only."""
        try:
            payload = payload or {}
            from ..services.estimator_scenario_service import \
                EstimatorScenarioService
            result = EstimatorScenarioService(self.env).build_cards(payload)

            intel = {}
            partner_id = int(payload.get("partner_id") or 0) or False
            if partner_id:
                from ..services.customer_pricing_intel_service import \
                    CustomerPricingIntelService
                intel = CustomerPricingIntelService(self.env).intel_report(
                    partner_id, payload)

            pairing_payload = dict(payload)
            pairing_payload["pickup_date"] = \
                (result.get("d0") or payload.get("pickup_date")
                 or False)
            from ..services.load_pairing_service import LoadPairingService
            pairing = LoadPairingService(self.env).pairing_report(
                pairing_payload)

            return {
                "ok": True,
                "scenarios": result.get("scenarios", []),
                "fatal": result.get("fatal", False),
                "message": result.get("message", ""),
                "warnings": result.get("warnings", []),
                "capacity": result.get("capacity", {}),
                "d0": result.get("d0"),
                "requested_date": result.get("requested_date"),
                "intel": intel,
                "pairing": pairing,
            }
        except Exception as exc:
            _logger.exception("estimator bridge estimate_request failed")
            return {"error": "estimate_request_failed",
                    "message": str(exc)[:300]}

    # ── Saved Location matching (MP2 text-first workflow) ───────────

    def location_match_or_create(self, company_name, address, city="",
                                 province="", postal_code="", lat=0.0,
                                 lng=0.0, place_id=""):
        """Match a normalized extracted address to an existing Saved
        Location, or create ONE Pending Review location when complete and
        unmatched. Never verified, never communicates, never quotes or
        books — review-only."""
        try:
            from ..services.estimator_location_service import \
                EstimatorLocationService
            loc, status = EstimatorLocationService(self.env).match_or_create(
                company_name, address, city, province, postal_code,
                float(lat or 0.0), float(lng or 0.0), place_id or "")
            extras = {}
            if loc:
                try:
                    Hours = self.env["prema.dispatch.location.hours"].sudo()
                    snap = {}
                    for h in Hours.search([("facility_id", "=", loc.id)]):
                        snap[str(h.day_of_week)] = [h.open_time or 0.0,
                                                    h.close_time or 0.0]
                    svc_pickup = svc_delivery = 15
                    if hasattr(loc, "planning_service_time_minutes"):
                        try:
                            svc_pickup = max(1, int(
                                loc.planning_service_time_minutes(
                                    stop_type="pickup") or 15))
                            svc_delivery = max(1, int(
                                loc.planning_service_time_minutes(
                                    stop_type="delivery") or 15))
                        except Exception:
                            pass
                    extras = {
                        "operating_hours_snapshot": snap,
                        "service_time_minutes_pickup": svc_pickup,
                        "service_time_minutes_delivery": svc_delivery,
                        "per_pallet_service_minutes": int(
                            loc.per_pallet_service_minutes or 0)
                        if "per_pallet_service_minutes" in loc._fields
                        else 0,
                        "tz_name": "America/Toronto",
                    }
                except Exception:
                    _logger.exception(
                        "estimator location extras failed for %s",
                        loc.id if loc else "?")
                    extras = {}
            return {
                "saved_location_id": loc.id if loc else False,
                "status": status,
                "name": loc.name if loc else "",
                "address": loc.address if loc else "",
                "city": loc.city if loc else "",
                "province_code": loc.province_code if loc else "",
                "postal_code": loc.postal_code if loc else "",
                "verification_state": loc.verification_state if loc
                else "",
                **extras,
            }
        except Exception as exc:
            _logger.exception("estimator location_match_or_create failed")
            return {"saved_location_id": False, "status": "incomplete",
                    "name": "", "error": str(exc)[:200]}

    def location_search_rpc(self, term):
        """Free-text Saved Location search for the panel stop combobox."""
        try:
            from ..services.estimator_location_service import \
                EstimatorLocationService
            return EstimatorLocationService(self.env).search(
                term or "", limit=8)
        except Exception as exc:
            _logger.exception("estimator location_search_rpc failed")
            return []

    def transfer_estimator_scenario_rpc(self, request_id, scenario_key=None):
        """§10 — populate the lead's draft Rate Confirmation from the
        estimator's SELECTED scenario: stops, quantities, windows,
        instructions and the suggested sell ride onto the draft (empty
        slots only — never clobber human work). Nothing is sent,
        confirmed, invoiced or booked here; the booking step stays
        unavailable until acceptance is recorded and capacity re-checks
        at conversion."""
        Request = self.env["premafirm.estimator.scenario.request"].sudo()
        request = Request.browse(int(request_id))
        if not request.exists():
            return {"error": "Request not found."}
        lead = request.lead_id or (request.partner_id and
                                   request._find_open_lead(
                                       request.partner_id))
        if not lead:
            return {"error": "no_lead",
                    "message": "No open opportunity for this customer."}
        CQ = self.env["logistics.custom.quote"]
        draft = CQ.find_or_create_draft_for_lead(
            lead.id, idempotency_key="estimator-rc:%s" % request.id)
        resp = request.response_json or {}
        inputs = request.inputs_json or {}
        scenario = next((s for s in (resp.get("scenarios") or [])
                         if s.get("key") == scenario_key), None)
        if scenario is None:
            scenario = next((s for s in (resp.get("scenarios") or [])
                             if s.get("feasible")), None)
        stops = resp.get("structured_stops") or []
        pickup = next((s for s in stops
                       if s.get("stop_type") == "pickup"), None)
        delivery = next((s for s in stops
                         if s.get("stop_type") == "delivery"), None)

        def _addr(s):
            if not s:
                return ""
            return " ".join(filter(None, (
                s.get("address") or "", s.get("city") or "",
                s.get("province") or "")))

        stop_lines = []
        for i, s in enumerate(stops, 1):
            parts = ["%d. %s %s" % (i, s.get("stop_type") or "stop",
                                    s.get("company_name") or "")]
            if _addr(s):
                parts.append(_addr(s))
            qty = []
            if s.get("pallets"):
                qty.append("%s plt" % s["pallets"])
            if s.get("cases"):
                qty.append("%s cs" % s["cases"])
            if s.get("weight_lbs"):
                qty.append("%s lb" % s["weight_lbs"])
            if qty:
                parts.append(" · ".join(qty))
            if s.get("stop_date"):
                parts.append("date %s" % s["stop_date"])
            if s.get("time_window_type") == "exact" and s.get("exact_time"):
                parts.append("exact %02d:%02d" % (
                    int(s["exact_time"]),
                    int((s["exact_time"] % 1) * 60)))
            elif s.get("time_window_type") == "window":
                parts.append("window %02d:%02d–%02d:%02d" % (
                    int(s.get("window_start") or 0),
                    int(((s.get("window_start") or 0) % 1) * 60),
                    int(s.get("window_end") or 0),
                    int(((s.get("window_end") or 0) % 1) * 60)))
            if s.get("instructions"):
                parts.append("instructions: %s" % s["instructions"])
            stop_lines.append(" ".join(p for p in parts if p))

        eq = str(inputs.get("equipment") or "dry")
        if eq not in ("dry", "reefer"):
            eq = "dry"
        populate = {}
        if pickup:
            populate["pickup_address"] = _addr(pickup) or False
            populate["pickup_postal_code"] = \
                (pickup.get("postal_code") or "") or False
        if delivery:
            populate["delivery_address"] = _addr(delivery) or False
            populate["delivery_postal_code"] = \
                (delivery.get("postal_code") or "") or False
        # Conversion feeds the booking stops from the quote's RESOLVED
        # FSA fields (the phone wizard fills them from its own FSA
        # resolution); a free-text postal alone leaves them empty and the
        # departure span check at booking time has no regions. Resolve
        # here so estimator-created drafts convert the same way.
        for resolved_field, stop in (("resolved_fsa_pickup", pickup),
                                     ("resolved_fsa_delivery", delivery)):
            if not stop or draft[resolved_field]:
                continue
            fsa = self.env["logistics.fsa"].sudo().resolve_from_postal(
                stop.get("postal_code") or "")
            if fsa:
                populate[resolved_field] = fsa.fsa
        if inputs.get("pallets"):
            populate["pallets"] = int(inputs["pallets"])
        if inputs.get("weight_lbs"):
            populate["weight_lbs"] = float(inputs["weight_lbs"])
        populate["load_type"] = "ltl"
        populate["temperature_mode"] = eq
        if "required_temperature_c" in inputs and inputs[
                "required_temperature_c"] is not None:
            populate["required_temperature_c"] = \
                float(inputs["required_temperature_c"])
        populate["requested_pickup_date"] = \
            inputs.get("pickup_date") or False

        scenario_line = ""
        sell = False
        if scenario:
            sell = scenario.get("suggested_sell")
            scenario_line = (
                "Selected scenario: %s — suggested sell %s%s."
                % (scenario.get("title") or scenario.get("key") or "?",
                   ("$%.2f" % sell) if sell is not False else "(none)",
                   (" (operating cost $%.2f)"
                    % scenario["cost"])
                   if scenario.get("cost") is not False else ""))

        if draft.state in ("new", "reviewing", "quoted"):
            updates = {}
            for key, value in populate.items():
                if key == "required_temperature_c":
                    if value is None:
                        continue  # 0°C is a VALID reefer setpoint.
                elif value in ("", False, 0.0, None):
                    continue
                cur = draft[key]
                if cur in ("", False, 0.0, None):
                    updates[key] = value
                elif (draft.state == "new" and
                        ((key == "pallets" and cur == 1) or
                         (key == "temperature_mode" and cur == "dry"))):
                    # The quote model defaults (1 pallet, Dry) are not human
                    # content — on a fresh draft the estimator's facts fill
                    # them. Once staff starts reviewing (state left "new")
                    # their edits win and nothing is overwritten.
                    updates[key] = value
            notes = "\n".join(filter(None, (
                "Prepared from the Prema Estimator (request %s). %s"
                % (request.name or request.id, scenario_line),
                "Stop-by-stop (reviewed):",
                *stop_lines,
                "Draft only: nothing was priced, sent or booked.")))
            if not draft.notes:
                updates["notes"] = notes
            if sell is not False and not draft.quoted_price:
                # The sell-price audit gate demands a Manual Price Reason
                # whenever the quoted price differs from the system price —
                # the estimator's suggested sell is a recommendation, so its
                # provenance is recorded in the same write.
                updates.update({
                    "quoted_price": float(sell),
                    "manual_price_reason":
                        "Estimator suggested sell (scenario %s, request %s)"
                        % (scenario.get("key") or "?",
                           request.name or request.id)})
            if updates:
                draft.write(updates)
                # Audit trail only when this call actually changed the
                # draft — re-transfer calls with nothing to fill stay quiet.
                draft.message_post(
                    body=("<p>%s</p>" % "<br/>".join(
                        ("<p>%s</p>" % n) for n in notes.split("\n"))),
                    subtype_xmlid="mail.mt_note")

        # The booking step stays unavailable until acceptance is recorded
        # and the internal GO is given; conversion itself rechecks
        # capacity/schedule via the booking pipeline.
        workflow = [
            {"step": "review",
             "label": "Staff reviews the draft",
             "done": draft.state in ("reviewing", "quoted", "accepted",
                                     "converted")},
            {"step": "send",
             "label": "Staff explicitly sends the Rate Confirmation",
             "done": bool(draft.is_locked)},
            {"step": "acceptance",
             "label": "Customer approval recorded against the reviewed "
                      "version",
             "done": bool(draft.acceptance_recorded_at)},
            {"step": "booking",
             "label": "Staff confirms the booking (capacity and schedule "
                      "re-checked at conversion)",
             "done": draft.state == "converted",
             "available": bool(draft.acceptance_recorded_at)},
        ]
        return {
            "quote_id": draft.id, "quote_name": draft.name,
            "state": draft.state, "workflow": workflow,
            "action": {
                "type": "ir.actions.act_window",
                "res_model": "logistics.custom.quote",
                "res_id": draft.id,
                "views": [[False, "form"]],
                "target": "current",
            },
        }

    # ── Route development week view (§13) ───────────────────────────

    def route_development(self, vehicle_id=0, week_start=None,
                          fsa_codes=None):
        """Scheduled corridor week for the truck + the request's region
        pair, development gaps, and region city context for AI lead
        matching.  Read-only advisory."""
        try:
            from ..services.route_development_service import \
                RouteDevelopmentService
            return RouteDevelopmentService(self.env).route_dev_report(
                vehicle_id, week_start, fsa_codes or [])
        except Exception as exc:
            _logger.exception("estimator bridge route_development failed")
            return {"error": "route_development_failed",
                    "message": str(exc)[:300]}

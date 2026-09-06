"""§15 daily multi-booking trip optimizer — persisted proposal (D-B4).

prema.dispatch.day.route.proposal — one PROPOSED stop sequence for one
truck / operating day, produced by DayRouteService (proposal-only; nothing
is written to jobs/stops at generation). Applying is an explicit dispatcher
action:

  * guarded by a sha1 fingerprint of the whole day scope, recomputed at
    apply time — any drift (window/load/status/order changes, added or
    removed stops) refuses the apply with a clear message;
  * applied exactly once (state proposed → applied, version bump);
  * audited end-to-end on prema.dispatch.day.route.event (same pattern as
    the load-plan event audit).

A day whose scope changed after the proposal was generated is marked
stale by write hooks on the dispatch stop / dispatch job, so a stale
proposal is never silently re-applied. Creating a new proposal for the
same truck/day supersedes the previous open one.
"""
import json

from odoo import api, fields, models
from odoo.exceptions import UserError

PROPOSAL_EVENT_TYPES = [
    ("proposal_created", "Proposal Created"),
    ("proposal_applied", "Proposal Applied"),
    ("proposal_cancelled", "Proposal Cancelled"),
    ("proposal_superseded", "Proposal Superseded"),
    ("marked_stale", "Marked Stale"),
    ("manual_reorder", "Board Reorder"),
    ("reorder_refused", "Reorder Refused"),
]

STALE_REASON_DEFAULT = "The day changed after this proposal was generated."


class PremaDispatchDayRouteProposal(models.Model):
    _name = "prema.dispatch.day.route.proposal"
    _description = "Day Trip Route Proposal"
    _order = "operating_date desc, id desc"

    name = fields.Char(compute="_compute_name", string="Proposal")
    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Truck", required=True, index=True,
        readonly=True)
    operating_date = fields.Date(
        string="Operating Day", required=True, index=True, readonly=True)
    state = fields.Selection([
        ("proposed", "Proposed"),
        ("applied", "Applied"),
        ("cancelled", "Cancelled"),
    ], default="proposed", required=True, index=True)
    is_stale = fields.Boolean(string="Stale", default=False, index=True)
    stale_reason = fields.Text(string="Stale Reason", readonly=True)
    stale_since = fields.Datetime(readonly=True)
    stale_triggered_by = fields.Many2one("res.users", readonly=True)

    version = fields.Integer(default=1, readonly=True)
    feasible = fields.Boolean(default=True, readonly=True)
    reason = fields.Char(readonly=True)
    conflicts_json = fields.Text(readonly=True)
    capacity_timeline_json = fields.Text(readonly=True)

    start_dt = fields.Datetime(readonly=True)
    finish_eta = fields.Datetime(readonly=True)
    vehicle_max_pallets = fields.Integer(readonly=True)
    onboard_at_start = fields.Integer(readonly=True)
    peak_onboard = fields.Integer(readonly=True)
    total_distance_km = fields.Float(readonly=True)
    total_drive_minutes = fields.Integer(readonly=True)
    total_waiting_minutes = fields.Integer(readonly=True)

    fingerprint = fields.Char(readonly=True, copy=False)
    created_by = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True)
    created_at = fields.Datetime(
        default=fields.Datetime.now, readonly=True)
    applied_by = fields.Many2one("res.users", readonly=True)
    applied_at = fields.Datetime(readonly=True)
    cancelled_by = fields.Many2one("res.users", readonly=True)
    cancelled_at = fields.Datetime(readonly=True)

    total_stops = fields.Integer(
        compute="_compute_total_stops", string="Stops")

    line_ids = fields.One2many(
        "prema.dispatch.day.route.proposal.line", "proposal_id",
        string="Stops", readonly=True)
    event_ids = fields.One2many(
        "prema.dispatch.day.route.event", "proposal_id",
        string="Audit Events", readonly=True)

    # No SQL unique (vehicle, date): applied/cancelled history must be kept,
    # so a later proposal for the same truck/day may always be created. The
    # OPEN set (state=proposed, not stale) is kept to exactly one per
    # truck/day in generate() — creating a new one supersedes the open one.

    @api.depends("line_ids")
    def _compute_total_stops(self):
        for p in self:
            p.total_stops = len(p.line_ids)

    @api.depends("vehicle_id", "operating_date")
    def _compute_name(self):
        for p in self:
            p.name = "DRP-%s-%s" % (
                p.vehicle_id.name or p.vehicle_id.id,
                p.operating_date or "?")

    # ── Staff guard ─────────────────────────────────────────────────

    def _check_dispatch_staff_or_raise(self, require_manager=False):
        """Dispatchers, dispatch managers, booking managers and system
        users may create/review/apply proposals; cancelling is manager-
        level only (the review list stays tamper-light)."""
        user = self.env.user
        groups = ("prema_dispatch.group_dispatcher",
                  "prema_dispatch.group_dispatch_manager",
                  "prema_logistics_booking.group_logistics_booking_manager",
                  "base.group_system")
        if require_manager:
            groups = ("prema_dispatch.group_dispatch_manager",
                      "prema_logistics_booking.group_logistics_booking_manager",
                      "base.group_system")
        if not any(user.has_group(g) for g in groups):
            raise UserError(
                "Not authorized — dispatcher or booking-manager access "
                "required for day route proposals.")

    # ── Lifecycle ───────────────────────────────────────────────────

    @api.model
    def generate(self, vehicle_id, operating_date):
        """Create a proposal for truck/day (proposal-only). Any previous
        OPEN proposal for the same truck/day is superseded (marked stale),
        so the open set is always exactly one per truck/day."""
        self._check_dispatch_staff_or_raise()
        from odoo.addons.prema_logistics_booking.services.day_route_service import (
            DayRouteService)
        svc = DayRouteService(self.env)
        payload = svc.build_proposal_payload(vehicle_id, operating_date)
        older = self.search([
            ("vehicle_id", "=", payload["vehicle_id"]),
            ("operating_date", "=", payload["operating_date"]),
            ("state", "=", "proposed"),
            ("is_stale", "=", False),
        ])
        for old in older:
            old._log_event("proposal_superseded",
                           reason="Superseded by a newer proposal for the "
                                  "same truck/day.")
            old.write({
                "is_stale": True,
                "stale_reason": "Superseded by a newer proposal for the "
                                "same truck/day.",
                "stale_since": fields.Datetime.now(),
                "stale_triggered_by": self.env.user.id,
            })
        proposal = self.create(payload)
        proposal._log_event(
            "proposal_created",
            reason="Proposal generated for %s on %s." % (
                proposal.vehicle_id.name, proposal.operating_date))
        return proposal

    def action_apply(self):
        """UI button wrapper (an object button must not return the apply
        dict — callers wanting the result use apply())."""
        self.ensure_one()
        self.apply()

    def apply(self):
        """Apply the proposed order EXACTLY once. Idempotence: a second
        call (or a call on any non-proposed record) is refused. A stale or
        drifted proposal is never silently re-applied — the dispatcher must
        regenerate."""
        self.ensure_one()
        self._check_dispatch_staff_or_raise()
        if self.state == "applied":
            raise UserError("This proposal was already applied on %s by %s."
                            % (self.applied_at, self.applied_by.name
                               if self.applied_by else "?"))
        if self.state != "proposed":
            raise UserError("Only a Proposed proposal can be applied "
                            "(state: %s)." % self.state)
        if self.is_stale:
            raise UserError(
                "This proposal is STALE and cannot be applied.\n%s"
                % (self.stale_reason or STALE_REASON_DEFAULT))
        if not self.feasible:
            raise UserError(
                "This proposal is marked infeasible and cannot be applied.\n"
                "Reason: %s" % (self.reason or "no feasible sequence"))

        from odoo.addons.prema_logistics_booking.services.day_route_service import (
            DayRouteService, DONE_STATUSES, PINNED_STATUSES)
        svc = DayRouteService(self.env)
        # 1) Fingerprint guard — the day must be exactly as proposed.
        current_fp = svc.compute_fingerprint(
            self.vehicle_id.id, self.operating_date)
        if current_fp != self.fingerprint:
            self._log_event(
                "reorder_refused",
                reason="Apply refused: day drifted since generation "
                       "(fingerprint mismatch).")
            raise UserError(
                "This proposal no longer matches the truck/day — stops, "
                "windows, loads or the order changed after it was "
                "generated. Regenerate the proposal and review it again.")
        # 2) Scope integrity: the persisted lines must still cover the day.
        scope = svc.day_scope(self.vehicle_id.id, self.operating_date)
        scope_ids = set(scope["movable"].ids) | set(scope["pinned"].ids)
        line_stop_ids = set(self.line_ids.mapped("stop_id.id"))
        if scope_ids != line_stop_ids or not self.line_ids:
            raise UserError(
                "The proposal's stop set no longer matches the truck/day. "
                "Regenerate the proposal.")

        # 3) Rebuild the merged order from the persisted lines: pinned
        #    stops keep their entry slot, movable stops follow the
        #    optimized order (this is what was reviewed and approved).
        pinned_by_order = {l.stop_id.id: l for l in self.line_ids
                           if l.pinned}
        movable_by_opt = sorted(
            (l for l in self.line_ids if not l.pinned),
            key=lambda l: l.optimized_order or 0)
        pinned_sorted = sorted(
            self.line_ids.filtered("pinned"),
            key=lambda l: l.entry_order or 0)
        current = scope["movable"] | scope["pinned"]
        # Ordering key: the day's current sequence, exactly as generated.
        merged = []
        move_iter = iter(movable_by_opt)
        for stop in current.sorted(lambda s: (s.sequence or 0, s.id)):
            if stop.id in pinned_by_order:
                merged.append(stop)
            else:
                line = next(move_iter, None)
                if line:
                    merged.append(line.stop_id)
                else:
                    merged.append(stop)

        # 4) Execute: the proposal flips to applied FIRST — the day is
        #    committed to this plan before any stop is touched, and the
        #    stop-write stale hooks then invalidate only OTHER open
        #    proposals, never this one. The stop writes themselves run
        #    under the _day_route_silent escape hatch: this apply IS the
        #    day-route mutation authority (fingerprint + scope guards
        #    already ran), so the hook cascade would only double-report.
        #    Written per stop: sequences flat; ETA advisory fields from
        #    the walked plan; scheduled_time only where nothing set it
        #    yet (schedule authority preserved, parity with Auto Plan
        #    consolidation).
        self.write({
            "state": "applied",
            "applied_by": self.env.user.id,
            "applied_at": fields.Datetime.now(),
            "version": self.version + 1,
        })
        by_stop = {l.stop_id.id: l for l in self.line_ids}
        old_order = [(l.stop_id.id, l.stop_id.sequence)
                     for l in self.line_ids.sorted("entry_order")]
        applied = 0
        for i, stop in enumerate(merged):
            seq = (i + 1) * 10
            vals = {"sequence": seq}
            line = by_stop.get(stop.id)
            if (line and not line.pinned
                    and stop.status not in DONE_STATUSES + PINNED_STATUSES
                    and not stop.route_locked and line.eta):
                eta = line.eta
                vals.update({
                    "travel_arrival_at": eta,
                    "facility_service_start_at":
                        line.service_start_at or eta,
                    "planned_departure_at": line.departure_at or eta,
                    "customer_eta_at": eta,
                })
                if not stop.scheduled_time and eta:
                    vals["scheduled_time"] = eta
            stop.with_context(_day_route_silent=True).write(vals)
            applied += 1
        # Any OTHER open proposal for this day is now stale by definition.
        others = self.search([
            ("id", "!=", self.id),
            ("vehicle_id", "=", self.vehicle_id.id),
            ("operating_date", "=", self.operating_date),
            ("state", "=", "proposed"),
        ])
        for other in others:
            other._log_event(
                "marked_stale",
                reason="Another proposal for this truck/day was applied.")
            other.write({
                "is_stale": True,
                "stale_reason": "Another proposal for this truck/day was "
                                "applied.",
                "stale_since": fields.Datetime.now(),
                "stale_triggered_by": self.env.user.id,
            })
        self._log_event(
            "proposal_applied",
            reason="Applied: %d stops resequenced." % applied,
            old_value=[{"stop_id": sid, "sequence": seq}
                       for sid, seq in old_order],
            new_value=[{"stop_id": s.id, "sequence": s.sequence}
                       for s in merged],
            snapshot={"vehicle_id": self.vehicle_id.id,
                      "operating_date":
                          self.operating_date.isoformat()})
        return {"success": True, "applied": applied}

    def action_cancel(self):
        self.ensure_one()
        self._check_dispatch_staff_or_raise()
        if self.state != "proposed":
            raise UserError("Only a Proposed proposal can be cancelled "
                            "(state: %s)." % self.state)
        self._log_event("proposal_cancelled", reason="Cancelled by user.")
        self.write({
            "state": "cancelled",
            "cancelled_by": self.env.user.id,
            "cancelled_at": fields.Datetime.now(),
            "version": self.version + 1,
        })

    @api.model
    def _mark_stale_for_stops(self, stop_ids, reason):
        """Hook entry: any OPEN proposal containing one of these stops is
        stale (its day changed underneath it). Runs elevated so drivers /
        cron writers can invalidate too; the actor stays the audit owner."""
        actor = self.env.user.id
        proposals = self.sudo().search([
            ("state", "=", "proposed"),
            ("line_ids.stop_id", "in", list(stop_ids)),
        ])
        for p in proposals:
            p._log_event("marked_stale", reason=reason, actor=actor)
            p.sudo().write({
                "is_stale": True,
                "stale_reason": reason,
                "stale_since": fields.Datetime.now(),
                "stale_triggered_by": actor,
            })
        return proposals

    @api.model
    def _mark_stale_for_jobs(self, job_ids, reason):
        """Hook entry: proposals covering any of these jobs' stops."""
        stop_ids = self.env["prema.dispatch.stop"].search(
            [("job_id", "in", list(job_ids))]).ids
        return self._mark_stale_for_stops(stop_ids, reason)

    def _log_event(self, event_type, reason=None, old_value=None,
                   new_value=None, snapshot=None, actor=None):
        self.ensure_one()
        self.env["prema.dispatch.day.route.event"].sudo().create({
            "proposal_id": self.id,
            "vehicle_id": self.vehicle_id.id,
            "operating_date": self.operating_date,
            "event_type": event_type,
            "changed_by": actor or self.env.user.id,
            "reason": reason,
            "old_value_json": json.dumps(old_value, default=str)
            if old_value is not None else False,
            "new_value_json": json.dumps(new_value, default=str)
            if new_value is not None else False,
            "snapshot_json": json.dumps(snapshot, default=str)
            if snapshot is not None else False,
        })

    @api.model
    def _log_day_event(self, vehicle_id, operating_date, event_type,
                       reason=None, old_value=None, new_value=None,
                       snapshot=None, actor=None):
        """Standalone audit row for a truck/day, linked to the most recent
        proposal for that day when one exists (the Planner board's manual
        reorder / refused reorder trail — a day may have no proposal at
        all and the event must still be recorded)."""
        proposal = False
        if vehicle_id and operating_date:
            latest = self.search([
                ("vehicle_id", "=", vehicle_id),
                ("operating_date", "=", operating_date),
            ], order="id desc", limit=1)
            proposal = latest.id if latest else False
        self.env["prema.dispatch.day.route.event"].sudo().create({
            "proposal_id": proposal,
            "vehicle_id": vehicle_id or False,
            "operating_date": operating_date or False,
            "event_type": event_type,
            "changed_by": actor or self.env.user.id,
            "reason": reason,
            "old_value_json": json.dumps(old_value, default=str)
            if old_value is not None else False,
            "new_value_json": json.dumps(new_value, default=str)
            if new_value is not None else False,
            "snapshot_json": json.dumps(snapshot, default=str)
            if snapshot is not None else False,
        })

    @api.model
    def propose_stop_order_for_truck(self, vehicle_id, date_str):
        """No-mutate day proposal RPC (used by the Planner side panel)."""
        return self.generate(vehicle_id, date_str)

    def action_view_events(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Proposal Audit Events",
            "res_model": "prema.dispatch.day.route.event",
            "view_mode": "tree",
            "domain": [("id", "in", self.event_ids.ids)],
            "context": {"create": False, "edit": False},
        }


class PremaDispatchDayRouteProposalLine(models.Model):
    _name = "prema.dispatch.day.route.proposal.line"
    _description = "Day Route Proposal Line"
    _order = "proposal_id, optimized_order, entry_order, id"

    proposal_id = fields.Many2one(
        "prema.dispatch.day.route.proposal", required=True,
        ondelete="cascade", index=True)
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop",
        ondelete="set null", index=True)
    job_id = fields.Many2one("prema.dispatch.job", index=True)
    entry_order = fields.Integer(
        string="Entry Order",
        help="Position of the stop in the day's current order.")
    optimized_order = fields.Integer(
        string="Optimized Order",
        help="Position of the stop in the proposed sequence.")
    pinned = fields.Boolean(
        string="Pinned",
        help="En-route/arrived/route-locked stops keep their slot.")
    eta = fields.Datetime(string="ETA")
    waiting_minutes = fields.Integer(string="Wait (min)")
    service_start_at = fields.Datetime(string="Service Start")
    departure_at = fields.Datetime(string="Departure")
    onboard_before = fields.Integer(string="Onboard Before")
    onboard_after = fields.Integer(string="Onboard After")
    weight_after = fields.Float(string="Weight After (lbs)")
    drive_minutes = fields.Integer(string="Drive (min)")
    distance_km = fields.Float(string="Segment (km)")


class PremaDispatchDayRouteEvent(models.Model):
    _name = "prema.dispatch.day.route.event"
    _description = "Day Route Audit Event"
    _order = "changed_at desc"

    proposal_id = fields.Many2one(
        "prema.dispatch.day.route.proposal", ondelete="set null",
        index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", index=True)
    operating_date = fields.Date(index=True)
    event_type = fields.Selection(
        PROPOSAL_EVENT_TYPES, required=True, index=True)
    changed_by = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True)
    changed_at = fields.Datetime(
        default=fields.Datetime.now, readonly=True)
    reason = fields.Text()
    old_value_json = fields.Text()
    new_value_json = fields.Text()
    snapshot_json = fields.Text()

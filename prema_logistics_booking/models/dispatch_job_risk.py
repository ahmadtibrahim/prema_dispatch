"""Structured, persisted engine-risk explanations (TODO 14).

Every reason an assignment guard can block or warn about for a
(job, candidate truck) evaluation is recorded as one
``prema.dispatch.job.risk`` row with a stable snake_case ``code``, a
severity matching the guard that produced it (guards that BLOCK the
assignment are ``hard`` — red on the Planner board; guards that only warn
are ``soft`` — yellow), and the guard's own human message. UI surfaces
(board cards, job form) therefore never disagree with the assignment
guards: the row set mirrors exactly the conditions the guards check.

Rows are engine-written only (``source='engine'``). The whole set for a
job is atomically replaced at every evaluation (delete old rows, create
the new set), so a job always reflects its LATEST evaluation — the row
set is the deterministic record. Evaluation happens on assignment
attempts (assign_job_to_truck, both the prema_dispatch base path and the
prema_logistics_booking override), on demand via
``evaluate_job_risks_rpc`` / the job-form "Explain Risks" action — and
NEVER as a side effect of a Planner board load (the board payload reads
persisted rows only).

Reason-code catalogue (each mirrors one existing guard — see
services/job_risk_service.py for the exact condition table):

hard  equipment_mismatch       reefer job on a non-reefer truck
hard  liftgate_missing         liftgate job on a truck without one
hard  feasibility_blocked      base assign "not_feasible" verdict
hard  departure_controlled     job bound to a departure whose truck differs
hard  truck_day_blocked        truck/day already held by an ACTIVE corridor
                               departure (job has no fixed-window stops)
hard  window_conflict          same truck/day hold, but the job carries
                               fixed-window appointment stops that cannot
                               flex around the corridor commitment
hard  departure_conflict       truck/day already reserved by another LTL
                               planner operation / corridor departure
soft  capacity_exceeded        pallet/equivalents peak exceeds truck capacity
soft  payload_exceeded         load weight exceeds truck payload
soft  appointment_outside_hours  appointment falls outside the facility's
                               frozen operating-hours snapshot

(HOS: this build has no ELD / driver-log data source, so ``hos_violation``
is intentionally never generated — a dimension with no data never invents
rows.)
"""
from odoo import _, api, fields, models


class PremaDispatchJobRisk(models.Model):
    _name = "prema.dispatch.job.risk"
    _description = "Dispatch Job Engine Risk"
    _order = "event_at desc, id desc"

    job_id = fields.Many2one(
        "prema.dispatch.job", string="Dispatch Job",
        ondelete="cascade", index=True, required=True, readonly=True,
        copy=False)
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking",
        related="job_id.logistics_booking_id", readonly=True,
        help="The job's LTL booking link, when present.")
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop",
        ondelete="set null", index=True, readonly=True, copy=False,
        help="The stop the reason refers to (appointment/hours rows).")
    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Candidate Truck",
        ondelete="set null", index=True, readonly=True, copy=False,
        help="The truck the evaluation was run against. The persisted set "
             "is always the LATEST evaluation's outcome.")
    severity = fields.Selection([
        ("hard", "Hard — Blocks Assignment"),
        ("soft", "Soft — Warning"),
    ], string="Severity", required=True, readonly=True)
    code = fields.Char(
        string="Reason Code", readonly=True, index=True,
        help="Stable snake_case code identifying the guard condition.")
    message = fields.Text(string="Reason", readonly=True)
    event_at = fields.Datetime(
        string="Evaluated At", default=fields.Datetime.now, index=True,
        readonly=True)
    capacity_used_equiv = fields.Float(
        string="Capacity Used (equiv)", digits=(10, 1), readonly=True,
        help="Pallet-equivalents this job + same-day committed truck jobs "
             "would put on the truck (capacity rows).")
    capacity_available_equiv = fields.Float(
        string="Capacity Available (equiv)", digits=(10, 1), readonly=True)
    equipment = fields.Char(string="Equipment", readonly=True)
    temperature_c = fields.Float(string="Temperature (°C)", readonly=True)
    hours_summary = fields.Char(
        string="Hours Summary", readonly=True,
        help="Facility-hours context for appointment_outside_hours rows.")
    hos_summary = fields.Char(
        string="HOS Summary", readonly=True,
        help="Driver-hours context. No HOS data source exists on this "
             "build, so this is never populated yet.")
    source = fields.Selection([
        ("engine", "Engine"),
        ("manual", "Manual"),
    ], string="Source", default="engine", required=True, readonly=True)

    @api.model
    def evaluate_job_risks_rpc(self, job_id, truck_id=None):
        """Staff RPC: (re)evaluate + persist the engine risks for one job.

        ``truck_id`` is the candidate truck (default: the job's current
        truck). Never raises for missing records — returns the payload
        form ``{"risk_reasons": [{severity, code, message}, ...]}`` the
        Planner board consumes.
        """
        job = self.env["prema.dispatch.job"].browse(job_id)
        if not job.exists():
            return {"risk_reasons": [], "error": "Job not found"}
        vehicle = self.env["fleet.vehicle"]
        if truck_id:
            vehicle = vehicle.browse(truck_id)
            if not vehicle.exists():
                return {"risk_reasons": [], "error": "Truck not found"}
        elif job.vehicle_id:
            vehicle = job.vehicle_id
        try:
            rows = job._engine_risk_rows(vehicle)
        except Exception:
            rows = []
        return {"risk_reasons": rows}

    @api.model
    def _risk_reasons_map_for_jobs(self, job_ids):
        """job_id → engine-risk payloads, from PERSISTED rows only.

        Board-load companion: never triggers an evaluation. Deterministic
        ordering per job: hard rows first, then code, then id.
        """
        payload_map = {}
        if not job_ids:
            return payload_map
        rows = self.sudo().search([
            ("job_id", "in", list(job_ids)),
            ("source", "=", "engine"),
        ])
        by_job = {}
        for row in rows:
            by_job.setdefault(row.job_id.id, []).append(row)
        for job_id, job_rows in by_job.items():
            ordered = sorted(
                job_rows,
                key=lambda r: (0 if r.severity == "hard" else 1,
                               r.code or "", r.id))
            payload_map[job_id] = [
                {"severity": r.severity, "code": r.code,
                 "message": r.message}
                for r in ordered
            ]
        return payload_map


class PremaDispatchJobEngineRiskExtension(models.Model):
    _inherit = "prema.dispatch.job"

    risk_ids = fields.One2many(
        "prema.dispatch.job.risk", "job_id", string="Engine Risks",
        readonly=True, copy=False,
        help="Latest engine evaluation rows for this job (assign-guard "
             "reasons, persisted at each evaluation).")
    risk_count = fields.Integer(
        string="Engine Risks", compute="_compute_risk_count")
    risk_hard_count = fields.Integer(
        string="Hard Risks", compute="_compute_risk_count")

    @api.depends("risk_ids")
    def _compute_risk_count(self):
        for job in self:
            risks = self.env["prema.dispatch.job.risk"].sudo().search([
                ("job_id", "=", job.id)])
            job.risk_count = len(risks)
            job.risk_hard_count = len(
                risks.filtered(lambda r: r.severity == "hard"))

    def _engine_risk_rows(self, vehicle, extra_reasons=None, at=None):
        """Persist + return this job's engine-risk payload for a candidate
        truck.

        Replaces the job's prior engine rows atomically (the row set is
        the deterministic record of the LATEST evaluation). Returns the
        payload form ``[{severity, code, message}, ...]`` in deterministic
        order (hard first, then code). ``extra_reasons`` lets a wire point
        pass guard-derived reasons that are not visible in stored state
        yet (e.g. the feasibility verdict computed during the assign
        attempt itself); an extra reason with the same code REPLACES the
        state-derived row so the guard stays authoritative.
        """
        self.ensure_one()
        from odoo.addons.prema_logistics_booking.services.job_risk_service import (
            JobRiskService,
        )
        return JobRiskService(self.env).evaluate_and_persist(
            self, vehicle or self.env["fleet.vehicle"],
            at=at, extra_reasons=extra_reasons)

    def _persisted_risk_payload(self):
        """Read-only payload from the persisted rows (no re-evaluation)."""
        self.ensure_one()
        rows = self.env["prema.dispatch.job.risk"].sudo().search([
            ("job_id", "=", self.id),
            ("source", "=", "engine"),
        ])
        return [
            {"severity": r.severity, "code": r.code, "message": r.message}
            for r in sorted(
                rows, key=lambda r: (0 if r.severity == "hard" else 1,
                                     r.code or "", r.id))
        ]

    def _evaluate_engine_risks(self):
        """Evaluation entry for the job form: run against the job's current
        truck when assigned; otherwise keep the persisted rows (there is no
        candidate to evaluate). Returns the payload."""
        self.ensure_one()
        if self.vehicle_id:
            return self._engine_risk_rows(self.vehicle_id)
        return self._persisted_risk_payload()

    def _open_engine_risks(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Engine Risks — %s" % self.name,
            "res_model": "prema.dispatch.job.risk",
            "view_mode": "tree,form",
            "domain": [("job_id", "=", self.id)],
            "context": {"create": False},
        }

    def action_explain_engine_risks(self):
        """Job-form header button: evaluate against the current truck (or
        keep the persisted rows when unassigned), then show the reasons."""
        self.ensure_one()
        self._evaluate_engine_risks()
        return self._open_engine_risks()

    def action_open_engine_risks(self):
        """Job-form smart button: open the persisted risk rows."""
        self.ensure_one()
        return self._open_engine_risks()

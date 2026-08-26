# ════════════════════════════════════════════════════════════════════
# Phases 12-16 — Booking execution layer.
#   Scenario choice, execution profitability (estimated vs actual, kept
#   distinct), margin guard, missed-connection handling, and the
#   dispatcher-override audit surface. The frozen customer price is
#   NEVER rewritten by execution cost changes.
# ════════════════════════════════════════════════════════════════════
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class LogisticsBookingExecution(models.Model):
    _inherit = "logistics.booking"

    # ── Scenario / execution state ──────────────────────────────────
    execution_scenario_ids = fields.One2many(
        "logistics.execution.scenario", "booking_id", string="Scenarios")
    execution_scenario_id = fields.Many2one(
        "logistics.execution.scenario", string="Execution Scenario",
        ondelete="set null", index=True, tracking=True)
    execution_snapshot = fields.Json(
        string="Execution Snapshot", readonly=True,
        help="Chosen scenario's frozen execution plan — later cost or "
             "carrier changes never rewrite this audit record.")
    execution_confirmation_required = fields.Boolean(
        string="Carrier Confirmation Required", default=False,
        help="True when the chosen execution depends on an unconfirmed "
             "subcontractor — customer-facing availability stays honest.")
    margin_warning = fields.Boolean(
        string="Margin Below Target", compute="_compute_execution_totals",
        store=True,
        help="Estimated execution margin below the configured minimum — "
             "dispatcher decision required, not a hard block.")
    has_subcontracted_legs = fields.Boolean(
        string="Subcontracted Legs", compute="_compute_execution_totals",
        store=True)

    # ── Execution cost totals (estimated vs actual, distinct) ───────
    own_fleet_cost_total = fields.Float(
        string="Own Fleet Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    subcontract_cost_total = fields.Float(
        string="Subcontract Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    hub_cost_total = fields.Float(
        string="Hub / Transfer Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    carrier_detention_cost_total = fields.Float(
        string="Carrier Detention Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    customer_detention_revenue = fields.Float(
        string="Customer Detention Revenue", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True,
        help="Approved customer SELL detention (Phase 10) — an "
             "independent authority from carrier BUY detention.")
    execution_estimated_cost = fields.Float(
        string="Execution Estimated Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    execution_estimated_margin = fields.Float(
        string="Execution Estimated Margin", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    execution_estimated_margin_pct = fields.Float(
        string="Execution Estimated Margin %", digits=(5, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    execution_cost_available = fields.Boolean(
        string="Execution Cost Available", compute="_compute_execution_totals",
        store=True, readonly=True,
        help="True only when a genuine estimated execution cost exists. "
             "A missing estimate must never read as a $0 cost or a "
             "100% margin — the UI hides margin figures and warns when "
             "this is False.")
    actual_total_cost = fields.Float(
        string="Actual Total Cost", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    actual_margin = fields.Float(
        string="Actual Gross Margin", digits=(12, 2),
        compute="_compute_execution_totals", store=True, readonly=True)
    actual_margin_pct = fields.Float(
        string="Actual Margin %", digits=(5, 2),
        compute="_compute_execution_totals", store=True, readonly=True)

    @api.depends("leg_ids.execution_mode", "leg_ids.estimated_leg_cost",
                 "leg_ids.accepted_buy_rate", "leg_ids.actual_leg_cost",
                 "leg_ids.hub_transfer_cost",
                 "leg_ids.carrier_detention_amount",
                 "leg_ids.connection_exception")
    def _compute_execution_totals(self):
        Param = self.env["ir.config_parameter"].sudo()
        try:
            min_margin = float(Param.get_param(
                "logistics.minimum_margin_pct", "10.0") or 10.0)
        except ValueError:
            min_margin = 10.0
        for booking in self:
            own = sub = hub = det = 0.0
            est_total = 0.0
            act_total = 0.0
            has_sub = False
            for leg in booking.leg_ids:
                if leg.execution_mode == "subcontracted":
                    sub += leg.accepted_buy_rate or leg.estimated_leg_cost or 0.0
                    has_sub = True
                elif leg.execution_mode == "own_fleet":
                    own += leg.estimated_leg_cost or 0.0
                hub += leg.hub_transfer_cost or 0.0
                det += leg.carrier_detention_amount or 0.0
                # Estimated total: own-fleet legs use their frozen
                # estimator result; subcontracted legs use the frozen
                # accepted carrier rate when no own estimate was ever
                # written (that rate IS their estimate authority). A leg
                # with neither stays out — never invented as $0.
                est_total += leg.estimated_leg_cost or (
                    leg.accepted_buy_rate
                    if leg.execution_mode == "subcontracted" else 0.0)
                if leg.actual_leg_cost:
                    act_total += leg.actual_leg_cost
            est_total += hub + det
            # Actuals come ONLY from billed/vendor sources (actual_leg_cost
            # written by the vendor-bill review). Estimated hub/detention
            # costs never masquerade as actuals.
            revenue = booking.calculated_price or 0.0
            det_rev = 0.0
            items = self.env["prema.dispatch.detention.item"].search([
                ("job_id", "in", booking.dispatch_job_ids.ids),
            ])
            for item in items:
                if item.state in ("approved", "modified"):
                    det_rev += item.approved_amount or 0.0
            booking.own_fleet_cost_total = round(own, 2)
            booking.subcontract_cost_total = round(sub, 2)
            booking.hub_cost_total = round(hub, 2)
            booking.carrier_detention_cost_total = round(det, 2)
            booking.customer_detention_revenue = round(det_rev, 2)
            booking.execution_estimated_cost = round(est_total, 2)
            booking.execution_estimated_margin = round(
                revenue - est_total, 2)
            booking.execution_estimated_margin_pct = round(
                (revenue - est_total) / revenue * 100.0, 2) if revenue > 0 \
                else 0.0
            booking.actual_total_cost = round(act_total, 2) if act_total \
                else 0.0
            booking.actual_margin = round(
                revenue - act_total, 2) if act_total else 0.0
            booking.actual_margin_pct = round(
                (revenue - act_total) / revenue * 100.0, 2) \
                if revenue > 0 and act_total else 0.0
            booking.has_subcontracted_legs = has_sub
            # Cost is "available" when a genuine estimate exists — or when
            # there is nothing to estimate yet (no legs). The UI hides the
            # margin figures and warns while it is False.
            booking.execution_cost_available = bool(est_total) \
                or not booking.leg_ids
            booking.margin_warning = bool(est_total) and \
                booking.execution_estimated_margin_pct < min_margin

    def _populate_own_fleet_cost_estimates(self, legs=None):
        """Deterministic own-fleet estimated cost for confirmed corridor
        legs — frozen authorities only, never live pricing tables, never
        a division of the booking total:
          * a per-leg allocation from the frozen cost_snapshot when the
            estimator emits one (no current channel does — guarded for
            future per-leg estimators): keys "legs"/"per_leg" indexed by
            leg sequence;
          * a single-leg booking may take the booking's frozen
            confirmation-time estimator result (estimated_cost, written
            by BookingOrchestrationService from the Prema AI estimator
            before leg creation);
          * anything else is left unset — a missing cost must surface as
            "requires estimate", never as $0 (execution_cost_available
            stays False and the UI warns instead of showing 100% margin).
        Idempotent: existing estimated_leg_cost values are never
        rewritten (the field is itself 'never rewritten after
        acceptance')."""
        for booking in self:
            targets = (legs or booking.leg_ids).filtered(
                lambda l: l.execution_mode == "own_fleet"
                and not l.estimated_leg_cost)
            if not targets:
                continue
            cost_snap = booking.cost_snapshot or {}
            if not isinstance(cost_snap, dict):
                cost_snap = {}
            per_leg = cost_snap.get("legs") or cost_snap.get("per_leg") or {}
            for leg in targets:
                entry = per_leg.get(str(leg.sequence))
                if entry is None:
                    entry = per_leg.get(leg.sequence)
                if isinstance(entry, dict) and entry.get("total_cost"):
                    leg.write({
                        "estimated_leg_cost": round(
                            float(entry["total_cost"]), 2),
                        "cost_source": "own_cost_estimate",
                    })
                    continue
                if len(booking.leg_ids) == 1 and booking.estimated_cost:
                    leg.write({
                        "estimated_leg_cost": round(
                            float(booking.estimated_cost), 2),
                        "cost_source": "own_cost_estimate",
                    })

    def action_recompute_margin(self):
        """Recompute the execution profitability view after an offer is
        accepted or a cost changes. Never touches the frozen customer
        price and never rewrites accepted estimates."""
        self._compute_execution_totals()
        for booking in self:
            booking.invalidate_recordset()
        return True

    def action_generate_execution_scenarios(self):
        """(Re)generate the bounded execution scenario set (16.1) for
        this booking via the scenario engine. Engine recommends,
        dispatcher decides."""
        from ..services.execution_scenario_service import (
            ExecutionScenarioService)
        service = ExecutionScenarioService(self.env)
        for booking in self:
            service.generate(booking)
        return True

    # ── Missed connection (16.7) ────────────────────────────────────

    def action_detect_missed_connections(self):
        """Freight at the hub whose onward departure was missed/cancelled
        is flagged — never marked delivered, never silently reassigned.
        Custody (pallets) is preserved untouched."""
        for booking in self:
            for leg in booking.leg_ids:
                if leg.execution_status in ("delivered", "at_hub") or \
                        not leg.departure_id:
                    continue
                dep = leg.departure_id
                if dep.status in ("cancelled", "completed") and \
                        leg.execution_status not in ("delivered",):
                    leg.action_mark_connection_exception()
        return True

    def action_release_missed_connections(self):
        for booking in self:
            for leg in booking.leg_ids:
                if leg.connection_exception:
                    leg.action_clear_connection_exception()
        return True

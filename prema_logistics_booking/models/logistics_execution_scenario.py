# ════════════════════════════════════════════════════════════════════
# Phase 12 — Execution Scenario. For each shipment the engine generates
# feasible execution alternatives (own fleet / scheduled corridor / hub
# split / subcontract mixes) and ranks them; the dispatcher chooses. The
# chosen scenario freezes an execution snapshot on the booking — later
# cost changes never rewrite the originally accepted estimate.
# ════════════════════════════════════════════════════════════════════
from odoo import _, api, fields, models


class LogisticsExecutionScenario(models.Model):
    _name = "logistics.execution.scenario"
    _description = "Execution Scenario"
    _order = "booking_id, rank"

    name = fields.Char(string="Scenario", compute="_compute_name", store=True)
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="cascade",
        index=True, required=True)
    rank = fields.Integer(string="Rank", default=10,
                          help="Rank of feasible scenarios — lower is better.")
    state = fields.Selection([
        ("auto_bookable", "AUTO BOOKABLE"),
        ("dispatch_confirmation", "DISPATCH CONFIRMATION REQUIRED"),
        ("carrier_rate_required", "CARRIER RATE REQUIRED"),
        ("carrier_acceptance_required", "CARRIER ACCEPTANCE REQUIRED"),
        ("manual_review", "MANUAL REVIEW"),
    ], string="Status", default="manual_review", required=True, tracking=True)
    chosen = fields.Boolean(string="Chosen", default=False, tracking=True)
    scenario_summary = fields.Char(
        string="Summary", compute="_compute_name", store=True)
    customer_revenue = fields.Float(
        string="Customer Revenue (pre-tax)", digits=(12, 2), readonly=True)
    estimated_total_cost = fields.Float(
        string="Estimated Total Cost", digits=(12, 2), readonly=True)
    estimated_margin = fields.Float(
        string="Estimated Gross Margin", digits=(12, 2), readonly=True)
    estimated_margin_pct = fields.Float(
        string="Estimated Margin %", digits=(5, 2), readonly=True)
    execution_plan = fields.Json(
        string="Execution Plan", readonly=True,
        help="Frozen per-leg plan: execution_mode, carrier, buy rate, "
             "cost source. Never rewritten after choice.")
    compliance_review = fields.Boolean(
        string="Compliance Review Required", default=False,
        help="Cross-border subcontract without configured authority — "
             "never auto-assign an outside carrier.")

    @api.depends("booking_id", "rank", "state")
    def _compute_name(self):
        for sc in self:
            label = "%s — %s" % (sc.booking_id.name or sc.booking_id.id,
                                 dict(sc._fields["state"].selection)
                                 .get(sc.state, sc.state))
            sc.name = "Scenario %02d: %s" % (sc.rank, label)
            sc.scenario_summary = "%02d · %s" % (sc.rank, label)

    def action_choose(self):
        """Dispatcher selects this scenario. Only one chosen per booking.
        Freezes the execution plan on the booking — engine suggestions
        never silently replace a dispatcher decision."""
        for sc in self:
            self.search([
                ("booking_id", "=", sc.booking_id.id),
                ("chosen", "=", True),
            ]).write({"chosen": False})
            sc.write({"chosen": True})
            sc.booking_id.write({
                "execution_scenario_id": sc.id,
                "execution_snapshot": sc.execution_plan,
                "execution_confirmation_required":
                    sc.state != "auto_bookable",
            })
        return True

# -*- coding: utf-8 -*-
"""fleet.vehicle additions: operator link, issue tracking, availability.

Two rules, deliberately different:

* a COMPANY truck keeps its existing eligibility rule — the boolean
  ``x_operational_logistics`` — so nothing about today's dispatch changes;
* an OWNER-OPERATOR truck is only offered when it is in the Ready fleet
  state, the operator's compliance is current (authority verified and an
  insurer-direct COI in date) and there is no unresolved blocking issue.

``is_dispatch_available`` is a STORED computed field so every existing
consumer can filter on one boolean instead of re-deriving the rule. Insurance
expiry is date-based, so a daily cron refreshes it — otherwise a truck whose
COI lapsed overnight would keep being offered.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    operator_id = fields.Many2one(
        "prema.owner.operator", string="Owner-operator", index=True,
        ondelete="restrict",
        help="Set only by the onboarding approval. Empty means company truck.")
    application_id = fields.Many2one(
        "prema.owner.operator.application", string="Source application",
        index=True, readonly=True, copy=False, ondelete="restrict",
        help="The application that created this equipment.")

    issue_reason = fields.Text(
        string="Issue reason",
        help="Why the truck is unavailable. Required while the fleet state is "
             "Issue / Temporarily Unavailable.")
    issue_expected_return = fields.Date(string="Expected return")
    issue_cleared_on = fields.Datetime(string="Issue cleared on", readonly=True)
    issue_cleared_by = fields.Many2one("res.users", string="Cleared by",
                                       readonly=True)
    issue_blocking = fields.Boolean(
        string="Blocking dispatch", compute="_compute_issue_blocking",
        store=True, index=True,
        help="True while the truck sits in the Issue / Temporarily "
             "Unavailable state and has not been cleared.")
    operator_compliance_ok = fields.Boolean(
        related="operator_id.compliance_ok", store=True, readonly=True)
    is_dispatch_available = fields.Boolean(
        string="Available for dispatch", compute="_compute_is_dispatch_available",
        store=True, index=True)

    @api.model
    def _issue_state(self):
        """The Issue / Temporarily Unavailable fleet state (data, not code)."""
        return self.env["fleet.vehicle.state"].search(
            [("name", "ilike", "Issue")], limit=1)

    @api.model
    def _ready_state(self):
        return self.env["fleet.vehicle.state"].search(
            [("name", "=", "Ready")], limit=1)

    @api.depends("state_id", "issue_cleared_on", "active")
    def _compute_issue_blocking(self):
        issue = self._issue_state()
        for vehicle in self:
            on_issue = bool(issue) and vehicle.state_id.id == issue.id
            vehicle.issue_blocking = bool(
                vehicle.active and on_issue and not vehicle.issue_cleared_on)

    @api.depends("active", "x_operational_logistics", "state_id",
                 "issue_blocking", "operator_id", "operator_compliance_ok")
    def _compute_is_dispatch_available(self):
        ready = self._ready_state()
        for vehicle in self:
            if not vehicle.active or vehicle.issue_blocking:
                vehicle.is_dispatch_available = False
            elif vehicle.operator_id:
                vehicle.is_dispatch_available = bool(
                    ready and vehicle.state_id.id == ready.id
                    and vehicle.operator_compliance_ok)
            else:
                vehicle.is_dispatch_available = bool(
                    vehicle.x_operational_logistics)

    @api.constrains("state_id", "issue_reason")
    def _check_issue_reason(self):
        issue = self._issue_state()
        for vehicle in self:
            if issue and vehicle.state_id.id == issue.id \
                    and not (vehicle.issue_reason or "").strip():
                raise ValidationError(_(
                    "Record why the truck is on Issue / Temporarily "
                    "Unavailable."))

    def action_clear_issue(self):
        """Clear the blocking issue and put the truck back to Ready.

        Clearance is explicit and audited — never inferred from the
        expected-return date passing.
        """
        ready = self._ready_state()
        for vehicle in self:
            vehicle.write({
                "issue_cleared_on": fields.Datetime.now(),
                "issue_cleared_by": self.env.user.id,
                "state_id": ready.id or vehicle.state_id.id,
            })
            vehicle.message_post(
                body=_("Issue cleared — truck returned to Ready."),
                subtype_xmlid="mail.mt_note")
        return True

    # ------------------------------------------------------------------
    # the ONE availability authority
    # ------------------------------------------------------------------
    def _dispatch_available(self):
        self.ensure_one()
        return self.is_dispatch_available

    @api.model
    def _dispatch_available_domain(self):
        """The same rule as a search domain, for the dispatch consumers."""
        return [("is_dispatch_available", "=", True)]

    @api.model
    def _cron_refresh_availability(self):
        """Re-evaluate the stored boolean, mainly for insurance expiry."""
        vehicles = self.search([])
        vehicles._compute_is_dispatch_available()
        return True

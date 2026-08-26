# ════════════════════════════════════════════════════════════════════
# Phase 10 — Customer Detention
#   Rule hierarchy: most specific wins — (customer, facility) →
#   (customer) → (facility) → company-wide rule → company default
#   (single ir.config_parameter authority, seeded by migration).
#   Charge formula: billable = MAX(dwell − free, 0); units =
#   CEILING(billable / increment); charge = units × rate.
#   Suggested amounts are STAFF-REVIEWED (Approve / Modify / Waive)
#   before they are added to the booking's existing draft invoice —
#   detention never creates a second invoice.
# ════════════════════════════════════════════════════════════════════
import json
import logging
import math

from odoo import _, api, exceptions, fields, models

_logger = logging.getLogger(__name__)

_DETENTION_DEFAULTS_PARAM = "prema_dispatch.detention_defaults"


class PremaDispatchDetentionRule(models.Model):
    _name = "prema.dispatch.detention.rule"
    _description = "Detention Rule"
    _order = "partner_id, facility_id, id"

    name = fields.Char(string="Name", compute="_compute_name", store=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", ondelete="cascade", index=True,
        help="Restrict this rule to one customer. Empty = applies to any "
             "customer (most-specific rule wins).")
    facility_id = fields.Many2one(
        "prema.dispatch.location", string="Facility", ondelete="cascade",
        index=True,
        help="Restrict this rule to one facility. Empty = applies to any "
             "facility (most-specific rule wins).")
    enabled = fields.Boolean(string="Enabled", default=True)
    free_minutes = fields.Integer(
        string="Free Time (min)", default=30,
        help="Dwell beyond this triggers detention billing.")
    increment_minutes = fields.Integer(
        string="Billing Increment (min)", default=30,
        help="Billing is rounded UP to whole increments.")
    rate_per_increment = fields.Float(
        string="Rate per Increment", digits=(10, 2),
        help="Charge per billing increment. Buy (what Prema pays) is tracked "
             "separately in vendor costs — this is the SELL rate.")

    @api.depends("partner_id", "facility_id")
    def _compute_name(self):
        for rule in self:
            parts = []
            if rule.partner_id:
                parts.append(rule.partner_id.name)
            if rule.facility_id:
                parts.append(rule.facility_id.name)
            rule.name = " → ".join(parts) if parts else "Company-wide"

    @api.onchange("partner_id", "facility_id")
    def _onchange_defaults(self):
        # Seed empty fields from the company default so a new rule always
        # starts from the configured baseline.
        for rule in self:
            if not rule.free_minutes and not rule.increment_minutes \
                    and not rule.rate_per_increment:
                defaults = self._company_defaults()
                rule.free_minutes = defaults["free_minutes"]
                rule.increment_minutes = defaults["increment_minutes"]
                rule.rate_per_increment = defaults["rate_per_increment"]

    @api.model
    def _company_defaults(self):
        """Company-wide detention baseline from the ONE config-parameter
        authority (seeded idempotently by the 18.0.3.29.0 migration)."""
        try:
            raw = self.env["ir.config_parameter"].sudo().get_param(
                _DETENTION_DEFAULTS_PARAM, "{}") or "{}"
            defaults = json.loads(raw)
        except ValueError:
            defaults = {}
        return {
            "free_minutes": int(defaults.get("free_minutes", 30) or 30),
            "increment_minutes": int(defaults.get("increment_minutes", 30) or 30),
            "rate_per_increment": float(defaults.get("rate_per_increment", 0.0) or 0.0),
        }

    @api.model
    def _match(self, partner_id, facility_id):
        """Resolve detention parameters for a stop. Hierarchy — most
        specific rule wins: (customer, facility) → (customer) → (facility)
        → company-wide rule → company default. Returns
        {rule, free_minutes, increment_minutes, rate_per_increment}."""
        rules = self.search([("enabled", "=", True)])
        pid = partner_id or False
        fid = facility_id or False
        for pattern in ((pid, fid), (pid, False), (False, fid), (False, False)):
            hit = rules.filtered(
                lambda r, p=pattern: r.partner_id.id == p[0]
                and r.facility_id.id == p[1])
            if hit:
                hit = hit[0]
                return {
                    "rule": hit,
                    "free_minutes": max(0, int(hit.free_minutes or 0)),
                    "increment_minutes": max(1, int(hit.increment_minutes or 1)),
                    "rate_per_increment": float(hit.rate_per_increment or 0.0),
                }
        defaults = self._company_defaults()
        return {
            "rule": False,
            "free_minutes": defaults["free_minutes"],
            "increment_minutes": defaults["increment_minutes"],
            "rate_per_increment": defaults["rate_per_increment"],
        }


class PremaDispatchDetentionItem(models.Model):
    _name = "prema.dispatch.detention.item"
    _description = "Detention Item"
    _order = "id desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(
        string="Detention #", default="New", copy=False, readonly=True)
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", ondelete="cascade",
        required=True, index=True)

    _sql_constraints = [
        ("stop_id_unique", "unique(stop_id)",
         "Only one detention item per stop."),
    ]
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Job", ondelete="cascade", index=True)
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="set null", index=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", related="job_id.partner_id",
        store=True, index=True, readonly=True)
    facility_id = fields.Many2one(
        "prema.dispatch.location", string="Facility",
        related="stop_id.saved_location_id", store=True, readonly=True)
    actual_arrival_time = fields.Datetime(
        related="stop_id.actual_arrival_time", readonly=True)
    actual_departure_time = fields.Datetime(
        related="stop_id.actual_departure_time", readonly=True)
    actual_dwell_minutes = fields.Integer(
        string="Actual Dwell (min)", readonly=True,
        help="Dwell = actual departure − actual arrival (the actuals are the "
             "authority — never planned times).")
    free_minutes = fields.Integer(string="Free Time (min)", readonly=True)
    increment_minutes = fields.Integer(
        string="Billing Increment (min)", readonly=True)
    rate_per_increment = fields.Float(
        string="Rate per Increment", digits=(10, 2), readonly=True)
    billable_minutes = fields.Integer(
        string="Billable (min)", compute="_compute_charges", store=True,
        readonly=True)
    units = fields.Integer(
        string="Billing Units", compute="_compute_charges", store=True,
        readonly=True)
    suggested_amount = fields.Monetary(
        string="Suggested Charge", compute="_compute_charges", store=True,
        readonly=True,
        help="Original suggested charge — never rewritten after review.")
    approved_amount = fields.Monetary(
        string="Approved Charge", tracking=True,
        help="The amount actually billed after review (modify to change).")
    state = fields.Selection([
        ("draft", "Draft"),
        ("approved", "Approved"),
        ("modified", "Modified"),
        ("waived", "Waived"),
    ], string="Status", default="draft", tracking=True, copy=False)
    review_user_id = fields.Many2one(
        "res.users", string="Reviewed By", readonly=True)
    review_time = fields.Datetime(string="Reviewed At", readonly=True)
    reason_notes = fields.Text(
        string="Reason / Notes",
        help="Why the charge was modified or waived.")
    invoiced = fields.Boolean(string="Invoiced", readonly=True, copy=False)
    invoice_line_id = fields.Many2one(
        "account.move.line", string="Invoice Line", readonly=True,
        ondelete="set null", copy=False)
    currency_id = fields.Many2one(
        "res.currency", readonly=True,
        default=lambda self: self.env.company.currency_id)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        seq = self.env["ir.sequence"].sudo().next_by_code(
            "prema.dispatch.detention.item")
        for i, rec in enumerate(records):
            rec.name = "DET/%s" % (seq if len(records) == 1
                                   else "%s-%d" % (seq, i + 1))
        return records

    @api.depends("actual_dwell_minutes", "free_minutes", "increment_minutes",
                 "rate_per_increment")
    def _compute_charges(self):
        for item in self:
            billable = max(
                int(item.actual_dwell_minutes or 0) - int(item.free_minutes or 0),
                0)
            units = int(math.ceil(
                billable / float(item.increment_minutes or 1))) if billable else 0
            item.billable_minutes = billable
            item.units = units
            item.suggested_amount = round(
                units * float(item.rate_per_increment or 0.0), 2)

    @api.model
    def _suggest_for_stop(self, stop):
        """Create (or refresh a still-draft) detention item from a
        completed stop's ACTUAL dwell. Idempotent — one item per stop.
        Never auto-approves and never auto-invoices; suggested amounts
        always go through staff review."""
        if not stop.actual_arrival_time or not stop.actual_departure_time:
            return False
        dwell = (stop.actual_departure_time - stop.actual_arrival_time)
        dwell_min = dwell.total_seconds() / 60.0
        job = stop.job_id
        params = self.env["prema.dispatch.detention.rule"]._match(
            job.partner_id.id if job else False,
            stop.saved_location_id.id if stop.saved_location_id else False,
        )
        if dwell_min <= params["free_minutes"]:
            return False
        booking_id = False
        if job and "logistics_booking_id" in job._fields \
                and "logistics.booking" in self.env.registry.models \
                and job.logistics_booking_id:
            booking_id = job.logistics_booking_id.id
        existing = self.search([("stop_id", "=", stop.id)], limit=1)
        if existing:
            if existing.state == "draft":
                vals = {
                    "actual_dwell_minutes": int(round(dwell_min)),
                    "free_minutes": params["free_minutes"],
                    "increment_minutes": params["increment_minutes"],
                    "rate_per_increment": params["rate_per_increment"],
                }
                if booking_id:
                    vals["booking_id"] = booking_id
                existing.write(vals)
            return existing
        return self.create({
            "stop_id": stop.id,
            "job_id": job.id if job else False,
            "booking_id": booking_id,
            "actual_dwell_minutes": int(round(dwell_min)),
            "free_minutes": params["free_minutes"],
            "increment_minutes": params["increment_minutes"],
            "rate_per_increment": params["rate_per_increment"],
        })

    # ── Review workflow ─────────────────────────────────────────────

    def _mark_reviewed(self, state, amount):
        self.write({
            "state": state,
            "approved_amount": amount,
            "review_user_id": self.env.user.id,
            "review_time": fields.Datetime.now(),
        })

    def action_approve(self):
        for item in self:
            if item.state in ("approved", "modified", "waived"):
                raise exceptions.UserError(
                    _("Detention %s is already %s.") % (item.name, item.state))
            item._mark_reviewed("approved", item.suggested_amount)
        return True

    def action_modify(self):
        for item in self:
            if item.state in ("approved", "modified", "waived"):
                raise exceptions.UserError(
                    _("Detention %s is already %s.") % (item.name, item.state))
            if item.approved_amount is None or item.approved_amount < 0:
                raise exceptions.UserError(
                    _("Set an Approved Charge before confirming the "
                      "modification."))
            item._mark_reviewed("modified", item.approved_amount)
        return True

    def action_waive(self):
        for item in self:
            if item.state in ("approved", "modified", "waived"):
                raise exceptions.UserError(
                    _("Detention %s is already %s.") % (item.name, item.state))
            item._mark_reviewed("waived", 0.0)
        return True

    def action_add_to_invoice(self):
        """Append the approved charge to the booking's existing DRAFT
        invoice (never a second invoice). Idempotent — one line per item."""
        for item in self:
            if item.invoiced or item.invoice_line_id:
                continue
            if item.state not in ("approved", "modified"):
                raise exceptions.UserError(
                    _("Detention %s must be Approved or Modified before it "
                      "can be invoiced.") % item.name)
            if not item.approved_amount:
                raise exceptions.UserError(
                    _("Detention %s has no approved amount.") % item.name)
            booking = item.booking_id
            if not booking:
                raise exceptions.UserError(
                    _("Detention %s has no booking — invoice manually and "
                      "link the line back to this item.") % item.name)
            invoice = booking._create_draft_invoice()
            if not invoice:
                raise exceptions.UserError(
                    _("Could not create/open the draft invoice for booking "
                      "%s — check the freight product mapping.") % booking.name)
            invoice = invoice.sudo()
            line = invoice.invoice_line_ids.filtered(
                lambda l, i=item.id: (l.name or "") and
                ("Detention #%s" % i) in l.name)[:1]
            if not line:
                product = booking._select_freight_product()[0]
                tax_ids = []
                if booking.tax_rule_id:
                    tax_ids = [(6, 0, [booking.tax_rule_id.id])]
                detail = " — %s" % item.reason_notes if item.reason_notes else ""
                name = "%s — Detention #%s (%s × %s min)%s" % (
                    item.facility_id.name if item.facility_id else "Detention",
                    item.id, item.units, item.increment_minutes, detail)
                # Odoo 18: lines are added through the parent invoice
                # (the o2m command fills move_id — a bare create on
                # invoice_line_ids raises KeyError: 'move_id').
                invoice.write({"invoice_line_ids": [(0, 0, {
                    "product_id": product.id if product else False,
                    "name": name,
                    "quantity": 1,
                    "price_unit": item.approved_amount,
                    "tax_ids": tax_ids,
                })]})
                invoice.invalidate_recordset()
                line = invoice.invoice_line_ids.filtered(
                    lambda l, i=item.id: (l.name or "") and
                    ("Detention #%s" % i) in l.name)[:1]
            item.write({"invoiced": True, "invoice_line_id": line.id})
        return True

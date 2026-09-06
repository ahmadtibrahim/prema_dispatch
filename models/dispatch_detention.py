# ════════════════════════════════════════════════════════════════════
# Phase 10 — Customer Detention
#   Rule hierarchy: most specific wins — (customer, facility) →
#   (customer) → (facility) → company-wide rule → company default
#   (single ir.config_parameter authority, seeded by migration).
#   Charge formula: billable = MAX(dwell − free, 0); units =
#   CEILING(billable / increment); charge = clamp(units × rate,
#   minimum_charge, maximum_charge). Rate display keeps the
#   increment-based calculation; a per-hour equivalent is shown on the
#   rule form for reference only.
#   Rules carry a PICKUP vs DELIVERY dimension (stop-kind split, §18):
#   the base columns are the DELIVERY parameter set and pickup_* columns
#   the pickup set; a pickup side left entirely at zero falls back to
#   the matched rule's delivery side so pre-§18 rules behave unchanged
#   (the 18.0.3.47.0 migration backfills existing rows idempotently).
#   Dwell span: (dock start → release) when both timing events were
#   recorded, otherwise (arrival → departure) — dispatch records
#   check-in / dock-start / release via stop.action_record_timing.
#   Suggested amounts are STAFF-REVIEWED (Approve / Modify / Waive)
#   before they are added to the booking's existing draft invoice —
#   detention never creates a second invoice. Rules may require
#   evidence (evidence_required) — approve/modify are then gated on
#   linked evidence. Exception codes (traffic / weather / facility /
#   customer / …) are CATALOG ONLY: they never rewrite the suggested
#   amount — the item always stays in the human review state machine.
# ════════════════════════════════════════════════════════════════════
import json
import logging
import math

from odoo import _, api, exceptions, fields, models

_logger = logging.getLogger(__name__)

_DETENTION_DEFAULTS_PARAM = "prema_dispatch.detention_defaults"

# ── Stop-kind split (§18) ─────────────────────────────────────────────
DETENTION_KIND_DELIVERY = "delivery"
DETENTION_KIND_PICKUP = "pickup"

# Dispatch stop types that count as PICKUP-kind for detention rule
# matching; every other stop type (dropoff / return / transfer /
# cross_dock_drop / other) is delivery-kind.
_PICKUP_STOP_TYPES = ("pickup", "cross_dock_pickup")

DETENTION_EXCEPTIONS = [
    ("none", "None"),
    ("traffic", "Traffic / Road Delay"),
    ("weather", "Weather"),
    ("facility_delay", "Facility Delay (dock / staff)"),
    ("customer_delay", "Customer-Caused Delay"),
    ("equipment", "Equipment Issue"),
    ("driver", "Driver / Carrier-Caused"),
    ("other", "Other"),
]


def detention_stop_kind(stop):
    """Pickup-kind or delivery-kind of a dispatch stop for rule matching
    and item freezing. 'other' stops default to delivery-kind."""
    if stop and stop.stop_type in _PICKUP_STOP_TYPES:
        return DETENTION_KIND_PICKUP
    return DETENTION_KIND_DELIVERY


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
    # ── Delivery parameter set (base columns — pre-§18 single set) ──
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
    # ── Pickup parameter set (§18 stop-kind split) ──────────────────
    pickup_free_minutes = fields.Integer(
        string="Pickup Free Time (min)",
        help="Free time for PICKUP-kind stops. Empty/zero with the whole "
             "pickup side untouched falls back to the delivery columns of "
             "the same rule (legacy behaviour).")
    pickup_increment_minutes = fields.Integer(
        string="Pickup Billing Increment (min)",
        help="Billing increment for PICKUP-kind stops.")
    pickup_rate_per_increment = fields.Float(
        string="Pickup Rate per Increment", digits=(10, 2),
        help="SELL rate per billing increment for PICKUP-kind stops.")
    # ── Rule extras (§18) ───────────────────────────────────────────
    minimum_charge = fields.Float(
        string="Minimum Charge", digits=(10, 2),
        help="Floor for the suggested charge once any unit bills. 0 = no "
             "minimum (legacy behaviour).")
    maximum_charge = fields.Float(
        string="Maximum Charge (cap)", digits=(10, 2),
        help="Optional cap on the suggested charge. 0 = no cap (legacy "
             "behaviour).")
    evidence_required = fields.Boolean(
        string="Evidence Required at Approval",
        help="When set, an item following this rule cannot be Approved or "
             "Modified until evidence records are linked to it (Waive stays "
             "available).")
    # Hourly-rate equivalents — display only; the increment-based
    # calculation stays the authority.
    delivery_hourly_rate = fields.Float(
        string="Delivery Rate / Hour (equiv)", digits=(10, 2),
        compute="_compute_hourly_rates", readonly=True)
    pickup_hourly_rate = fields.Float(
        string="Pickup Rate / Hour (equiv)", digits=(10, 2),
        compute="_compute_hourly_rates", readonly=True)

    @api.depends("increment_minutes", "rate_per_increment",
                 "pickup_increment_minutes", "pickup_rate_per_increment")
    def _compute_hourly_rates(self):
        for rule in self:
            rule.delivery_hourly_rate = round(
                float(rule.rate_per_increment or 0.0)
                / max(1, int(rule.increment_minutes or 1)) * 60.0, 2)
            inc = rule.pickup_increment_minutes or rule.increment_minutes
            rate = (rule.pickup_rate_per_increment
                    if rule._pickup_side_set() else rule.rate_per_increment)
            rule.pickup_hourly_rate = round(
                float(rate or 0.0) / max(1, int(inc or 1)) * 60.0, 2)

    @api.depends("partner_id", "facility_id")
    def _compute_name(self):
        for rule in self:
            parts = []
            if rule.partner_id:
                parts.append(rule.partner_id.name)
            if rule.facility_id:
                parts.append(rule.facility_id.name)
            rule.name = " → ".join(parts) if parts else "Company-wide"

    def _pickup_side_set(self):
        """The pickup parameter set is 'set' the moment ANY pickup column
        is non-zero — a fully-zero pickup side falls back to the delivery
        columns (legacy single-set behaviour, §18)."""
        self.ensure_one()
        return bool(self.pickup_free_minutes or self.pickup_increment_minutes
                    or self.pickup_rate_per_increment)

    def _pickup_params(self):
        """Pickup-side parameters of this rule: the pickup columns when the
        pickup side was configured, else the delivery columns."""
        self.ensure_one()
        if self._pickup_side_set():
            return {
                "free_minutes": self.pickup_free_minutes,
                "increment_minutes": self.pickup_increment_minutes,
                "rate_per_increment": self.pickup_rate_per_increment,
            }
        return {
            "free_minutes": self.free_minutes,
            "increment_minutes": self.increment_minutes,
            "rate_per_increment": self.rate_per_increment,
        }

    @api.onchange("partner_id", "facility_id", "free_minutes",
                  "increment_minutes", "rate_per_increment")
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
            # §18 stop-kind split: keep the pickup side in step with the
            # delivery side while it was never configured — the split is
            # OPT-IN per rule. An intentionally different pickup side (any
            # pickup column non-zero) is never overwritten.
            if not rule._pickup_side_set():
                rule.pickup_free_minutes = rule.free_minutes
                rule.pickup_increment_minutes = rule.increment_minutes
                rule.pickup_rate_per_increment = rule.rate_per_increment

    @api.model_create_multi
    def create(self, vals_list):
        # Server-side twin of the onchange: rules created through the API
        # or imports get the same pickup-side fallback copy.
        defaults = None
        for vals in vals_list:
            pickup_set = bool(
                vals.get("pickup_free_minutes")
                or vals.get("pickup_increment_minutes")
                or vals.get("pickup_rate_per_increment"))
            if pickup_set:
                continue
            if any(vals.get(k) for k in (
                    "free_minutes", "increment_minutes",
                    "rate_per_increment")):
                vals.setdefault("pickup_free_minutes",
                                vals.get("free_minutes"))
                vals.setdefault("pickup_increment_minutes",
                                vals.get("increment_minutes"))
                vals.setdefault("pickup_rate_per_increment",
                                vals.get("rate_per_increment"))
            else:
                # Entirely empty rule → seed both sides from the company
                # default baseline.
                if defaults is None:
                    defaults = self._company_defaults()
                for key in ("free_minutes", "increment_minutes",
                            "rate_per_increment"):
                    vals.setdefault(key, defaults[key])
                for key in ("pickup_free_minutes",
                            "pickup_increment_minutes",
                            "pickup_rate_per_increment"):
                    vals.setdefault(key, defaults[key[len("pickup_"):]])
        return super().create(vals_list)

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
    def _match(self, partner_id, facility_id, stop_kind="delivery"):
        """Resolve detention parameters for a stop. Hierarchy — most
        specific rule wins: (customer, facility) → (customer) → (facility)
        → company-wide rule → company default. `stop_kind` ("pickup" or
        "delivery", §18) selects the rule's per-kind parameter set; a
        pickup side never configured falls back to the matched rule's
        delivery columns. Returns {rule, stop_kind, free_minutes,
        increment_minutes, rate_per_increment, minimum_charge,
        maximum_charge, evidence_required}."""
        kind = stop_kind if stop_kind == DETENTION_KIND_PICKUP \
            else DETENTION_KIND_DELIVERY
        rules = self.search([("enabled", "=", True)])
        pid = partner_id or False
        fid = facility_id or False
        for pattern in ((pid, fid), (pid, False), (False, fid), (False, False)):
            hit = rules.filtered(
                lambda r, p=pattern: r.partner_id.id == p[0]
                and r.facility_id.id == p[1])
            if hit:
                hit = hit[0]
                params = hit._pickup_params() if kind == DETENTION_KIND_PICKUP \
                    else {
                        "free_minutes": hit.free_minutes,
                        "increment_minutes": hit.increment_minutes,
                        "rate_per_increment": hit.rate_per_increment,
                    }
                return {
                    "rule": hit,
                    "stop_kind": kind,
                    "free_minutes": max(0, int(params["free_minutes"] or 0)),
                    "increment_minutes": max(1, int(params["increment_minutes"] or 1)),
                    "rate_per_increment": float(params["rate_per_increment"] or 0.0),
                    "minimum_charge": float(hit.minimum_charge or 0.0),
                    "maximum_charge": float(hit.maximum_charge or 0.0),
                    "evidence_required": bool(hit.evidence_required),
                }
        defaults = self._company_defaults()
        return {
            "rule": False,
            "stop_kind": kind,
            "free_minutes": defaults["free_minutes"],
            "increment_minutes": defaults["increment_minutes"],
            "rate_per_increment": defaults["rate_per_increment"],
            "minimum_charge": 0.0,
            "maximum_charge": 0.0,
            "evidence_required": False,
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
    # §18 facility-timing records beyond arrival/departure — recorded on
    # the STOP (they can happen before this item exists) and mirrored here
    # read-only. Dwell spans (dock start → release) when both exist.
    check_in_at = fields.Datetime(
        related="stop_id.check_in_at", readonly=True)
    dock_start_at = fields.Datetime(
        related="stop_id.dock_start_at", readonly=True)
    released_at = fields.Datetime(
        related="stop_id.released_at", readonly=True)
    stop_kind = fields.Selection([
        (DETENTION_KIND_PICKUP, "Pickup"),
        (DETENTION_KIND_DELIVERY, "Delivery"),
    ], string="Stop Kind", readonly=True,
        help="Which per-kind rule parameter set was applied (frozen at "
             "suggestion, §18).")
    actual_dwell_minutes = fields.Integer(
        string="Actual Dwell (min)", readonly=True,
        help="Dwell = the span that measures how long the facility kept the "
             "truck: (dock start → release) when both timing events were "
             "recorded, otherwise actual departure − actual arrival (the "
             "actuals are the authority — never planned times).")
    free_minutes = fields.Integer(string="Free Time (min)", readonly=True)
    increment_minutes = fields.Integer(
        string="Billing Increment (min)", readonly=True)
    rate_per_increment = fields.Float(
        string="Rate per Increment", digits=(10, 2), readonly=True)
    minimum_charge = fields.Monetary(
        string="Minimum Charge", readonly=True,
        currency_field="currency_id",
        help="Rule floor frozen at suggestion; 0 = no minimum.")
    maximum_charge = fields.Monetary(
        string="Maximum Charge (cap)", readonly=True,
        currency_field="currency_id",
        help="Rule cap frozen at suggestion; 0 = no cap.")
    evidence_required = fields.Boolean(
        string="Evidence Required", readonly=True,
        help="Rule flag frozen at suggestion: Approve/Modify are gated on "
             "linked evidence records.")
    exception_type = fields.Selection(
        DETENTION_EXCEPTIONS, string="Exception Type", copy=False,
        help="Catalog of why the facility kept the truck (traffic / weather "
             "/ facility / customer …). CATALOG ONLY — it never rewrites "
             "the suggested charge: the item always stays in staff review "
             "(Approve / Modify / Waive).")
    evidence_ids = fields.Many2many(
        "prema.dispatch.evidence",
        "prema_dispatch_detention_evidence_rel",
        "detention_item_id", "evidence_id",
        string="Evidence",
        domain="[('stop_id', '=', stop_id)]",
        help="Dispatch evidence records backing this charge (POD/POP/issue "
             "photos). Stop evidence auto-links when the item is suggested.")
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
                 "rate_per_increment", "minimum_charge", "maximum_charge")
    def _compute_charges(self):
        for item in self:
            billable = max(
                int(item.actual_dwell_minutes or 0) - int(item.free_minutes or 0),
                0)
            units = int(math.ceil(
                billable / float(item.increment_minutes or 1))) if billable else 0
            charge = round(
                units * float(item.rate_per_increment or 0.0), 2)
            # §18 rule extras — floors/caps frozen from the matched rule.
            # The minimum only binds when at least one unit bills (free
            # time inside the free window is never a charge).
            if units and item.minimum_charge and charge < item.minimum_charge:
                charge = round(item.minimum_charge, 2)
            if item.maximum_charge and charge > item.maximum_charge:
                charge = round(item.maximum_charge, 2)
            item.billable_minutes = billable
            item.units = units
            item.suggested_amount = charge

    @api.model
    def _dwell_minutes(self, stop):
        """Detention dwell span (§18): (dock start → release) when both
        timing events were recorded — the facility-held span — otherwise
        (arrival → departure). Returns whole minutes."""
        if stop.dock_start_at and stop.released_at \
                and stop.released_at >= stop.dock_start_at:
            span = stop.released_at - stop.dock_start_at
        elif stop.actual_arrival_time and stop.actual_departure_time \
                and stop.actual_departure_time >= stop.actual_arrival_time:
            span = stop.actual_departure_time - stop.actual_arrival_time
        else:
            return 0
        return int(round(span.total_seconds() / 60.0))

    @api.model
    def _sync_evidence_for_stop(self, stop):
        """Link the stop's live evidence records (uploaded, not
        superseded) to the stop's detention item — called when an item is
        suggested and when a new evidence record arrives, so the approval
        gate sees the full proof set."""
        item = self.search([("stop_id", "=", stop.id)], limit=1)
        if not item:
            return False
        live = self.env["prema.dispatch.evidence"].search([
            ("stop_id", "=", stop.id),
            ("upload_state", "=", "uploaded"),
            ("superseded_by_id", "=", False),
        ])
        missing = live - item.evidence_ids
        if missing:
            item.write({"evidence_ids": [(4, e.id) for e in missing]})
        return bool(missing)

    @api.model
    def _suggest_for_stop(self, stop):
        """Create (or refresh a still-draft) detention item from a
        completed stop's ACTUAL dwell. Idempotent — one item per stop.
        Never auto-approves and never auto-invoices; suggested amounts
        always go through staff review. Dwell uses the (dock start →
        release) span when both timing events were recorded (§18)."""
        if not stop.actual_arrival_time or not stop.actual_departure_time:
            return False
        dwell_min = self._dwell_minutes(stop)
        job = stop.job_id
        kind = detention_stop_kind(stop)
        params = self.env["prema.dispatch.detention.rule"]._match(
            job.partner_id.id if job else False,
            stop.saved_location_id.id if stop.saved_location_id else False,
            stop_kind=kind,
        )
        booking_id = False
        if job and "logistics_booking_id" in job._fields \
                and "logistics.booking" in self.env.registry.models \
                and job.logistics_booking_id:
            booking_id = job.logistics_booking_id.id
        frozen = {
            "stop_kind": kind,
            "actual_dwell_minutes": dwell_min,
            "free_minutes": params["free_minutes"],
            "increment_minutes": params["increment_minutes"],
            "rate_per_increment": params["rate_per_increment"],
            "minimum_charge": params["minimum_charge"],
            "maximum_charge": params["maximum_charge"],
            "evidence_required": params["evidence_required"],
        }
        existing = self.search([("stop_id", "=", stop.id)], limit=1)
        if existing:
            # A still-draft item always follows the stop: §18 timing
            # backfills (dock start / release recorded after completion)
            # can shrink the facility-held span below the free window or
            # to zero — the stale suggestion must NOT survive. Reviewed
            # items are immutable and stay untouched.
            if existing.state == "draft":
                vals = dict(frozen)
                if booking_id:
                    vals["booking_id"] = booking_id
                existing.write(vals)
                self._sync_evidence_for_stop(stop)
            return existing
        if dwell_min <= 0:
            return False
        if dwell_min <= params["free_minutes"]:
            return False
        item = self.create({
            "stop_id": stop.id,
            "job_id": job.id if job else False,
            "booking_id": booking_id,
            **frozen,
        })
        self._sync_evidence_for_stop(stop)
        return item

    # ── Review workflow ─────────────────────────────────────────────

    def _mark_reviewed(self, state, amount):
        self.write({
            "state": state,
            "approved_amount": amount,
            "review_user_id": self.env.user.id,
            "review_time": fields.Datetime.now(),
        })

    def _check_review_requirements(self, charge_bearing=True):
        """§18 evidence gate: when the frozen rule flag requires evidence,
        a charge-bearing review (Approve or Modify) needs linked evidence
        records. Waive never does. Exception codes never auto-change the
        suggested amount — review stays human — so they play no part in
        this gate."""
        for item in self:
            if item.state in ("approved", "modified", "waived"):
                raise exceptions.UserError(
                    _("Detention %s is already %s.") % (item.name, item.state))
            if item.evidence_required and not item.evidence_ids:
                raise exceptions.UserError(
                    _("Detention %s requires evidence before approval "
                      "(rule: evidence required). Link the stop's "
                      "evidence records to the item first, or Waive the "
                      "charge.") % item.name)

    def action_approve(self):
        self._check_review_requirements()
        for item in self:
            item._mark_reviewed("approved", item.suggested_amount)
        return True

    def action_modify(self):
        self._check_review_requirements()
        for item in self:
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
                # §18: the line label carries the frozen kind/parameters —
                # which per-kind rule side applied, the frozen extras
                # (minimum/cap) and the exception catalog code.
                kind = {
                    "pickup": "Pickup detention",
                    "delivery": "Delivery detention",
                }.get(item.stop_kind, "Detention")
                if item.exception_type and item.exception_type != "none":
                    kind += " (%s)" % dict(DETENTION_EXCEPTIONS).get(
                        item.exception_type, item.exception_type)
                span = "%s × %s min" % (item.units, item.increment_minutes)
                frozen = [part for part in (
                    ("min %g" % item.minimum_charge)
                    if item.minimum_charge else "",
                    ("cap %g" % item.maximum_charge)
                    if item.maximum_charge else "",
                ) if part]
                name = "%s — Detention #%s (%s; %s%s)%s" % (
                    item.facility_id.name if item.facility_id else "Detention",
                    item.id, kind, span,
                    "; " + ", ".join(frozen) if frozen else "", detail)
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

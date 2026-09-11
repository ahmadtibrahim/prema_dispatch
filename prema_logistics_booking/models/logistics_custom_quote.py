import datetime
import hashlib
import html
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .logistics_custom_quote_send_attempt import (
    _INTERNAL_UPDATE_CTX as _CQ_ATTEMPT_INTERNAL_UPDATE_CTX)

QUOTE_STATE = [
    ("new", "New"),
    ("reviewing", "Reviewing"),
    ("quoted", "Quoted"),
    ("accepted", "Accepted"),
    ("declined", "Declined"),
    ("converted", "Converted to Booking"),
]

# ════════════════════════════════════════════════════════════════════
# Customer Rate Confirmation lifecycle (productized, master §3.7/§4).
#
# logistics.custom.quote IS the customer Rate Confirmation document:
#
#   draft (new/reviewing/quoted — fully editable)
#     → action_preview            PURE READ — renders the PDF, writes nothing
#     → action_send_rc            EXPLICIT one-shot send (idempotent: one
#                                 send per revision, row-locked, marker
#                                 written BEFORE queueing, immutable
#                                 logistics.custom.quote.send.attempt rows)
#     → action_record_customer_acceptance   staff records out-of-band
#                                 acceptance (channel + timestamp) — no
#                                 confirm, no email
#     → action_confirm_internally staff-side GO decision (internal only)
#     → action_convert_to_booking gated: acceptance recorded AND internal
#                                 confirmed (or booking-manager override w/
#                                 reason); converts exactly once
#     → action_revise             AUTHORIZED (booking-manager) revision
#                                 path: a sent document is immutable; revise
#                                 branches revision N+1 (same doc number,
#                                 revised_from_id chain, revision_no + 1,
#                                 reason/user/timestamp) which starts
#                                 unlocked and may be sent once more.
#
# Lock rule: a row that has a send attempt (is_locked) is READ-ONLY for
# every customer-facing field; only lifecycle fields (state, acceptance,
# internal-confirm, conversion linkage) may change on it. Nothing except
# action_send_rc ever emails; preview / AI / save / acceptance recording /
# customer-view tracking send nothing. Portal view: no customer RC portal
# page exists yet (blocked on the portal-bridge design) — acceptance is
# staff-recorded; the hooks below are the extension points for a portal.
# ════════════════════════════════════════════════════════════════════

# States in which a row is an editable draft candidate (uniqueness rule:
# a CRM lead has at most ONE open draft — see create() and
# find_or_create_draft_for_lead).
_CQ_OPEN_DRAFT_STATES = ("new", "reviewing", "quoted")
# States after which the lead may start a fresh draft cycle.
_CQ_TERMINAL_STATES = ("declined", "converted")

# Fields whose values make up the customer-facing document. Writing any of
# them on a SENT (locked) row is refused — the only way to change a sent
# Rate Confirmation is the authorized Revise & Resend path.
_CQ_SEND_LOCKED_FIELDS = frozenset((
    "name", "partner_id", "contact_name", "contact_email", "contact_phone",
    "company_name", "source", "notes",
    "pickup_postal_code", "pickup_address",
    "delivery_postal_code", "delivery_address",
    "pallets", "weight_lbs", "temperature_mode", "required_temperature_c",
    "load_type", "departure_id", "commodity", "requested_pickup_date",
    "quoted_price", "system_calculated_price", "manual_price_reason",
    "reason_code",
    "resolved_fsa_pickup", "resolved_fsa_delivery",
    "resolved_region_pickup", "resolved_region_delivery",
    "routing_strategy",
    "crm_lead_id", "idempotency_key",
    "revision_no", "revised_from_id", "revision_reason", "revised_at",
    "revised_by_id", "last_sent_at", "last_sent_attempt_id",
    "revision_changes_summary",
    # §6/§7 (D-B2): identifiers, pricing basis and payment agreement are
    # all customer-facing — locked once sent, like the rest of the
    # document.
    "customer_po", "reference", "bol_number",
    "price_tax_mode",
    "payment_method_id", "payment_term_id",
    "quickpay_apply", "quickpay_discount_pct", "quickpay_deadline_days",
    "quickpay_stack_allowed", "quickpay_override_reason",
))

# Fields compared between a sent revision and its successor — recorded as
# revision_changes_summary and shown as "what changed" on the form.
_CQ_REVISION_DIFF_FIELDS = (
    "quoted_price",
    "requested_pickup_date", "departure_id",
    "pallets", "weight_lbs", "load_type", "temperature_mode",
    "required_temperature_c", "commodity",
    "pickup_postal_code", "pickup_address",
    "delivery_postal_code", "delivery_address",
    "contact_name", "contact_email", "contact_phone",
    # §6/§7 (D-B2): identifier + payment changes are shown to the customer
    # on the revision email ("what changed").
    "customer_po", "reference", "bol_number",
    "price_tax_mode",
    "payment_method_id", "payment_term_id",
    "quickpay_apply", "quickpay_discount_pct", "quickpay_deadline_days",
    "quickpay_stack_allowed",
)

# Context flag used by this module's own internal writes to a locked row
# (send markers). Never set from the UI or by public methods.
_CQ_INTERNAL_WRITE_CTX = "logistics_cq_internal_write"


class LogisticsCustomQuote(models.Model):
    """Quote request for shipments outside automated pricing — manual review."""
    _name = "logistics.custom.quote"
    _description = "Custom Quote Request"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin",
                "logistics.temperature.mixin"]

    name = fields.Char(string="Quote #", readonly=True, copy=False, default="New")
    partner_id = fields.Many2one("res.partner", string="Customer", index=True)
    contact_name = fields.Char(string="Contact Name")
    contact_email = fields.Char(string="Email")
    contact_phone = fields.Char(string="Phone")
    company_name = fields.Char(string="Company")

    # Shipment
    pickup_postal_code = fields.Char(string="Pickup Postal Code")
    pickup_address = fields.Text(string="Pickup Address")
    delivery_postal_code = fields.Char(string="Delivery Postal Code")
    delivery_address = fields.Text(string="Delivery Address")
    pallets = fields.Integer(default=1)
    weight_lbs = fields.Float(string="Weight (lbs)")
    temperature_mode = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")],
        default="dry",
    )
    required_temperature_c = fields.Float(
        string="Required Temperature °C",
        help="Required for Reefer quotes. 0°C is a valid value.",
    )
    load_type = fields.Selection([("ltl", "LTL"), ("ftl", "FTL")], default="ltl")
    departure_id = fields.Many2one(
        "logistics.corridor.departure", string="Assigned Departure",
        help="Exact truck departure this quote will ride on when converted "
             "to a booking. Required to convert — a custom quote has no "
             "automated Rate Plan route to resolve one from.",
    )
    commodity = fields.Char(string="Commodity")
    requested_pickup_date = fields.Date(string="Requested Pickup")
    notes = fields.Text(string="Notes")
    # ── Freight identifiers (§6, D-B2) ────────────────────────────────
    # Three SEPARATE identifiers with distinct owners — never fallbacks
    # for one another:
    #   * customer_po — ONLY the customer-supplied PO; blank when the
    #     customer gave none. Never derived from anything.
    #   * reference   — PremaFirm's stable end-to-end Internal Load
    #     Reference; generated ONCE here (defaults to the RC number when
    #     conversion starts) and then propagated UNCHANGED through the
    #     booking, the dispatch job and the invoice.
    #   * bol_number  — the shipper/carrier BOL, only once the
    #     operational load / BOL is formally created (often after the RC
    #     was sent — backfilled on the booking then).
    customer_po = fields.Char(
        string="Customer PO #", copy=False,
        help="The customer's own purchase order number. Populated ONLY "
             "from a customer-supplied PO — never filled from the load "
             "reference or the BOL. Leave blank when the customer gave no PO.")
    reference = fields.Char(
        string="Internal Load Reference", index=True,
        help="PremaFirm's stable end-to-end Internal Load Reference for "
             "this load (the SAME value appears on the Rate Confirmation, "
             "the booking and the invoice). Never filled from the "
             "customer PO or the BOL.")
    bol_number = fields.Char(
        string="BOL #", copy=False,
        help="Shipper-provided BOL number — entered ONLY when the "
             "operational load / BOL is formally created (usually after "
             "this Rate Confirmation was sent).")
    # ── Pricing basis / payment (§7, D-B2) ────────────────────────────
    price_tax_mode = fields.Selection([
        ("exclusive", "Exclusive of Taxes"),
        ("inclusive", "Inclusive of Taxes"),
    ], string="Quoted Price Is", default="exclusive",
        help="How the Quoted Price is stated: exclusive of applicable "
             "taxes (tax is added on the invoice) or inclusive (the "
             "customer-agreed total already contains the tax). The "
             "invoice preserves the agreed total in both modes.")
    payment_method_id = fields.Many2one(
        "logistics.payment.method", string="Payment Method",
        help="Payment method agreed on this document (e.g. credit card, "
             "Interac e-Transfer). Defaults from the customer profile; "
             "may be overridden per document within the customer's "
             "allowed methods. Carried unchanged onto the booking and the "
             "invoice.")
    payment_term_id = fields.Many2one(
        "account.payment.term", string="Payment Terms",
        help="Terms agreed on this document (due date basis). Defaults "
             "from the customer profile; carried unchanged onto the "
             "booking and the invoice.")
    quickpay_apply = fields.Boolean(
        string="QuickPay Discount Applies", default=False,
        help="Customer opted into the early-payment QuickPay discount on "
             "this document. Disabled by default; only offered when the "
             "customer's profile enables QuickPay (or a booking manager "
             "records a per-document override with a reason).")
    quickpay_discount_pct = fields.Float(
        string="QuickPay Discount %",
        help="Early-payment discount percentage agreed on this document.")
    quickpay_deadline_days = fields.Integer(
        string="QuickPay Deadline (days)", default=7,
        help="The discount is valid when the customer pays within this "
             "many days of the invoice date.")
    quickpay_stack_allowed = fields.Boolean(
        string="QuickPay Stacking Allowed", default=False,
        help="Allow this QuickPay discount to stack with other discounts "
             "(e.g. a manual price adjustment) on the same document. "
             "Off by default — discounts never stack silently.")
    quickpay_override_reason = fields.Char(
        string="QuickPay Override Reason",
        help="Required when QuickPay is applied to a document whose "
             "customer profile does not enable QuickPay (per-document "
             "override). Audited on the chatter.")
    quickpay_discount_amount = fields.Float(
        string="QuickPay Discount Amount", readonly=True,
        compute="_compute_quickpay_amounts",
        help="Original quoted price × QuickPay %.")
    quickpay_discounted_total = fields.Float(
        string="Discounted Total (paid by deadline)", readonly=True,
        compute="_compute_quickpay_amounts",
        help="What the customer pays when paying by the QuickPay "
             "deadline: original balance minus the QuickPay discount.")

    # Resolution
    state = fields.Selection(QUOTE_STATE, default="new", tracking=True)
    quoted_price = fields.Float(string="Quoted Price")
    # ── Manual / negotiated sell price audit ─────────────────────────
    # quoted_price is the FINAL price offered — the customer-facing number
    # (the printed quotation shows only this). system_calculated_price
    # preserves the original pricing-engine result and the manual_price_*
    # fields the audit trail. Staff may edit quoted_price until the quote
    # is converted; a reason is required when it differs from the system
    # price (a reset back to the system price exempts).
    system_calculated_price = fields.Float(
        string="System Calculated Price", readonly=True,
        help="Original pricing-engine sell price — audit only, never shown "
             "to the customer.")
    manual_price_override = fields.Boolean(
        string="Manual Price Override", readonly=True, copy=False)
    manual_price_adjustment = fields.Float(
        string="Manual Price Adjustment", readonly=True,
        compute="_compute_manual_price_adjustment",
        help="Quoted price minus system calculated price "
             "(negative = discount).")
    manual_price_reason = fields.Char(string="Manual Price Reason")
    manual_price_changed_by = fields.Many2one(
        "res.users", string="Price Changed By", readonly=True)
    manual_price_changed_at = fields.Datetime(
        string="Price Changed At", readonly=True)
    internal_notes = fields.Text(string="Internal Notes")
    reason_code = fields.Char(string="Reason", help="Why this required manual quoting.")

    # Resolution fields
    resolved_fsa_pickup = fields.Char(string="Resolved Pickup FSA")
    resolved_fsa_delivery = fields.Char(string="Resolved Delivery FSA")
    resolved_region_pickup = fields.Many2one("logistics.region")
    resolved_region_delivery = fields.Many2one("logistics.region")
    routing_strategy = fields.Char()

    # Converted booking
    booking_id = fields.Many2one("logistics.booking", string="Converted Booking", readonly=True)
    estimator_id = fields.Many2one("premafirm.rate.estimator", string="Estimator", readonly=True)
    crm_lead_id = fields.Many2one(
        "crm.lead",
        string="CRM Opportunity",
        index=True,
        ondelete="set null",
        copy=False,
        help="Opportunity that originated this rate confirmation.",
    )

    # Source
    source = fields.Selection([
        ("website", "Website"),
        ("phone", "Phone"),
        ("email", "Email"),
        ("internal", "Internal"),
    ], default="website")

    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    currency_id = fields.Many2one("res.currency", related="company_id.currency_id", readonly=True)
    quotation_valid_until = fields.Date(
        string="Quotation Valid Until",
        compute="_compute_quotation_valid_until",
        help="30 days after the quote was created — shown on the printed quotation.",
    )

    # ════════════════════════════════════════════════════════════════
    # Rate Confirmation lifecycle fields (§3.7/§4 productization)
    # ════════════════════════════════════════════════════════════════
    # Exactly-one-draft / idempotency: callers that want "the lead's RC
    # draft" use find_or_create_draft_for_lead; the key is stored here so
    # repeated engine-side calls (wave 2) resolve to the same row.
    idempotency_key = fields.Char(
        string="Idempotency Key", index=True, copy=False,
        help="Caller-supplied key recorded at draft creation so a repeated "
             "create request returns the same record instead of stacking "
             "parallel drafts.")
    # Send state — a row with a send attempt is LOCKED (read-only doc).
    send_attempt_ids = fields.One2many(
        "logistics.custom.quote.send.attempt", "cq_id",
        string="Send Attempts", readonly=True)
    is_locked = fields.Boolean(
        string="Sent (locked)", compute="_compute_lifecycle_flags",
        help="A send attempt exists for this document: the customer has "
             "been emailed this revision, so every customer-facing field "
             "is read-only. Only the authorized Revise & Resend path may "
             "produce a new revision.")
    is_superseded = fields.Boolean(
        string="Superseded", compute="_compute_lifecycle_flags",
        help="A newer revision of this document exists — never send or "
             "convert this row; work on the newest revision.")
    last_sent_at = fields.Datetime(string="Last Sent At", readonly=True)
    last_sent_attempt_id = fields.Many2one(
        "logistics.custom.quote.send.attempt", string="Last Send",
        readonly=True, ondelete="set null")
    # Customer acceptance recorded out-of-band by staff (no customer
    # portal page for this document exists yet — blocked on the
    # portal-bridge design; action_record_customer_acceptance* is the
    # extension point a future portal accept button must reuse, and it
    # records acceptance ONLY — it never confirms internally or emails).
    acceptance_channel = fields.Selection([
        ("email", "Email"),
        ("portal", "Portal"),
        ("phone", "Phone"),
        ("text", "Text"),
    ], string="Acceptance Channel",
        help="How the customer's acceptance was received "
             "(email / portal / phone / text). Required before the "
             "acceptance can be recorded.")
    acceptance_recorded_at = fields.Datetime(
        string="Acceptance Recorded At", readonly=True)
    acceptance_recorded_by = fields.Many2one(
        "res.users", string="Acceptance Recorded By", readonly=True)
    # Staff-side GO decision (internal only, never customer-visible).
    internal_confirmed = fields.Boolean(
        string="Internally Confirmed", readonly=True, tracking=True,
        help="Staff-side GO decision: the deal is commercial/committed "
             "internally. Recorded by action_confirm_internally.")
    internal_confirmed_at = fields.Datetime(
        string="Internally Confirmed At", readonly=True)
    internal_confirmed_by = fields.Many2one(
        "res.users", string="Internally Confirmed By", readonly=True)
    # Booking-manager override of the internal-confirm gate (must carry a
    # reason; only group_logistics_booking_manager may use it).
    conversion_override = fields.Boolean(
        string="Convert Without Internal Confirmation")
    conversion_override_reason = fields.Char(
        string="Override Reason",
        help="Required when converting before internal confirmation — "
             "manager-authorized exception, audited.")
    conversion_override_by = fields.Many2one(
        "res.users", string="Override By", readonly=True)
    conversion_override_at = fields.Datetime(
        string="Override At", readonly=True)
    # Revision history (revisions are NEW rows branching off a sent one).
    revision_no = fields.Integer(
        string="Revision", default=1, readonly=True,
        help="Document revision. Revision 1 is the original; each "
             "authorized Revise & Resend branches revision N+1 with the "
             "same document number.")
    revised_from_id = fields.Many2one(
        "logistics.custom.quote", string="Revised From", readonly=True,
        index=True, ondelete="set null",
        help="The sent revision this row was revised from — its immutable "
             "send/print artifacts (attempt rows, markers, PDF source "
             "values) remain untouched on that row.")
    revision_reason = fields.Text(string="Revision Reason", readonly=True)
    revised_at = fields.Datetime(string="Revised At", readonly=True)
    revised_by_id = fields.Many2one(
        "res.users", string="Revised By", readonly=True)
    revision_changes_summary = fields.Json(
        string="Revision Changes", readonly=True,
        help="JSON diff of the customer-facing document fields between the "
             "previous sent revision and this one — frozen at send.")
    revision_changes_html = fields.Html(
        string="What Changed", compute="_compute_revision_changes_html",
        help="Shows the differences between the previous sent revision and "
             "this one (live while editing, frozen once sent).")

    # ── Customer-view tracking (extension point — R7) ────────────────
    # No portal page exists for this document yet (portal-bridge design
    # pending). When one is built, the view route must ONLY touch these
    # fields (pure read + timestamp, no commercial mutation) and any
    # accept button must call action_record_customer_acceptance — never
    # confirm internally and never email.
    viewed_by_customer_at = fields.Datetime(
        string="Last Viewed By Customer", readonly=True)
    viewed_by_customer_count = fields.Integer(
        string="Customer Views", readonly=True, default=0)

    @api.depends("create_date")
    def _compute_quotation_valid_until(self):
        for record in self:
            base = record.create_date or fields.Datetime.now()
            record.quotation_valid_until = (base + datetime.timedelta(days=30)).date()

    @api.depends("send_attempt_ids")
    def _compute_lifecycle_flags(self):
        """is_locked: this exact row has a send attempt (was emailed once).
        is_superseded: a newer revision row branched off this one."""
        locked_ids = {a.cq_id.id for a in self.sudo().send_attempt_ids}
        superseding = self.search([("revised_from_id", "in", self.ids)]) \
            if self.ids else self.browse()
        superseded_ids = set(superseding.mapped("revised_from_id").ids)
        for rec in self:
            rec.is_locked = rec.id in locked_ids
            rec.is_superseded = rec.id in superseded_ids

    @api.depends("revision_changes_summary", "revised_from_id",
                 "quoted_price", "requested_pickup_date", "departure_id",
                 "pallets", "weight_lbs", "load_type", "temperature_mode",
                 "required_temperature_c", "commodity",
                 "pickup_postal_code", "pickup_address",
                 "delivery_postal_code", "delivery_address",
                 "contact_name", "contact_email", "contact_phone",
                 "customer_po", "reference", "bol_number",
                 "price_tax_mode", "payment_method_id", "payment_term_id",
                 "quickpay_apply", "quickpay_discount_pct",
                 "quickpay_deadline_days", "quickpay_stack_allowed")
    def _compute_revision_changes_html(self):
        for rec in self:
            if not rec.revised_from_id:
                rec.revision_changes_html = False
                continue
            frozen = bool(rec.revision_changes_summary)
            diff = rec.revision_changes_summary or \
                rec._revision_diff_payload(rec.revised_from_id)
            if frozen:
                heading = _(
                    "Revision %s of %s — sent on %s") % (
                    rec.revision_no, rec.revised_from_id.name,
                    (rec.last_sent_at or fields.Datetime.now())
                    .strftime("%Y-%m-%d %H:%M"))
            else:
                heading = _(
                    "Revision %s of %s — changes vs the sent revision "
                    "(not sent yet)") % (rec.revision_no,
                                         rec.revised_from_id.name)
            rec.revision_changes_html = \
                "<div><strong>%s</strong>%s</div>" % (
                    html.escape(heading), rec._diff_to_html(diff))

    @api.depends("quoted_price", "system_calculated_price")
    def _compute_manual_price_adjustment(self):
        for rec in self:
            rec.manual_price_adjustment = round(
                (rec.quoted_price or 0.0) - (rec.system_calculated_price or 0.0), 2)

    @api.depends("quoted_price", "quickpay_apply", "quickpay_discount_pct")
    def _compute_quickpay_amounts(self):
        """QuickPay numbers shown on the document: the discount (original
        balance × %) and what the customer actually pays by the deadline.
        The original balance itself (quoted_price) is never overwritten —
        QuickPay is an early-payment incentive, not a reprice."""
        for rec in self:
            if not rec.quickpay_apply or not rec.quickpay_discount_pct:
                rec.quickpay_discount_amount = 0.0
                rec.quickpay_discounted_total = 0.0
                continue
            discount = round(
                (rec.quoted_price or 0.0) * rec.quickpay_discount_pct / 100.0, 2)
            rec.quickpay_discount_amount = discount
            rec.quickpay_discounted_total = round(
                (rec.quoted_price or 0.0) - discount, 2)

    # ════════════════════════════════════════════════════════════════
    # §7 (D-B2) payment/QuickPay validation — every change is tracked
    # (tracking=True fields above → chatter audit trail).
    # ════════════════════════════════════════════════════════════════

    @api.onchange("partner_id")
    def _onchange_partner_payment_defaults(self):
        if not self.partner_id:
            return
        defaults = self.partner_id._logistics_payment_defaults()
        for key, value in defaults.items():
            setattr(self, key, value)

    def _check_quickpay_configuration(self, vals, partner=None):
        """Cross-field QuickPay guards shared by create() and write().

        Raises UserError when QuickPay would silently stack with another
        discount, or when a per-document override is used without the
        recorded reason. Returns vals (possibly completed with the
        stacking flag from the customer profile).
        """
        if self:
            partner = partner or self.partner_id
            quickpay_was_on = self.quickpay_apply
        else:
            partner = partner or self.env["res.partner"]
            quickpay_was_on = False
        if "quickpay_apply" in vals:
            quickpay_on = bool(vals.get("quickpay_apply"))
        else:
            quickpay_on = quickpay_was_on
        if not quickpay_on:
            # QuickPay off (or untouched) — no guard applies; a write that
            # merely clears an old quickpay_override_reason is harmless.
            return vals
        turning_on = quickpay_on and not quickpay_was_on
        if turning_on:
            pct = vals.get(
                "quickpay_discount_pct",
                self.quickpay_discount_pct if self else 0.0) or 0.0
            if pct <= 0:
                raise UserError(_(
                    "QuickPay is on but no discount percentage is set — "
                    "set the QuickPay Discount %% above 0."))
            if not partner.x_logistics_quickpay_enabled:
                reason = (vals.get("quickpay_override_reason")
                          or (self.quickpay_override_reason if self else "")
                          or "")
                if not (reason or "").strip():
                    raise UserError(_(
                        "Customer %s is not QuickPay-eligible. A "
                        "per-document override needs a recorded QuickPay "
                        "Override Reason (audit trail).") %
                        (partner.name or ""))
                if not vals.get("quickpay_override_reason") and reason:
                    vals = dict(vals, quickpay_override_reason=reason)
        # Stacking guard: a discounted price (manual adjustment below the
        # system price) plus QuickPay = two discounts. Never silent.
        stack_allowed = vals.get("quickpay_stack_allowed") \
            if "quickpay_stack_allowed" in vals \
            else (self.quickpay_stack_allowed if self else False)
        if not stack_allowed and partner.x_logistics_quickpay_stack_allowed:
            vals = dict(vals, quickpay_stack_allowed=True)
            stack_allowed = True
        quoted = (vals.get("quoted_price") if "quoted_price" in vals
                  else (self.quoted_price if self else 0.0)) or 0.0
        system = (vals.get("system_calculated_price")
                  if "system_calculated_price" in vals
                  else (self.system_calculated_price if self else 0.0)) or 0.0
        if system and quoted and round(quoted, 2) < round(system, 2) \
                and not stack_allowed:
            raise UserError(_(
                "QuickPay may not stack with the manual price adjustment "
                "on this document. Either clear QuickPay, restore the "
                "system price, or explicitly allow stacking (QuickPay "
                "Stacking Allowed on the document or the customer)."))
        return vals

    def _check_payment_method_allowed(self, vals):
        """The per-document payment-method override must stay within the
        customer's allowed methods (contractual boundary)."""
        if "payment_method_id" not in vals or not vals.get("payment_method_id"):
            return
        partner = self.partner_id if self else self.env["res.partner"]
        if not partner:
            return
        allowed = partner.x_logistics_allowed_payment_method_ids
        if not allowed:
            return
        if vals["payment_method_id"] not in allowed.ids:
            raise UserError(_(
                "Payment method is not in the allowed methods of customer "
                "%s. Allowed: %s.") % (
                partner.name,
                ", ".join(allowed.mapped("name")) or "—"))

    def _require_manual_price_reason(self):
        """The Manual Price Reason policy, enforced where the price COMMITS.

        A reason is required only when the pricing engine actually priced
        this quote AND the human's figure departs from that figure. With no
        engine price there is nothing to deviate from: the first real
        figure a human enters IS the quote, not an override of one, so
        nothing is demanded. 0.0 means "never priced" — freight is never
        free — the same reading `_check_quickpay_configuration` uses.

        This is a Send / Convert gate and NEVER a draft gate. Opening,
        editing, saving, and CRM-side draft creation are the work a draft
        exists for and are never blocked by it.
        """
        for rec in self:
            system = rec.system_calculated_price or 0.0
            if not system or not rec.quoted_price:
                continue
            if round(rec.quoted_price, 2) == round(system, 2):
                continue
            if (rec.manual_price_reason or "").strip():
                continue
            if rec.currency_id:
                quoted = rec.currency_id.format(rec.quoted_price)
                system_text = rec.currency_id.format(system)
            else:
                quoted = "%.2f" % rec.quoted_price
                system_text = "%.2f" % system
            raise UserError(_(
                "A Manual Price Reason is required when the quoted price "
                "differs from the system calculated price: this Rate "
                "Confirmation (%(name)s) quotes %(quoted)s against a system "
                "price of %(system)s. Record the reason on the document, "
                "then retry — nothing was sent, confirmed or booked.") % {
                "name": rec.name,
                "quoted": quoted,
                "system": system_text,
            })

    # ════════════════════════════════════════════════════════════════
    # create/write — single-draft rule + send-lock guard
    # ════════════════════════════════════════════════════════════════

    @api.model_create_multi
    def create(self, vals_list):
        # §3.7 exactly-one-open-draft: refuse to open a SECOND editable
        # draft for the same lead through the normal ORM/UI/RPC surface.
        # Authorized internal staff flows (phone wizard, website request,
        # find_or_create_draft_for_lead) run sudo and keep their existing
        # semantics; the public API boundary is where stacking drafts
        # would otherwise happen.
        if not (self.env.su or self.env.context.get(_CQ_INTERNAL_WRITE_CTX)):
            by_lead = {}
            for vals in vals_list:
                lead = vals.get("crm_lead_id")
                state = vals.get("state", "new")
                if lead and state in _CQ_OPEN_DRAFT_STATES:
                    by_lead.setdefault(lead, []).append(vals)
            for lead, draft_vals in by_lead.items():
                existing = self.search([
                    ("crm_lead_id", "=", lead),
                    ("state", "in", list(_CQ_OPEN_DRAFT_STATES)),
                    ("booking_id", "=", False),
                ])
                existing = self._filter_open_drafts(existing)
                if existing:
                    raise UserError(_(
                        "This CRM opportunity already has an open draft "
                        "Rate Confirmation (%s). Reuse it or use the "
                        "authorized Revise & Resend path — parallel drafts "
                        "for one opportunity are not allowed.") %
                        existing[0].name)
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("logistics.custom.quote") or "CQ-0001"
            # §7 (D-B2): partner payment/QuickPay defaults fill ONLY the
            # keys the caller did not already pick (per-key, not
            # all-or-nothing): a caller that set the method or the
            # stacking flag alone still inherits the profile's QuickPay
            # offer, while an explicitly chosen value always wins.
            if vals.get("partner_id"):
                partner = self.env["res.partner"].browse(vals["partner_id"])
                vals.update({
                    key: value for key, value in
                    partner._logistics_payment_defaults().items()
                    if key not in vals
                })
            # §7 guards (raise before any row is created).
            partner = self.env["res.partner"].browse(
                vals.get("partner_id") or False)
            vals = self._check_quickpay_configuration(vals, partner=partner)
            self._check_payment_method_allowed(vals)
        return super().create(vals_list)

    def write(self, vals):
        """Send-lock guard + the manual quoted-price audit.

        A SENT (locked) Rate Confirmation is immutable for every
        customer-facing field: only the lifecycle fields (state,
        acceptance, internal confirmation, conversion linkage) may change
        on it. Anything else must go through the authorized Revise &
        Resend path, which branches a NEW revision instead of rewriting
        the emailed document.
        """
        if not self.env.context.get(_CQ_INTERNAL_WRITE_CTX):
            touched_protected = _CQ_SEND_LOCKED_FIELDS & vals.keys()
            if touched_protected:
                for rec in self:
                    if rec.is_locked:
                        raise UserError(_(
                            "This Rate Confirmation (%s) was already sent "
                            "to the customer on %s and is locked. Its "
                            "customer-facing fields may not be edited "
                            "anymore — use \"Revise & Resend\" (booking "
                            "managers) to create revision %s with the new "
                            "terms; the sent revision is never rewritten.") %
                            (rec.name,
                             rec.last_sent_at.strftime("%Y-%m-%d %H:%M")
                             if rec.last_sent_at else "—",
                             (rec.revision_no or 1) + 1))
        # §7 (D-B2) guards: QuickPay stacking/override rules and the
        # allowed-methods boundary are enforced on every write (tracking
        # fields provide the audit trail).
        if not self.env.context.get(_CQ_INTERNAL_WRITE_CTX):
            for rec in self:
                vals = rec._check_quickpay_configuration(
                    vals, partner=rec.partner_id)
                rec._check_payment_method_allowed(vals)
        changed = []
        for rec in self:
            if "quoted_price" in vals and vals.get("quoted_price") is not None \
                    and round(float(vals.get("quoted_price")), 2) != round(rec.quoted_price or 0.0, 2):
                new_price = float(vals.get("quoted_price"))
                old_price = rec.quoted_price or 0.0
                # The engine's figure is read as-is and is NEVER backfilled
                # from the record's own previous quoted price. That backfill
                # made the "system" price the human's own last keystroke, so
                # an unpriced draft (system 0.00) was permanently
                # unpriceable: the first figure a human typed "differed
                # from" a system price that had never existed. 0.0 here
                # means "the engine never priced this quote", not "priced
                # at zero" — freight is never free.
                system = (vals.get("system_calculated_price")
                          if "system_calculated_price" in vals
                          else rec.system_calculated_price) or 0.0
                if rec.state == "converted":
                    raise UserError(_(
                        "This quotation is already converted to a booking — "
                        "the final quoted price is frozen. Adjust the "
                        "booking's customer sell price instead."))
                # Entering or editing the price on a draft is the very work
                # the draft exists for, so nothing is refused here. The
                # Manual Price Reason is enforced where the figure becomes
                # consequential — Send and Convert — by
                # `_require_manual_price_reason`.
                changed.append((rec, old_price, new_price, system))
        # §6/§7 (D-B2): identifiers + payment agreement are NEVER changed
        # silently on the customer document — every change is posted on
        # the chatter (deterministic row, mirror of the sell-price audit;
        # the fields carry no tracking= so no duplicate row appears at
        # transaction commit).
        agreement_fields = {
            "price_tax_mode": "Price Is",
            "payment_method_id": "Payment Method",
            "payment_term_id": "Payment Terms",
            "quickpay_apply": "QuickPay Discount Applies",
            "quickpay_discount_pct": "QuickPay Discount %",
            "quickpay_deadline_days": "QuickPay Deadline (days)",
            "quickpay_stack_allowed": "QuickPay Stacking Allowed",
            "quickpay_override_reason": "QuickPay Override Reason",
            "customer_po": "Customer PO",
            "reference": "Internal Load Reference",
            "bol_number": "BOL #",
        }
        agreement_changes = []
        for rec in self:
            for fname, label in agreement_fields.items():
                if fname not in vals:
                    continue
                new_value = vals.get(fname)
                old_value = getattr(rec, fname)
                if fname.endswith("_id"):
                    old_value = old_value.id if old_value else False
                if bool(new_value) != bool(old_value) or \
                        (new_value is not None and str(new_value).strip()
                         != str(old_value or "").strip()):
                    agreement_changes.append((rec, label, old_value, new_value))
        result = super().write(vals)
        for rec, old_price, new_price, system in changed:
            rec.write({
                # "Override" means the engine priced it and a human departed
                # from that figure: there is nothing to override when the
                # engine never priced the quote. Keeps the flag exactly
                # aligned with the Send/Convert gate below — flag set ⇔ the
                # gate will demand a reason.
                "manual_price_override": bool(system) and round(new_price, 2) != round(system, 2),
                "manual_price_changed_by": self.env.user.id,
                "manual_price_changed_at": fields.Datetime.now(),
            })
            rec.message_post(body=self.env["logistics.booking"]._sell_price_audit_message(
                old_price, new_price, rec.manual_price_reason or "",
                self.env.user.name or ""))
        for rec, label, old_value, new_value in agreement_changes:
            rec.message_post(
                body=self.env["logistics.booking"]._payment_change_audit_message(
                    label, old_value, new_value, self.env.user.name or ""))
        return result

    # ════════════════════════════════════════════════════════════════
    # Draft discovery — R1 (§3.7)
    # ════════════════════════════════════════════════════════════════

    @api.model
    def _prepare_from_lead(self, lead, idempotency_key=None):
        """Payload mapping lead → populated draft RC (wave-2 engine callers
        may write further shipment/pricing fields before saving)."""
        partner = lead.partner_id
        vals = {
            "crm_lead_id": lead.id,
            "partner_id": partner.id if partner else False,
            "source": "internal",
            "state": "new",
        }
        if partner:
            vals.update({
                "company_name": partner.commercial_partner_id.name or partner.name,
                "contact_name": partner.name,
                "contact_email": partner.email or "",
                "contact_phone": partner.phone or partner.mobile or "",
            })
        else:
            vals.update({
                "contact_name": lead.contact_name or lead.name or "",
                "contact_email": lead.email_from or "",
                "contact_phone": lead.phone or "",
            })
        # §6 (D-B2): the ONLY sanctioned PO source is the customer-supplied
        # PO recorded on the lead ("Customer PO #"). It is never derived
        # from the reference/BOL or anything else. QuickPay defaults come
        # from the partner profile and stay OFF when the customer is not
        # eligible.
        lead_po = getattr(lead, "po_number", "") or ""
        vals["customer_po"] = (lead_po or "").strip()
        if partner:
            vals.update(
                partner._logistics_payment_defaults())
        if idempotency_key:
            vals["idempotency_key"] = idempotency_key
        return vals

    @api.model
    def find_or_create_draft_for_lead(self, lead_id, idempotency_key=None):
        """Return the lead's current Rate Confirmation row (serial
        pipeline: a lead has at most one active, discoverable draft).

        - While the lead has any non-terminal row (new/reviewing/quoted/
          accepted), THAT row is returned — repeated calls return the same
          record and can never stack parallel drafts.
        - A caller-supplied idempotency_key is stored on the created row;
          a matching row is returned as a strict-idempotency fallback for
          callers whose lead already terminalized.
        - Only after the previous row reached a terminal state (declined /
          converted) does a NEW draft get created.
        """
        lead = self.env["crm.lead"].browse(lead_id)
        if not lead.exists():
            raise UserError(_("CRM opportunity %s not found.") % lead_id)
        # Serialize draft creation per lead — two near-simultaneous calls
        # cannot both create (the second re-reads after the first commits).
        self.env.cr.execute(
            "SELECT id FROM crm_lead WHERE id = %s FOR UPDATE", (lead_id,))
        self.invalidate_recordset()
        current = self.search([
            ("crm_lead_id", "=", lead_id),
            ("state", "not in", list(_CQ_TERMINAL_STATES)),
        ], order="id desc", limit=1)
        if current:
            return current
        if idempotency_key:
            by_key = self.search([
                ("idempotency_key", "=", idempotency_key),
            ], limit=1)
            if by_key:
                return by_key
        return self.create(
            self._prepare_from_lead(lead, idempotency_key=idempotency_key))

    @api.model
    def _filter_open_drafts(self, records):
        """Keep only rows that are genuinely open drafts: no send attempt
        (never emailed → not locked) and no successor revision."""
        if not records:
            return records
        attempt_model = self.env["logistics.custom.quote.send.attempt"].sudo()
        locked = set(attempt_model.search(
            [("cq_id", "in", records.ids)]).mapped("cq_id").ids)
        superseded = set(self.search(
            [("revised_from_id", "in", records.ids)]).mapped("revised_from_id").ids)
        return records.filtered(
            lambda r: r.id not in locked and r.id not in superseded)

    # ════════════════════════════════════════════════════════════════
    # R3 (§4.2) — PREVIEW IS A PURE READ
    # ════════════════════════════════════════════════════════════════

    def action_preview(self):
        """Preview the Rate Confirmation document. PURE READ: renders the
        quotation PDF only — provably no send-attempt row, no mail, no
        marker, no state change, no commercial write of any kind."""
        self.ensure_one()
        return self.action_print_quotation()

    def action_print_quotation(self):
        """Print the PDF quotation document for this quote."""
        self.ensure_one()
        return self.env.ref(
            "prema_logistics_booking.action_report_logistics_quotation"
        ).report_action(self)

    # ════════════════════════════════════════════════════════════════
    # R4/R6 (§4.3-4.6) — EXPLICIT SEND, ONE-SHOT, IDEMPOTENT
    # ════════════════════════════════════════════════════════════════

    def _require_current_revision(self):
        if self.is_superseded:
            raise UserError(_(
                "A newer revision of this Rate Confirmation exists — this "
                "sent revision may not be acted on anymore. Work on the "
                "newest revision."))

    def _render_quotation_pdf(self):
        """Render the customer quotation PDF for this row (pure compute)."""
        self.ensure_one()
        report = self.env.ref(
            "prema_logistics_booking.action_report_logistics_quotation")
        content, _filetype = report.sudo()._render_qweb_pdf(self.ids)
        return content

    def _build_send_payload(self):
        """Subject/body/recipients for the customer RC email — shows only
        the customer-facing numbers (never system_calculated_price)."""
        self.ensure_one()
        contact = self.contact_name or self.partner_id.name or ""
        email = (self.partner_id.email or self.contact_email or "").strip()
        if not email:
            raise UserError(_(
                "This Rate Confirmation has no customer email address — set "
                "one on the Customer (or Contact Email) before sending."))
        price = self.currency_id.format(self.quoted_price) \
            if self.currency_id else "%.2f" % self.quoted_price
        revision_label = "" if self.revision_no <= 1 \
            else _(" — Revision %s") % self.revision_no
        dep_date = self.departure_id.departure_date \
            or self.requested_pickup_date
        rows = [
            (_("Pickup"), self.pickup_address or self.pickup_postal_code or "—"),
            (_("Delivery"), self.delivery_address or self.delivery_postal_code or "—"),
            (_("Requested Pickup"), dep_date or "—"),
            (_("Equipment"), dict(self._fields["temperature_mode"].selection)
                .get(self.temperature_mode, self.temperature_mode)),
            (_("Pallets"), self.pallets),
            (_("Weight (lbs)"), self.weight_lbs or "—"),
        ]
        # §6 (D-B2): identifiers on the customer email.
        if self.reference:
            rows.append((_("Internal Load Reference"), self.reference))
        if self.customer_po:
            rows.append((_("Customer PO"), self.customer_po))
        # §7 (D-B2): pricing basis + payment agreement on the email.
        rows.append((
            _("Price Is"),
            dict(self._fields["price_tax_mode"].selection)
            .get(self.price_tax_mode or "exclusive", "—")))
        if self.payment_method_id:
            rows.append((_("Payment Method"), self.payment_method_id.name))
        if self.payment_term_id:
            rows.append((_("Payment Terms"), self.payment_term_id.name))
        if self.quickpay_apply and self.quickpay_discount_pct:
            rows.append((_("QuickPay Discount (paid by deadline)"),
                         "%s%% — %s" % (
                             self.quickpay_discount_pct,
                             self.currency_id.format(
                                 self.quickpay_discounted_total)
                             if self.currency_id
                             else "%.2f" % self.quickpay_discounted_total)))
        rows += [
            (_("Total Price"), price),
            (_("Valid Until"), self.quotation_valid_until or "—"),
        ]
        body = "<p>%s</p><p>%s</p><table>" % (
            html.escape(_("Dear %s,") % (contact or _("Customer"))),
            html.escape(_("Please find your Rate Confirmation %s attached.%s") %
                        (self.name, revision_label)))
        for label, value in rows:
            body += "<tr><td><strong>%s</strong></td><td>%s</td></tr>" % (
                html.escape(str(label)), html.escape(str(value)))
        body += "</table><p>%s</p>" % html.escape(_(
            "If you have any questions about this Rate Confirmation, "
            "please reply to this email."))
        subject = _("Rate Confirmation %s%s") % (self.name, revision_label)
        return {
            "subject": subject,
            "body_html": body,
            "email": email,
            "contact": contact,
            "revision_label": revision_label,
        }

    def action_send_rc(self):
        """EXPLICIT one-shot send of the Rate Confirmation to the customer.

        Idempotency contract (mirrors the engine confirmation-email guard):
          * FOR UPDATE row lock on the quote;
          * refuses when any send-attempt row exists for this row — a
            repeated click/retry can never email twice, and the sent
            marker (last_sent_at / last_sent_attempt_id) is written BEFORE
            the mail is queued;
          * each send records an immutable
            logistics.custom.quote.send.attempt row (state, timestamp,
            revision, template/report, mail.mail id, fingerprint);
          * intentional resend = ONLY the authorized Revise & Resend path
            (new revision → new attempt row → its own single send).

        Nothing else — create, edit, save, preview, AI actions, acceptance
        recording, customer viewing — ever sends.
        """
        self.ensure_one()
        if self.state not in ("quoted", "accepted"):
            raise UserError(_(
                "Only a priced Rate Confirmation (state Quoted or Accepted) "
                "can be sent. This document is in state \"%s\".") %
                dict(QUOTE_STATE).get(self.state, self.state))
        if self.booking_id:
            raise UserError(_(
                "This Rate Confirmation is already converted to a booking — "
                "it can no longer be sent."))
        if not self.quoted_price:
            raise UserError(_(
                "Set the quoted price before sending the Rate Confirmation."))
        # The manual-price policy gate: this is the moment the figure
        # becomes customer-facing. A draft was never blocked for it.
        self._require_manual_price_reason()
        self._require_current_revision()
        payload = self._build_send_payload()

        # ── Row lock + marker check: two rapid clicks cannot both pass ──
        self.env.cr.execute(
            "SELECT id FROM logistics_custom_quote WHERE id = %s FOR UPDATE",
            (self.id,))
        self.invalidate_recordset()
        if self.send_attempt_ids:
            last = self.send_attempt_ids[0]
            raise UserError(_(
                "The Rate Confirmation %s was already sent to the customer "
                "on %s (revision %s). No duplicate email was sent. If the "
                "customer needs different terms, use \"Revise & Resend\" — "
                "the new revision is sent exactly once.") % (
                self.name,
                last.sent_at.strftime("%Y-%m-%d %H:%M")
                if last.sent_at else "—",
                last.revision_no or 1))

        pdf_content = self._render_quotation_pdf()

        # Diff against the revision this row was branched from — frozen
        # here as the revision_changes_summary for this new revision.
        summary = False
        if self.revised_from_id:
            summary = self._revision_diff_payload(self.revised_from_id) or {}

        fp = hashlib.sha1()
        fp.update(json.dumps({
            f: self._field_readable_value(f) for f in _CQ_REVISION_DIFF_FIELDS
        }, sort_keys=True, default=str).encode("utf-8"))
        fp.update(("::%s::%s" % (self.id, self.revision_no)).encode("utf-8"))
        send_hash = fp.hexdigest()

        mail = self.env["mail.mail"].sudo().create({
            "subject": payload["subject"],
            "body_html": payload["body_html"],
            "email_from": (self.company_id.email
                           or self.env.company.email
                           or self.env.user.email or "").strip()
                          or "noreply@localhost",
            "email_to": payload["email"],
            "attachment_ids": [(0, 0, {
                "name": "%s.pdf" % self.name,
                "raw": pdf_content,
                "mimetype": "application/pdf",
            })],
        })
        # Marker + immutable attempt row BEFORE the mail is sent/queued.
        attempt = self.env["logistics.custom.quote.send.attempt"].create({
            "cq_id": self.id,
            "state": "sent",
            "revision_no": self.revision_no,
            "template_ref": "prema_logistics_booking.action_report_logistics_quotation",
            "mail_mail_id": mail.id,
            "send_hash": send_hash,
        })
        self.with_context(_CQ_INTERNAL_WRITE_CTX=True).write({
            "last_sent_at": fields.Datetime.now(),
            "last_sent_attempt_id": attempt.id,
            "revision_changes_summary": summary,
        })
        try:
            mail.send()
        except Exception:
            # Roll back the "sent" attempt row to failed; the marker on
            # the quote rolls back with the transaction — a later retry
            # may send again (nothing was queued).
            try:
                attempt.with_context(
                    _CQ_ATTEMPT_INTERNAL_UPDATE_CTX=True
                ).write({"state": "failed"})
            except Exception:
                pass
            raise
        if mail.message_id:
            try:
                attempt.with_context(
                    _CQ_ATTEMPT_INTERNAL_UPDATE_CTX=True
                ).write({"provider_message_id": mail.message_id})
            except Exception:
                pass
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("Rate Confirmation Sent"),
                "message": _("%s was emailed to %s — recorded as send "
                             "attempt %s (revision %s).") % (
                    self.name, payload["email"], attempt.id,
                    self.revision_no),
                "type": "success", "sticky": False,
            },
        }

    # ════════════════════════════════════════════════════════════════
    # R4 (§4.3) — RECORD CUSTOMER ACCEPTANCE (out-of-band, staff-side)
    # ════════════════════════════════════════════════════════════════

    def action_record_customer_acceptance_form(self):
        """Form-friendly variant — uses the acceptance-channel selection on
        the form (the channel is REQUIRED for the audit trail)."""
        self.ensure_one()
        return self.action_record_customer_acceptance(
            self.acceptance_channel)

    def action_record_customer_acceptance(self, channel=None):
        """Staff records that the customer accepted the quoted rate
        out-of-band (email / portal / phone / text). Records acceptance
        ONLY: no internal confirmation, no email, no booking."""
        self.ensure_one()
        if not channel:
            raise UserError(_(
                "Select how the customer's acceptance was received "
                "(Acceptance Channel: email / portal / phone / text) "
                "before recording it."))
        if channel not in dict(
                self._fields["acceptance_channel"].selection):
            raise UserError(_("Unknown acceptance channel %r.") % channel)
        if self.state not in ("quoted", "accepted"):
            raise UserError(_(
                "Only a priced Rate Confirmation (state Quoted or "
                "Accepted) can record customer acceptance."))
        if not self.quoted_price:
            raise UserError(_(
                "Set the quoted price before recording customer acceptance."))
        self._require_current_revision()
        now = fields.Datetime.now()
        self.write({
            "acceptance_channel": channel,
            "acceptance_recorded_at": now,
            "acceptance_recorded_by": self.env.user.id,
            "state": "accepted",
        })
        self.message_post(
            body=_("Customer acceptance recorded — channel: %s.") % channel,
            subtype_xmlid="mail.mt_note")
        return True

    # Legacy alias kept for API compatibility — same explicit semantics.
    def action_accept(self):
        return self.action_record_customer_acceptance_form()

    # ════════════════════════════════════════════════════════════════
    # R4 (§4.5) — CONFIRM INTERNALLY (staff-side GO decision)
    # ════════════════════════════════════════════════════════════════

    def action_confirm_internally(self):
        """Staff-side GO decision: the deal is commercial/committed.
        Internal only — never emailed, never customer-visible. Idempotent."""
        self.ensure_one()
        if self.state != "accepted":
            raise UserError(_(
                "Record the customer's acceptance first — internal "
                "confirmation confirms the accepted deal."))
        if self.booking_id:
            raise UserError(_(
                "This Rate Confirmation is already converted to a booking."))
        self._require_current_revision()
        if self.internal_confirmed:
            return True
        now = fields.Datetime.now()
        self.write({
            "internal_confirmed": True,
            "internal_confirmed_at": now,
            "internal_confirmed_by": self.env.user.id,
        })
        self.message_post(
            body=_("Rate Confirmation confirmed internally."),
            subtype_xmlid="mail.mt_note")
        return True

    # ════════════════════════════════════════════════════════════════
    # R4 (§4.5) — CONVERT ACCEPTED RC TO BOOKING (exactly one booking)
    # ════════════════════════════════════════════════════════════════

    def _conversion_override_applied(self):
        """Manager-authorized bypass of the internal-confirmation gate."""
        self.ensure_one()
        if not (self.conversion_override
                and (self.conversion_override_reason or "").strip()):
            return False
        if not self.env.user.has_group(
                "prema_logistics_booking.group_logistics_booking_manager"):
            raise AccessError(_(
                "Only booking managers may convert a Rate Confirmation "
                "before internal confirmation (the override requires the "
                "booking-manager group and a recorded reason)."))
        return True

    @api.returns("logistics.booking", lambda value: value.id)
    def action_convert_to_booking(self):
        """Convert accepted quote into a real booking using the canonical
        BookingOrchestrationService. Idempotent — returns existing booking
        if already converted; the orchestration key custom_quote:{id} makes
        the booking unique even across channels."""
        self.ensure_one()
        if not self.quoted_price:
            raise UserError(_(
                "Set the quoted price before converting this Rate "
                "Confirmation to a booking."))
        # Idempotency: return existing booking (records for in-process
        # callers; the @api.returns downgrade hands remote RPC its id).
        if self.booking_id:
            return self.booking_id
        # The manual-price policy gate: this is the moment the figure
        # becomes a real, bookable commitment. A draft was never blocked,
        # and a repeat call that creates nothing is not blocked either.
        self._require_manual_price_reason()
        if self.state != "accepted":
            raise UserError(_(
                "Record the customer's acceptance first — only an accepted "
                "Rate Confirmation may be converted to a booking."))
        if not (self.internal_confirmed or self._conversion_override_applied()):
            raise UserError(_(
                "Confirm this Rate Confirmation internally first (the "
                "staff-side GO decision), or a booking manager must set "
                "the override flag with a reason."))
        self._require_current_revision()

        if not self.departure_id:
            raise UserError(_(
                "Assign an exact departure before converting this custom "
                "quote to a booking — a custom quote has no automated route "
                "to resolve one from, and a booking may never be confirmed "
                "without a real, capacity-validated departure."
            ))
        if self._conversion_override_applied():
            self.with_context(_CQ_INTERNAL_WRITE_CTX=True).write({
                "conversion_override_by": self.env.user.id,
                "conversion_override_at": fields.Datetime.now(),
            })

        # §6 (D-B2): the Internal Load Reference is generated exactly ONCE
        # here (default = this Rate Confirmation's number) when the staff
        # did not already assign one. From this point on the SAME string
        # propagates unchanged booking → dispatch job → invoice; it is
        # never re-derived from the PO or the BOL.
        if not self.reference:
            self.with_context(_CQ_INTERNAL_WRITE_CTX=True).write(
                {"reference": self.name or self.id})

        from ..services.booking_orchestration_service import BookingOrchestrationService
        svc = BookingOrchestrationService(self.env)

        norm = svc.normalize_request({
            "partner_id": self.partner_id.id,
            "pickup_stops": [{
                "postal_code": self.resolved_fsa_pickup or "",
                "formatted_address": self.pickup_address or "",
            }],
            "delivery_stops": [{
                "postal_code": self.resolved_fsa_delivery or "",
                "formatted_address": self.delivery_address or "",
            }],
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "commodity": self.commodity or "",
            "load_type": self.load_type,
            "equipment_type": self.temperature_mode,
            "required_temperature_c": self.required_temperature_c if self.temperature_mode == "reefer" else None,
            "pricing_method": "manual",
            "agreed_rate": self.quoted_price,
            "departure_id": self.departure_id.id,
            "custom_quote_id": self.id,
            "idempotency_key": f"custom_quote:{self.id}",
            # §6 identifiers — carried into the booking verbatim. Note:
            # customer_reference is deliberately NOT fed from the PO — the
            # legacy "ref" fallback must never become a PO alias.
            "po_number": self.customer_po or "",
            "reference": self.reference or "",
            "bol_number": self.bol_number or "",
            # §7 payment/QuickPay snapshot + pricing basis.
            "price_tax_mode": self.price_tax_mode or "exclusive",
            "payment_method_id": self.payment_method_id.id or False,
            "payment_term_id": self.payment_term_id.id or False,
            "quickpay_apply": self.quickpay_apply,
            "quickpay_discount_pct": self.quickpay_discount_pct,
            "quickpay_deadline_days": self.quickpay_deadline_days,
            "quickpay_stack_allowed": self.quickpay_stack_allowed,
            "quickpay_override_reason": self.quickpay_override_reason or "",
        }, source_channel="custom_quote")

        booking = svc.confirm_from_internal(
            norm,
            skip_invoice=False,
            # The FINAL quoted price becomes the booking's customer sell
            # price (revenue authority); the system price and the reason
            # travel along for the permanent audit trail.
            sell_price_override=self.quoted_price,
            sell_price_override_reason=self.manual_price_reason or "",
            sell_price_override_by=self.manual_price_changed_by.id or False,
            system_calculated_price=self.system_calculated_price,
        )
        self.booking_id = booking.id
        self.state = "converted"
        self.message_post(
            body=_("Converted to booking %s.") % booking.booking_number,
            subtype_xmlid="mail.mt_note")
        return booking

    # ════════════════════════════════════════════════════════════════
    # R5/R8 (§4.8/§4.9) — REVISE & RESEND (authorized revision path)
    # ════════════════════════════════════════════════════════════════

    def _prepare_revision_copy(self):
        """Snapshot of the sent document for the revision-N+1 branch.

        The old row is never touched again: it keeps its immutable send
        attempts, markers and field values (that IS the snapshot). The
        branch reopens editing with the same document number and payload.
        """
        self.ensure_one()
        return {
            "name": self.name,
            "partner_id": self.partner_id.id,
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "company_name": self.company_name,
            "source": self.source,
            "pickup_postal_code": self.pickup_postal_code,
            "pickup_address": self.pickup_address,
            "delivery_postal_code": self.delivery_postal_code,
            "delivery_address": self.delivery_address,
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "temperature_mode": self.temperature_mode,
            "required_temperature_c": self.required_temperature_c,
            "load_type": self.load_type,
            "departure_id": self.departure_id.id,
            "commodity": self.commodity,
            "requested_pickup_date": self.requested_pickup_date,
            "notes": self.notes,
            "quoted_price": self.quoted_price,
            "system_calculated_price": self.system_calculated_price,
            "manual_price_override": self.manual_price_override,
            "manual_price_reason": self.manual_price_reason,
            "manual_price_changed_by": self.manual_price_changed_by.id,
            "manual_price_changed_at": self.manual_price_changed_at,
            "internal_notes": self.internal_notes,
            "reason_code": self.reason_code,
            "resolved_fsa_pickup": self.resolved_fsa_pickup,
            "resolved_fsa_delivery": self.resolved_fsa_delivery,
            "resolved_region_pickup": self.resolved_region_pickup.id,
            "resolved_region_delivery": self.resolved_region_delivery.id,
            "routing_strategy": self.routing_strategy,
            "estimator_id": self.estimator_id.id,
            "crm_lead_id": self.crm_lead_id.id,
            "company_id": self.company_id.id,
            # §6/§7 (D-B2): the revision branch reopens the SAME
            # identifiers and payment agreement — editing them on the
            # branch is a deliberate, tracked change.
            "customer_po": self.customer_po,
            "reference": self.reference,
            "bol_number": self.bol_number,
            "price_tax_mode": self.price_tax_mode,
            "payment_method_id": self.payment_method_id.id,
            "payment_term_id": self.payment_term_id.id,
            "quickpay_apply": self.quickpay_apply,
            "quickpay_discount_pct": self.quickpay_discount_pct,
            "quickpay_deadline_days": self.quickpay_deadline_days,
            "quickpay_stack_allowed": self.quickpay_stack_allowed,
            "quickpay_override_reason": self.quickpay_override_reason,
        }

    def action_revise(self, reason=""):
        """Authorized revision path (booking managers only, R8).

        A sent Rate Confirmation can never be re-emailed or rewritten.
        This branches revision N+1 as a NEW draft row with the same
        document number, reopens it for editing, and records the reason,
        the user and the timestamp. Only that new revision may be sent
        again (its own single send attempt).
        """
        self.ensure_one()
        if not self.env.user.has_group(
                "prema_logistics_booking.group_logistics_booking_manager"):
            raise AccessError(_(
                "Only booking managers may revise a sent Rate Confirmation."))
        if self.booking_id:
            raise UserError(_(
                "This Rate Confirmation is already converted to a booking "
                "(%s) — revise by adjusting the booking instead.") %
                self.booking_id.booking_number)
        if not self.is_locked:
            raise UserError(_(
                "This Rate Confirmation has not been sent yet — edit the "
                "draft directly; a revision is only needed once the "
                "customer has received the document."))
        if self.is_superseded:
            raise UserError(_(
                "This Rate Confirmation was already revised — work on the "
                "newest revision."))
        if not (reason or "").strip():
            raise UserError(_(
                "A reason for the revision is required (it is recorded on "
                "the new revision with your name and timestamp)."))
        if self.state not in ("quoted", "accepted", "declined"):
            raise UserError(_(
                "This Rate Confirmation cannot be revised from state "
                "\"%s\".") % dict(QUOTE_STATE).get(self.state, self.state))

        now = fields.Datetime.now()
        vals = self._prepare_revision_copy()
        vals.update({
            "revision_no": (self.revision_no or 1) + 1,
            "revised_from_id": self.id,
            "revision_reason": (reason or "").strip(),
            "revised_at": now,
            "revised_by_id": self.env.user.id,
            # A new revision is a fresh offer: acceptance, internal
            # confirmation and override do NOT carry over (the new terms
            # need their own acceptance), and the row starts unlocked with
            # no send attempts and no idempotency key.
            "state": "quoted",
            "acceptance_channel": False,
            "acceptance_recorded_at": False,
            "acceptance_recorded_by": False,
            "internal_confirmed": False,
            "internal_confirmed_at": False,
            "internal_confirmed_by": False,
            "conversion_override": False,
            "conversion_override_reason": False,
            "conversion_override_by": False,
            "conversion_override_at": False,
            "idempotency_key": False,
        })
        new_revision = self.create(vals)
        self.message_post(
            body=_("Revision %s of this Rate Confirmation was created by "
                   "%s — %s") % (
                new_revision.revision_no, self.env.user.name,
                (reason or "").strip()),
            subtype_xmlid="mail.mt_note")
        return new_revision

    # ── Revision diff helpers ────────────────────────────────────────

    def _field_readable_value(self, field_name):
        """Human-readable (JSON-safe) value of one diff field."""
        self.ensure_one()
        field = self._fields[field_name]
        raw = self[field_name]
        if raw in (None, False):
            return None
        if field.type == "many2one":
            rec = raw
            return rec.display_name if rec else None
        if field.type == "selection":
            return dict(field.selection).get(raw, raw)
        if isinstance(raw, (datetime.date, datetime.datetime)):
            return raw.isoformat()
        return raw

    def _revision_diff_payload(self, other):
        """JSON-safe {field: [old, new]} diff of the customer-facing
        document fields between self and the sent revision `other`."""
        self.ensure_one()
        diff = {}
        for field_name in _CQ_REVISION_DIFF_FIELDS:
            old_val = other._field_readable_value(field_name) \
                if other else None
            new_val = self._field_readable_value(field_name)
            if new_val != old_val:
                diff[field_name] = [old_val, new_val]
        return diff

    def _diff_to_html(self, diff):
        if not diff:
            return "<p><em>%s</em></p>" % html.escape(
                _("No commercial details changed on this revision."))
        rows = []
        for field_name, (old_val, new_val) in diff.items():
            label = self._fields[field_name].string or field_name
            old_s = html.escape(str(old_val)) if old_val not in (None, "") else "—"
            new_s = html.escape(str(new_val)) if new_val not in (None, "") else "—"
            rows.append(
                "<li><strong>%s</strong>: %s → %s</li>" % (label, old_s, new_s))
        return "<ul>%s</ul>" % "".join(rows)

    # ── Customer view tracking hook (R7 extension point) ─────────────

    def _mark_viewed_by_customer(self):
        """Called by a future customer RC portal view (pure read page):
        timestamps the view ONLY — never a commercial mutation, never an
        email. No portal page exists yet, so nothing calls this today."""
        self.ensure_one()
        self.with_context(_CQ_INTERNAL_WRITE_CTX=True).write({
            "viewed_by_customer_at": fields.Datetime.now(),
            "viewed_by_customer_count":
                (self.viewed_by_customer_count or 0) + 1,
        })

    # ── Legacy state helpers ─────────────────────────────────────────

    def action_reset_price(self):
        """Convenience reset — quoted price back to the system price; the
        reason is then no longer required."""
        self.ensure_one()
        if not self.system_calculated_price:
            raise UserError(_("No system calculated price is stored for this quote."))
        self.write({
            "quoted_price": self.system_calculated_price,
            "manual_price_reason": "",
        })
        return True

    def action_start_review(self):
        self.state = "reviewing"

    def action_quote(self):
        self.state = "quoted"

    def action_decline(self):
        if self.state not in ("quoted", "accepted"):
            raise UserError(_(
                "Only a quoted Rate Confirmation can be declined."))
        self.write({"state": "declined"})
        self.message_post(
            body=_("Rate Confirmation declined."), subtype_xmlid="mail.mt_note")
        return True

    def action_open_estimator(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Prema AI Estimator",
            "res_model": "premafirm.rate.estimator",
            "view_mode": "form",
            "target": "current",
            "context": {
                "default_origin_address": self.pickup_address,
                "default_destination_address": self.delivery_address,
                "default_load_pallets": self.pallets,
                "default_load_weight_lbs": self.weight_lbs,
                "default_partner_id": self.partner_id.id,
            },
        }

"""D-A3B — CRM recurring-opportunity bridge (dispatch side of the E-A3
contract, ``docs/A3_RECURRING_BRIDGE_CONTRACT.md`` in the engine repo).

The engine module (premafirm_ai_engine, ``crm.recurring.opportunity``)
holds the SALES record and never imports logistics.  Everything that
touches ``logistics.*`` lives here (same precedent as
``crm_lead_rate_confirmation.py`` extending ``crm.lead``).

Implements, against the EXISTING recurring machinery
(``logistics.recurring.agreement`` / ``.job``: activation-gated, idempotent
cron generation, max 10 jobs, Google-verified locations):

* ``crm_opportunity_id`` reverse link on the agreement + the
  ``agreement_reference`` anchor ``"CRM-REC-<opportunity id>[ — PO]"``
  (customer PO text forwarded verbatim on the same field).
* ONE activation entry point from the CRM side,
  ``action_activate_for_crm_opportunity`` (on the agreement model): guards
  (contracted + customer-confirmed, no Sunday weekday — the dispatch
  network has no Sunday service — no irregular cadence, not Ended, no
  duplicate agreement, Google-verified pickup/delivery anchors set on the
  opportunity), creates the agreement with one job per selected weekday
  (Monday default when none) mirroring those anchors, activates it
  through the existing activation rules and only then syncs the
  opportunity to Active.  The cron generation semantics are untouched: no
  occurrence is generated during activation.
* Dispatch-side anchor fields on the opportunity itself
  (``pickup_dispatch_location_id`` / ``delivery_dispatch_location_id``):
  the engine record holds no logistics reference by design, and the base
  model refuses location-kind jobs without a Google-verified address — so
  the recurring route anchors are collected here (dispatch side), the
  same way the booking module already extends ``crm.lead``.
* Reverse sync on the agreement state actions (activate/pause/expire/
  cancel) → the engine event
  ``crm.recurring.opportunity.prema_sync_activation_from_dispatch``.
  Replay is idempotent (engine-side state stamps; replay adds no note)
  and the guard never overwrites a terminal Ended opportunity with a
  stale sync.
* Rate-confirmation intent consumption (§17.1-17.2): when a linked
  agreement whose opportunity has ``intent_rate_confirmation`` reaches
  Active, the canonical customer Rate Confirmation draft is produced
  (``logistics.custom.quote`` factory, the flow a confirmed one-off
  booking follows).  Failures log + warn-activity — activation is never
  blocked.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Opportunity weekday booleans → dispatch job preferred_weekday index.
# Sunday (6) is deliberately absent: the dispatch machinery (job
# ``preferred_weekday`` selection and the corridor/day operations) has no
# Sunday service.
_CRM_WEEKDAY_INDEX = {
    "preferred_monday": 0,
    "preferred_tuesday": 1,
    "preferred_wednesday": 2,
    "preferred_thursday": 3,
    "preferred_friday": 4,
    "preferred_saturday": 5,
}
_DAY_LABELS = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday",
}

_ANCHOR_PREFIX = "CRM-REC-"


def _agreement_anchor(opportunity):
    """Cross-module anchor stored in ``agreement_reference`` (contract §4):
    ``CRM-REC-<opportunity id>[ — customer PO]``.  The reverse lookup on the
    engine side is ``agreement_reference =like 'CRM-REC-<id>%'``."""
    anchor = "%s%d" % (_ANCHOR_PREFIX, opportunity.id)
    if opportunity.agreement_reference:
        anchor += " — " + opportunity.agreement_reference
    return anchor


class LogisticsRecurringAgreement(models.Model):
    """CRM recurring-opportunity bridge on logistics.recurring.agreement."""

    _inherit = "logistics.recurring.agreement"

    crm_opportunity_id = fields.Many2one(
        "crm.recurring.opportunity",
        string="CRM Recurring Opportunity",
        ondelete="set null", index=True, copy=False, tracking=True,
        help="The CRM recurring opportunity this agreement was created for "
             "(E-A3 bridge). The engine record mirrors the anchor in "
             "dispatch_agreement_reference; this field is the real link.",
    )

    # ── agreement state actions — §3 reverse sync overrides ───────────

    def action_activate(self):
        """Activate through the existing rules, then reverse-sync.

        §3 preconditions (contracted + customer confirmed, terminal Ended
        never resurrected) are checked BEFORE the write — the sync itself
        can only fail for environmental reasons, which are logged and
        turned into a warn-activity, never raised."""
        for agreement in self.filtered("crm_opportunity_id"):
            agreement._check_crm_opportunity_can_activate()
        result = super().action_activate()
        active = self.filtered(lambda a: a.state == "active")
        active._prema_sync_crm_opportunity()
        active._consume_crm_rate_confirmation_intent()
        return result

    def action_pause(self):
        result = super().action_pause()
        self._prema_sync_crm_opportunity()
        return result

    def action_expire(self):
        result = super().action_expire()
        self._prema_sync_crm_opportunity()
        return result

    def action_cancel(self):
        result = super().action_cancel()
        self._prema_sync_crm_opportunity()
        return result

    # ── §3 guards (raised before any agreement mutation) ──────────────

    def _check_crm_opportunity_can_activate(self):
        """Engine-side preconditions for syncing to Active, checked BEFORE
        the agreement write so a refused activation mutates nothing."""
        self.ensure_one()
        opportunity = self.crm_opportunity_id
        if not opportunity:
            return
        if opportunity.activation_state == "ended":
            raise UserError(_(
                "Cannot activate agreement %(agreement)s: its CRM recurring "
                "opportunity #%(id)s is Ended (terminal). Ended recurring "
                "opportunities are never re-activated — open a NEW "
                "recurring opportunity for a renewed arrangement.",
                agreement=self.name, id=opportunity.id))
        if opportunity.kind != "contracted" \
                or not opportunity.customer_confirmed:
            raise UserError(_(
                "Cannot activate agreement %(agreement)s: CRM recurring "
                "opportunity #%(id)s must be Contracted with customer "
                "confirmation recorded before its dispatch agreement can "
                "go Active.",
                agreement=self.name, id=opportunity.id))

    # ── §3 reverse sync event ─────────────────────────────────────────

    def _prema_sync_crm_opportunity(self, reason=""):
        """Mirror the agreement state onto the linked CRM opportunity
        through the engine event ``prema_sync_activation_from_dispatch``
        (idempotent replay — the engine's state stamps make a same-state
        replay a no-op that adds no note).

        Guard: an Ended opportunity is terminal on the sales side (the
        dedupe key was released); the sync never overwrites that newer
        state with a stale agreement event.  Sync failures are logged and
        surfaced as a warn-activity on the agreement — they never break
        the agreement write that just succeeded."""
        for agreement in self.filtered("crm_opportunity_id"):
            opportunity = agreement.crm_opportunity_id
            if agreement.state not in ("active", "paused", "expired",
                                       "cancelled"):
                continue
            if opportunity.activation_state == "ended":
                _logger.info(
                    "Agreement %s: linked CRM recurring opportunity %s is "
                    "Ended — state sync skipped (%s).",
                    agreement.id, opportunity.id, agreement.state)
                continue
            try:
                opportunity.prema_sync_activation_from_dispatch(
                    agreement.state,
                    agreement.agreement_reference or None,
                    reason=reason or _(
                        "linked dispatch agreement %(name)s",
                        name=agreement.name))
            except Exception as exc:  # noqa: BLE001 — contract: logging only
                _logger.exception(
                    "CRM recurring-opportunity sync failed for agreement %s "
                    "(state %s, opportunity %s)",
                    agreement.id, agreement.state, opportunity.id)
                agreement.activity_schedule(
                    "mail.mail_activity_data_warning",
                    summary=_("CRM opportunity sync failed"),
                    note=_(
                        "Agreement state %(state)s could not be mirrored on "
                        "CRM recurring opportunity #%(id)s: %(error)s",
                        state=agreement.state, id=opportunity.id, error=exc),
                    user_id=agreement.account_manager_id.id
                    or self.env.user.id,
                )

    # ── §17.1-17.2 rate-confirmation intent consumption ──────────────

    def _consume_crm_rate_confirmation_intent(self):
        """When a CRM-linked agreement reaches Active and its opportunity
        carries ``intent_rate_confirmation``, produce the customer Rate
        Confirmation draft through the canonical factory
        (``logistics.custom.quote.find_or_create_draft_for_lead`` — the
        flow a confirmed one-off booking follows).  The draft is filled
        only when it is OURS (idempotency key) and still untouched; an
        existing RC row for the lead (e.g. a one-off quote in flight) is
        never touched.  Failure logs + warn-activity — activation is never
        blocked."""
        for agreement in self.filtered(
                lambda a: a.crm_opportunity_id and a.state == "active"):
            opportunity = agreement.crm_opportunity_id
            if not opportunity.intent_rate_confirmation \
                    or not opportunity.lead_id:
                continue
            key = "crm-recurring-agreement:%d" % agreement.id
            Quote = self.env["logistics.custom.quote"]
            try:
                quote = Quote.sudo().find_or_create_draft_for_lead(
                    opportunity.lead_id.id, idempotency_key=key)
            except Exception as exc:  # noqa: BLE001 — never block activation
                _logger.exception(
                    "Rate Confirmation draft creation failed for agreement "
                    "%s (opportunity %s)", agreement.id, opportunity.id)
                agreement.activity_schedule(
                    "mail.mail_activity_data_warning",
                    summary=_("Customer Rate Confirmation not created"),
                    note=_(
                        "The recurring-agreement activation asked for a "
                        "customer Rate Confirmation, but the draft could "
                        "not be created: %(error)s", error=exc),
                    user_id=agreement.account_manager_id.id
                    or self.env.user.id)
                continue
            if quote.idempotency_key != key:
                # The lead already carries an open RC row (one-off quote or
                # prior draft): the dispatcher decides; never clobber it.
                _logger.info(
                    "Agreement %s: lead %s already has an open Rate "
                    "Confirmation (%s) — intent left to the dispatcher.",
                    agreement.id, opportunity.lead_id.id, quote.name)
                continue
            if quote.state == "new" and not quote.booking_id \
                    and not quote.is_locked:
                try:
                    quote.write(self._rate_confirmation_draft_values(
                        agreement, opportunity))
                    agreement.message_post(
                        body=_(
                            "Customer Rate Confirmation draft %(quote)s "
                            "created (intent flag on CRM recurring "
                            "opportunity #%(id)s) — set the quoted price "
                            "and send it to the customer.",
                            quote=quote.name, id=opportunity.id),
                        subtype_xmlid="mail.mt_note")
                except Exception as exc:  # noqa: BLE001 — logging only
                    _logger.exception(
                        "Rate Confirmation draft %s could not be populated "
                        "for agreement %s", quote.id, agreement.id)
                    agreement.activity_schedule(
                        "mail.mail_activity_data_warning",
                        summary=_("Rate Confirmation draft incomplete"),
                        note=_(
                            "Rate Confirmation %(quote)s was created but "
                            "could not be pre-filled: %(error)s",
                            quote=quote.name, error=exc),
                        user_id=agreement.account_manager_id.id
                        or self.env.user.id)

    def _rate_confirmation_draft_values(self, agreement, opportunity):
        """Shipment-profile mirror for the RC draft (only fields set on the
        CRM record are forwarded)."""
        vals = {}
        if opportunity.expected_pallets:
            vals["pallets"] = opportunity.expected_pallets
        if opportunity.expected_weight_lbs:
            vals["weight_lbs"] = opportunity.expected_weight_lbs
        if opportunity.expected_temperature_mode:
            vals["temperature_mode"] = opportunity.expected_temperature_mode
        if opportunity.expected_temperature_mode == "reefer":
            vals["required_temperature_c"] = \
                opportunity.required_temperature_c
        if opportunity.expected_load_type:
            vals["load_type"] = opportunity.expected_load_type
        if opportunity.commodity:
            vals["commodity"] = opportunity.commodity
        days = ", ".join(
            _DAY_LABELS[int(idx)] for idx in sorted(
                {int(job.preferred_weekday or "0")
                 for job in agreement.job_ids}))
        if not days:
            days = "Monday"
        job = agreement.job_ids.filtered("active")[:1]
        pallets = job.pallets if job else agreement.pallets or 1
        weight = job.weight_lbs if job else agreement.weight_lbs or 0.0
        vals["notes"] = "\n".join(filter(None, [
            _("Recurring dispatch agreement %(name)s (%(ref)s)",
              name=agreement.name, ref=agreement.agreement_reference or ""),
            _("Cadence: %(frequency)s — %(days)s",
              frequency=dict(agreement._fields["frequency"].selection)
              .get(agreement.frequency, agreement.frequency), days=days),
            _("Expected per shipment: %(pallets)s pallets, "
              "%(weight)s lb", pallets=pallets, weight=weight),
            _("Created from CRM recurring opportunity #%(id)s — set the "
              "quoted price and send to the customer through the Rate "
              "Confirmation flow.", id=opportunity.id),
        ]))
        return vals

    # ── agreement creation entry point (ONE explicit activation path) ─

    @api.model
    def action_activate_for_crm_opportunity(self, opportunity):
        """Create + activate the dispatch recurring agreement for a CRM
        recurring opportunity — the bridge's ONE activation entry point
        (contract §4).

        All guard rails raise BEFORE anything is created, so a refused
        activation leaves the opportunity untouched (e.g. in
        ``awaiting_activation``) with the reason in the error message:
          * contracted + customer-confirmed cadence only;
          * no duplicate agreement for the same opportunity;
          * never for an Ended opportunity;
          * irregular cadences stay engine-managed — no dispatch agreement;
          * no Sunday weekday — the dispatch network has no Sunday service;
          * effective dates must not be over;
          * Google-verified pickup + delivery anchors must be set on the
            opportunity (mirrors the base per-job activation rule).

        :param opportunity: ``crm.recurring.opportunity`` record (the
            engine-side sales record; every gate is checked on it).
        :return: the created (and activated) ``logistics.recurring.agreement``
        """
        opportunity.ensure_one()
        # ── guard rails (raise before creating anything) ──────────────
        if opportunity.activation_state == "ended":
            raise UserError(_(
                "Recurring opportunity #%(id)s is Ended (terminal): it "
                "cannot create a dispatch agreement. Open a NEW recurring "
                "opportunity for a renewed arrangement.",
                id=opportunity.id))
        existing = self.search(
            [("crm_opportunity_id", "=", opportunity.id)], limit=1)
        if existing:
            raise UserError(_(
                "A dispatch recurring agreement already exists for CRM "
                "recurring opportunity #%(id)s — agreement %(agreement)s. "
                "Duplicate agreements for one opportunity are not allowed; "
                "work on the existing one.",
                id=opportunity.id, agreement=existing.name))
        if opportunity.kind != "contracted" \
                or not opportunity.customer_confirmed:
            raise UserError(_(
                "Recurring opportunity #%(id)s must be Contracted with "
                "customer confirmation recorded before a dispatch "
                "recurring agreement may be created (activation is only "
                "possible for confirmed contracted cadences).",
                id=opportunity.id))
        if opportunity.frequency == "irregular":
            raise UserError(_(
                "Recurring opportunity #%(id)s is Irregular. Irregular "
                "cadences stay managed on the CRM recurring opportunity — "
                "the dispatch recurring engine only generates weekly / "
                "biweekly / monthly agreements, so no dispatch agreement "
                "was created. Track and trigger irregular work manually.",
                id=opportunity.id))
        if opportunity.preferred_sunday:
            raise UserError(_(
                "The dispatch network has no Sunday service — recurring "
                "opportunity #%(id)s lists Sunday among its preferred "
                "weekdays. Remove Sunday from the preferred weekdays on "
                "the CRM record (or contact dispatch for a manual "
                "arrangement); no agreement was created.",
                id=opportunity.id))

        # ── route anchors (raise before creating anything) ─────────────
        # Recurring jobs are created from the opportunity's Google-verified
        # pickup/delivery addresses — the same verified-location rule the
        # base activation enforces per job (a location-kind job without a
        # Google-verified location cannot exist in this model).
        pickup_location = opportunity.pickup_dispatch_location_id
        delivery_location = opportunity.delivery_dispatch_location_id
        if not pickup_location or not delivery_location:
            raise UserError(_(
                "Set the recurring Pickup and Delivery addresses on CRM "
                "recurring opportunity #%(id)s first (both must be "
                "Google-verified saved locations); no agreement was "
                "created.",
                id=opportunity.id))
        for label, location in (
                (_("Pickup"), pickup_location),
                (_("Delivery"), delivery_location),
        ):
            if not location.google_verified or not location.google_place_id:
                raise UserError(_(
                    "%(label)s address %(location)s must be verified "
                    "through Google Places before it may anchor a "
                    "recurring dispatch agreement (recurring opportunity "
                    "#%(id)s).",
                    label=label, location=location.display_name,
                    id=opportunity.id))

        today = fields.Date.context_today(self)
        start_date = opportunity.start_date or today
        # Blank end date stays blank: the agreement is open-ended (active
        # until paused, cancelled, or expired manually) — never invent one.
        end_date = opportunity.end_date
        if end_date and end_date < today:
            raise UserError(_(
                "Recurring opportunity #%(id)s ended on %(end)s — its "
                "effective period is over. Extend the dates on the CRM "
                "record before creating a dispatch agreement.",
                id=opportunity.id, end=end_date))

        # ── weekday → job mapping (Sunday already refused above) ──────
        weekday_indexes = sorted(
            idx for field_name, idx in _CRM_WEEKDAY_INDEX.items()
            if opportunity[field_name])
        if not weekday_indexes:
            weekday_indexes = [0]          # no day selected → Monday

        frequency_label = dict(self.env[
            "logistics.recurring.job"]._fields["frequency"].selection
        ).get(opportunity.frequency, opportunity.frequency)

        def _job_vals(weekday_index):
            job_vals = {
                "name": _("%(partner)s — %(day)s",
                          partner=opportunity.partner_id.name
                          or _("Customer"), day=_DAY_LABELS[weekday_index]),
                "frequency": opportunity.frequency,
                "preferred_weekday": str(weekday_index),
                # Jobs mirror the opportunity's Google-verified anchors but
                # keep auto-generate OFF: activation must not start the
                # booking generator — the dispatcher reviews each job and
                # flips Auto-generate only when the route is ready (see
                # service_notes).
                "pickup_kind": "location",
                "pickup_location_id": pickup_location.id,
                "delivery_kind": "location",
                "delivery_location_id": delivery_location.id,
                "auto_generate": False,
            }
            if opportunity.expected_pallets:
                job_vals["pallets"] = opportunity.expected_pallets
            if opportunity.expected_weight_lbs:
                job_vals["weight_lbs"] = opportunity.expected_weight_lbs
            if opportunity.expected_load_type:
                job_vals["load_type"] = opportunity.expected_load_type
            if opportunity.expected_temperature_mode:
                job_vals["temperature_mode"] = \
                    opportunity.expected_temperature_mode
                if opportunity.expected_temperature_mode == "reefer":
                    job_vals["required_temperature_c"] = \
                        opportunity.required_temperature_c
                    job_vals["temperature_confirmed"] = True
            if opportunity.commodity:
                job_vals["commodity"] = opportunity.commodity
            return job_vals

        note_lines = []
        if not opportunity.start_date:
            note_lines.append(_(
                "No start date on the CRM recurring opportunity — "
                "agreement starts %s.", start_date))
        if not opportunity.end_date:
            note_lines.append(_(
                "No end date on the CRM recurring opportunity — the "
                "agreement is open-ended: ACTIVE until paused, cancelled, "
                "or expired manually."))
        if opportunity.frequency_detail:
            note_lines.append(_(
                "Cadence detail from CRM: %s",
                opportunity.frequency_detail))
        note_lines.append(_(
            "Created from CRM recurring opportunity #%(id)s — jobs use "
            "the opportunity's Google-verified pickup/delivery addresses "
            "and auto-generate is OFF: review each job's route and enable "
            "Auto-generate before the network may create bookings.",
            id=opportunity.id))

        agreement_vals = {
            "partner_id": opportunity.partner_id.id,
            "crm_opportunity_id": opportunity.id,
            "agreement_reference": _agreement_anchor(opportunity),
            "frequency": opportunity.frequency,
            "start_date": start_date,
            "service_notes": "\n".join(note_lines),
            "job_ids": [(0, 0, _job_vals(idx)) for idx in weekday_indexes],
        }
        if end_date:
            agreement_vals["end_date"] = end_date
        # Assignment is read from the lead only (never written on the CRM
        # record); when the lead has no salesperson the agreement keeps its
        # own default (the acting user).
        if opportunity.lead_id.user_id:
            agreement_vals["account_manager_id"] = \
                opportunity.lead_id.user_id.id

        agreement = self.create(agreement_vals)
        try:
            # Existing activation rules: end-date check, ≥1 active job,
            # per-job validation, next_shipment_date stamping.  This
            # triggers the §3 reverse sync (opportunity → Active) and the
            # §17.1-17.2 rate-confirmation intent hook.  No occurrence is
            # generated — the cron keeps its own semantics untouched.
            agreement.action_activate()
        except Exception:
            _logger.exception(
                "Activation of the CRM-created agreement %s failed",
                agreement.id)
            raise
        agreement.message_post(
            body=_(
                "Created and activated from CRM recurring opportunity "
                "#%(id)s — %(cadence)s, %(jobs)s job(s), reference "
                "%(ref)s.",
                id=opportunity.id, cadence=frequency_label,
                jobs=len(agreement.job_ids),
                ref=agreement.agreement_reference or ""),
            subtype_xmlid="mail.mt_note")
        return agreement

    # ── agreement form: open the linked opportunity ──────────────────

    def action_open_crm_opportunity(self):
        """Stat-button: open the linked CRM recurring opportunity form."""
        self.ensure_one()
        if not self.crm_opportunity_id:
            raise UserError(_(
                "This agreement is not linked to a CRM recurring "
                "opportunity."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Recurring Opportunity"),
            "res_model": "crm.recurring.opportunity",
            "view_mode": "form",
            "res_id": self.crm_opportunity_id.id,
            "target": "current",
        }


class CrmRecurringOpportunity(models.Model):
    """Dispatch-side extension of the engine record — same precedent as
    ``crm_lead_rate_confirmation.py`` extending ``crm.lead``: the engine
    never imports logistics, so the recurring pickup/delivery anchors for
    the dispatch agreement live HERE (dispatch side), mirroring the
    Google-verified saved-location model the recurring jobs validate
    against.  The engine is untouched; the fields are plain M2o columns on
    its table."""

    _inherit = "crm.recurring.opportunity"

    pickup_dispatch_location_id = fields.Many2one(
        "prema.dispatch.location", string="Recurring Pickup Address",
        domain="[('active', '=', True)]",
        help="Google-verified pickup facility used by every recurring job "
             "of the dispatch agreement created from this opportunity.")
    delivery_dispatch_location_id = fields.Many2one(
        "prema.dispatch.location", string="Recurring Delivery Address",
        domain="[('active', '=', True)]",
        help="Google-verified delivery facility used by every recurring job "
             "of the dispatch agreement created from this opportunity.")

    def action_create_recurring_agreement(self):
        """'Create Recurring Agreement' header button: create + activate
        the linked dispatch agreement through the single entry point on
        ``logistics.recurring.agreement``.  All guard failures raise their
        clear UserError before anything is mutated; on success the
        opportunity is synced to Active by the agreement's activation."""
        self.ensure_one()
        agreement = self.env[
            "logistics.recurring.agreement"
        ].action_activate_for_crm_opportunity(self)
        return {
            "type": "ir.actions.act_window",
            "name": _("Recurring Agreement"),
            "res_model": "logistics.recurring.agreement",
            "view_mode": "form",
            "res_id": agreement.id,
            "target": "current",
        }

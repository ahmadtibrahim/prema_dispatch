# -*- coding: utf-8 -*-
"""E-A2 cross-module contract §5 — dispatch-side staff actions (MP1).

Two DELIBERATE staff actions on crm.lead (buttons on the opportunity form,
gated to booking/pricing managers):

(a) ``action_prepare_preliminary_estimate`` — supersedes the customer's
    shipment facts (engine LeadFactService over description + inbound
    customer emails), resolves the stops against the saved-location rules,
    prices EXCLUSIVELY through the canonical dispatch path
    (BookingOrchestrationService.normalize_request + prepare_quote — never
    an invented amount), then drafts ONE premafirm.lead.estimate.reply via
    its prepare_from_lead (price + provenance + non-binding disclaimer,
    nothing sent).  The extractor is REPLAYED into the draft so its stored
    fact snapshot is exactly what was priced — no second AI call.

(b) ``action_create_draft_rate_confirmation`` — B1 lifecycle semantics:
    at most one discoverable open draft per lead (find_or_create_draft_for_
    lead, idempotency fallback after terminalization), populated with the
    resolved shipment facts for human completion.  Never prices, sends,
    confirms or books; a sent row is never silently rewritten (Revise &
    Resend is the sanctioned path).

Safety: no mail.mail, no pipeline stage change, no logistics.booking, no
sale.order, no account.move.  Every engine import is inside
LeadQuoteDraftService (module cycle).  Sent / superseded / converted rows
are left strictly alone.
"""

from odoo import _, models

from odoo.exceptions import UserError

from ..services.lead_quote_draft_service import LeadQuoteDraftService


class CrmLeadEstimateBridge(models.Model):
    _inherit = "crm.lead"

    # ── (a) Prepare Preliminary Estimate Reply ─────────────────────────

    def action_prepare_preliminary_estimate(self):
        """Draft the staff preliminary-estimate reply (contract §5a).

        Runs the full chain — supersession → stop resolution → canonical
        dispatch pricing → engine draft — and opens the draft for human
        review.  Nothing is sent, staged or booked.

        Repeat-click guard: while the newest draft already reflects the
        latest customer statements, a repeat click reopens that draft
        untouched (no second AI call, no second pricing session, no
        duplicate).  Only a NEWER customer document than the draft re-runs
        the chain — the controlled re-estimate path after a correction.
        """
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Select or create the Customer on this opportunity before "
                "preparing a preliminary estimate."))
        Estimate = self.env["premafirm.lead.estimate.reply"]
        existing = Estimate.search(
            [("crm_lead_id", "=", self.id)],
            order="create_date desc, id desc", limit=1)
        companion = LeadQuoteDraftService(self.env)
        if existing and companion.customer_documents_covered_by_draft(
                self, existing):
            # The newest draft was already built from every current customer
            # document: a repeat click reopens the applicable draft. Never
            # reprice/reprose — the draft may already carry reviewer edits.
            self.message_post(
                body=_(
                    "Preliminary estimate draft %s is still current for the "
                    "latest customer statements — reopened; nothing was "
                    "re-priced.") % (existing.subject or existing.id),
                subtype_xmlid="mail.mt_note",
            )
            return self._estimate_open_action(existing)

        facts = companion.extract_effective_facts(self)
        # Validates both sides and raises BEFORE anything is written when a
        # side is city-only or not priceable (contract §6 rules).
        stops = companion.resolve_stops(self, facts, require_priceable=True)
        shipment = companion.shipment_values(facts)
        if shipment["temperature_mode"] == "reefer" \
                and not shipment["temperature_setpoint_stated"]:
            raise UserError(_(
                "Reefer shipment, but the customer never stated a numerical "
                "setpoint ('frozen' or 'chilled' alone is not a setpoint). "
                "Confirm the setpoint with the customer, then retry — "
                "nothing was priced or created."))
        if shipment["load_type"] != "ftl" and not shipment["pallets_stated"]:
            raise UserError(_(
                "The customer never stated how many pallets this LTL "
                "shipment is, and the LTL price is quoted per pallet. "
                "Confirm the pallet count with the customer (and state it "
                "on the opportunity), then retry — nothing was priced or "
                "created."))

        quote = companion.canonical_quote(
            self, companion.estimate_request_values(self, stops, shipment))
        amount = quote["calculated_price"]
        price_reference = companion.price_reference_from_quote(quote)
        draft = Estimate.prepare_from_lead(
            self,
            price_amount=amount,
            price_reference=price_reference,
            extractor=companion.replay_extractor(facts),
        )
        if existing:
            self.message_post(
                body=_(
                    "New customer statement(s) arrived after the previous "
                    "draft, so a fresh preliminary estimate draft %s was "
                    "prepared from the latest facts (amount %s — %s). The "
                    "older draft %s is kept for reference.") % (
                    draft.subject or draft.id,
                    format(amount, ",.2f"),
                    price_reference,
                    existing.subject or existing.id),
                subtype_xmlid="mail.mt_note",
            )
        else:
            self.message_post(
                body=_(
                    "Preliminary estimate draft %s created (amount %s came "
                    "from the dispatch pricing engine — %s). Review it in "
                    "the draft before anything is sent.") % (
                    draft.subject or draft.id,
                    format(amount, ",.2f"),
                    price_reference),
                subtype_xmlid="mail.mt_note",
            )
        return self._estimate_open_action(draft)

    def _estimate_open_action(self, draft):
        """One proper window action opening the estimate draft's form."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Preliminary Estimate Reply"),
            "res_model": "premafirm.lead.estimate.reply",
            "view_mode": "form",
            "res_id": draft.id,
            "context": {"default_crm_lead_id": self.id},
        }

    # ── (b) Create Draft Rate Confirmation ─────────────────────────────

    def action_create_draft_rate_confirmation(self):
        """Create (or surface) the lead's draft Rate Confirmation (§5b).

        Exactly-one open-draft semantics come from the B1 lifecycle
        (find_or_create_draft_for_lead): repeated clicks return the same
        row; a new cycle starts only after the previous row terminalized.
        The draft is populated from the LATEST effective customer facts for
        human completion — it is never priced, sent, confirmed or booked.
        """
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Select or create the Customer on this opportunity before "
                "creating a draft Rate Confirmation."))
        companion = LeadQuoteDraftService(self.env)
        CQ = self.env["logistics.custom.quote"]

        facts = companion.extract_effective_facts(self)
        # (b) tolerates civic-without-postal text stops (draft is completed
        # by a human), but city-only / unstated stops still raise.
        stops = companion.resolve_stops(self, facts, require_priceable=False)
        shipment = companion.shipment_values(facts)
        populate = companion.rc_populate_vals(stops, shipment)

        rows_before = CQ.search_count([("crm_lead_id", "=", self.id)])
        draft = CQ.find_or_create_draft_for_lead(
            self.id,
            idempotency_key="lead-rc-draft:%s:%s" % (
                self.id, rows_before + 1))
        if draft.is_locked:
            raise UserError(_(
                "This opportunity's Rate Confirmation (%s) was already "
                "sent — customer-facing fields on it are locked. Use "
                "'Revise & Resend' on that document to branch an updated "
                "draft instead.") % draft.name)

        if CQ.search_count([("crm_lead_id", "=", self.id)]) > rows_before:
            # This click created the draft: populate it fully.
            draft.write(populate)
            draft.message_post(
                body=_(
                    "Draft Rate Confirmation created from the CRM "
                    "opportunity ('Create Draft Rate Confirmation'). "
                    "Shipment facts above come from the latest customer "
                    "statements; nothing was priced, sent or booked."),
                subtype_xmlid="mail.mt_note")
        else:
            # Reuse: never clobber human work — fill only empty slots of
            # still-editable drafts (new/reviewing) and surface the latest
            # facts on the chatter for the reviewer.  A row that already
            # carries a price or an acceptance is the reviewer's document:
            # facts are only reported on the chatter there.
            gaps = {}
            if draft.state in ("new", "reviewing"):
                for key, value in populate.items():
                    if value in ("", False, 0.0, None):
                        continue
                    if key == "notes":
                        continue  # the reviewer's document
                    if draft[key] in ("", False, 0.0, None):
                        gaps[key] = value
            if gaps:
                draft.write(gaps)
            draft.message_post(
                body=_(
                    "Checked against the latest customer facts "
                    "(no send). Current facts: pickup %s (%s); delivery %s "
                    "(%s); %s pallets%s; pickup date %s%s.%s") % (
                    draft.pickup_address or "—",
                    draft.pickup_postal_code or "no postal stated",
                    draft.delivery_address or "—",
                    draft.delivery_postal_code or "no postal stated",
                    draft.pallets,
                    " at %s°C reefer" % draft.required_temperature_c
                    if draft.temperature_mode == "reefer" else "",
                    draft.requested_pickup_date or "not stated",
                    "; pickup window %s – %s" % (
                        shipment["pickup_earliest"] or "?",
                        shipment["pickup_latest"] or "?")
                    if shipment["pickup_earliest"]
                    or shipment["pickup_latest"] else "",
                    " Update the draft where the customer corrected an "
                    "earlier statement."),
                subtype_xmlid="mail.mt_note")
        return {
            "type": "ir.actions.act_window",
            "name": _("Draft Rate Confirmation"),
            "res_model": "logistics.custom.quote",
            "view_mode": "list,form",
            "res_id": draft.id,
            "context": {"default_crm_lead_id": self.id},
        }

# -*- coding: utf-8 -*-
"""E-A2 cross-module contract §5 — the preliminary estimate reply (MP1).

``action_prepare_preliminary_estimate`` — supersedes the customer's shipment
facts (engine LeadFactService over description + inbound customer emails),
resolves the stops against the saved-location rules, prices EXCLUSIVELY
through the canonical dispatch path (BookingOrchestrationService.normalize_
request + prepare_quote — never an invented amount), then drafts ONE
premafirm.lead.estimate.reply via its prepare_from_lead (price + provenance
+ non-binding disclaimer, nothing sent).  The extractor is REPLAYED into the
draft so its stored fact snapshot is exactly what was priced — no second AI
call.

The companion action ``action_create_draft_rate_confirmation`` (and with it
the whole ``logistics.custom.quote`` staff workflow) was removed: the Sales
quotation is the only commercial quotation, and a second draft object for
the same shipment was two quote authorities for one deal.  See
``crm_lead_quotation_bridge`` for the entry points that replaced it.

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

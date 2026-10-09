# -*- coding: utf-8 -*-
"""What the job actually earned, and the draft invoice that states it.

The rules, and why each one is written the way it is:

**LTL is per pallet, keyed on the pallet.** Handling is charged per pallet
actually taken in, at the rate frozen on the job. Keying each charge on the
pallet's own token is what lets pallets that arrive in a second batch be
invoiced on their own after the first batch has already been billed.

**FTL is one fee, and it replaces LTL — it never stacks.** Charging a flat fee
and then per-pallet handling for the same scope is the single most likely
billing error in a cross-dock, so the mode is chosen up front and the engine
can only ever produce one of the two.

**Storage runs from each pallet's own receipt, in started 24-hour periods,
after a free first day.** Three things follow, and all three are deliberate:
an internal move cannot restart the clock (the clock only ever reads the
receipt time); a partial release stops the clock for the pallets that left and
for no others; and a pallet still sitting on the dock after being "prepared for
release" keeps accruing, because prepared is not released.

**A zero rate is not a free service.** Labelling in particular ships with a
$0 placeholder, and billing it would give the work away silently. The engine
refuses unless the service was explicitly agreed free or carries a real rate.

Everything is idempotent by key and nothing already invoiced is ever touched,
so clicking a button twice cannot bill twice and a later release cannot
re-bill an earlier one.
"""
import math

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..service_map import (EXTRA, FTL, LABOUR, LABELING, LTL, PALLET_SUPPLY,
                           STORAGE, WRAPPING)


def _pfl_format_qty(quantity):
    """Print 4, not 4.0 — but keep a real fraction when there is one."""
    if abs(quantity - round(quantity)) < 1e-6:
        return "%d" % round(quantity)
    return ("%.2f" % quantity).rstrip("0").rstrip(".")

#: What prints on a customer's invoice. Deliberately free of pallet tokens,
#: internal notes and storage arithmetic — the invoice states the service and
#: the quantity, and the job holds the detail behind it.
CUSTOMER_LABELS = {
    LTL: "Cross-docking — LTL handling",
    FTL: "Cross-docking — FTL (per job)",
    STORAGE: "Dry storage after the first 24 hours",
    LABOUR: "Warehouse labour",
    WRAPPING: "Pallet shrink wrapping",
    PALLET_SUPPLY: "Pallet supply",
    LABELING: "Pallet labelling",
    EXTRA: "Agreed extra",
}

#: Services whose quantity is a recorded count rather than something derived
#: from the pallets. Their charges are computed as a *delta* against what has
#: already been charged, so raising the count after an invoice was posted
#: produces a new charge instead of rewriting the posted one.
COUNTED_SERVICES = {
    WRAPPING: "wrapping_pallets",
    PALLET_SUPPLY: "pallets_supplied",
    LABELING: "labeling_pallets",
}


def storage_periods(hours):
    """Started additional 24-hour periods after the free first day."""
    if hours <= 24.0:
        return 0
    return int(math.ceil((hours - 24.0) / 24.0))


class PremaPflWarehouseJob(models.Model):
    _inherit = "prema.pfl.warehouse.job"

    # ── charge construction ────────────────────────────────────────────
    def _pfl_charge_rate(self, service):
        self.ensure_one()
        rate = self._pfl_rate_for(service)
        if not rate:
            raise UserError(_(
                "%(service)s is not part of job %(job)s, so it cannot be "
                "charged. Confirm the job so its accepted rates are frozen.",
                service=service, job=self.name))
        return rate._pfl_billable_rate(service), rate

    def _pfl_upsert_charge(self, key, vals):
        """Create or refresh the charge identified by `key`.

        A charge that has reached an invoice is left exactly as it is: it is
        the customer's document now, and rewriting it would make the job and
        the invoice disagree.
        """
        Charge = self.env["prema.pfl.charge"].sudo()
        existing = Charge.search([("idempotency_key", "=", key)], limit=1)
        vals = dict(vals, idempotency_key=key, job_id=self.id)
        if existing:
            if existing.invoiced:
                return existing
            existing.write(vals)
            return existing
        return Charge.create(vals)

    def _pfl_all_charges(self):
        """This job's charges, read from the database rather than the cache.

        The engine writes through `sudo()` (see `_pfl_upsert_charge`), and a
        write in a different environment is not guaranteed to refresh the
        one-to-many cache on this record. Reading the ledger authoritatively
        each time is what keeps "what has already been charged" true — and a
        stale answer there means either a duplicated charge or a lost one.
        """
        self.ensure_one()
        return self.env["prema.pfl.charge"].search([("job_id", "=", self.id)])

    def _pfl_prune_charges(self, produced_keys):
        """Drop uninvoiced charges the engine no longer produces.

        This is what makes a correction work: correct a release time backwards
        and the storage period that no longer applies disappears — unless it
        was already invoiced, in which case it stays and a credit note is the
        only honest way to take it back.
        """
        stale = self._pfl_all_charges().filtered(
            lambda c: not c.invoiced and c.idempotency_key not in produced_keys)
        stale.unlink()

    def _pfl_build_charges(self):
        """Rebuild this job's charge ledger from the recorded actuals."""
        self.ensure_one()
        produced = set()
        handled = self.package_ids.filtered(lambda p: p.pfl_received_at)

        # ── handling: LTL per pallet, or the single FTL fee ────────────
        if self.service_mode == "ftl":
            if handled:
                rate, _rate = self._pfl_charge_rate(FTL)
                key = "ftl:%s" % self.id
                produced.add(key)
                self._pfl_upsert_charge(key, {
                    "service": FTL,
                    "name": CUSTOMER_LABELS[FTL],
                    "basis": _("One base fee for this job (%s pallets in "
                               "scope)", len(handled)),
                    "product_id": _rate.product_id.product_variant_id.id,
                    "quantity": 1.0,
                    "uom_id": _rate.uom_id.id,
                    "unit_rate": rate,
                    "amount": rate,
                })
        else:
            rate, _rate = self._pfl_charge_rate(LTL)
            for package in handled:
                key = "ltl:%s:%s" % (self.id, package.pfl_qr_token)
                produced.add(key)
                self._pfl_upsert_charge(key, {
                    "service": LTL,
                    "name": CUSTOMER_LABELS[LTL],
                    "basis": _("Pallet %(pallet)s handled", pallet=package.name),
                    "product_id": _rate.product_id.product_variant_id.id,
                    "package_id": package.id,
                    "quantity": 1.0,
                    "uom_id": _rate.uom_id.id,
                    "unit_rate": rate,
                    "amount": rate,
                })

        # ── storage: per pallet, per started period ────────────────────
        rate, _rate = self._pfl_charge_rate(STORAGE)
        for package in handled:
            periods = storage_periods(package.pfl_custody_hours)
            for index in range(1, periods + 1):
                key = "storage:%s:%s:%s" % (
                    self.id, package.pfl_qr_token, index)
                produced.add(key)
                self._pfl_upsert_charge(key, {
                    "service": STORAGE,
                    "name": CUSTOMER_LABELS[STORAGE],
                    "basis": _(
                        "Pallet %(pallet)s, period %(index)s, %(hours).1f h in "
                        "custody (first 24 h included)",
                        pallet=package.name, index=index,
                        hours=package.pfl_custody_hours),
                    "product_id": _rate.product_id.product_variant_id.id,
                    "package_id": package.id,
                    "quantity": 1.0,
                    "uom_id": _rate.uom_id.id,
                    "unit_rate": rate,
                    "amount": rate,
                })

        # ── labour: per recorded session, one hour minimum per person ──
        labour_rate, labour_product = self._pfl_charge_rate(LABOUR)
        for labour in self.labour_ids:
            key = "labour:%s:%s" % (self.id, labour.id)
            produced.add(key)
            amount = labour.billable_hours * labour_rate
            self._pfl_upsert_charge(key, {
                "service": LABOUR,
                "name": CUSTOMER_LABELS[LABOUR],
                "basis": _(
                    "%(people)s person(s) × max(1 h, %(minutes).0f min) = "
                    "%(hours).2f person-hours",
                    people=labour.persons, minutes=labour.minutes,
                    hours=labour.billable_hours),
                "product_id": labour_product.product_id.product_variant_id.id,
                "quantity": labour.billable_hours,
                "uom_id": labour_product.uom_id.id,
                "unit_rate": labour_rate,
                "amount": amount,
            })

        # ── counted services: wrapping, pallet supply, labelling ───────
        for service, field_name in COUNTED_SERVICES.items():
            desired = self[field_name]
            existing = self._pfl_all_charges().filtered(
                lambda c, s=service: c.service == s)
            already = sum(existing.mapped("quantity"))
            # Charges already standing for this service are *produced* even
            # when nothing new is due. Forgetting that is how the pruner below
            # deletes a charge the moment the recorded count stops rising —
            # which is exactly the case for a service agreed free, whose
            # quantity never changes again.
            produced |= set(existing.mapped("idempotency_key"))
            delta = desired - already
            if delta <= 0:
                continue
            # `_pfl_charge_rate` refuses a zero rate that was never agreed
            # free, which is what stops labelling (a $0 placeholder) from
            # being given away without a decision.
            rate, _rate = self._pfl_charge_rate(service)
            key = "%s:%s:%s" % (service, self.id, int(already) + 1)
            produced.add(key)
            self._pfl_upsert_charge(key, {
                "service": service,
                "name": CUSTOMER_LABELS[service],
                "basis": _("%(count)s pallet(s) at the accepted rate",
                           count=int(delta)),
                "product_id": _rate.product_id.product_variant_id.id,
                "quantity": float(delta),
                "uom_id": _rate.uom_id.id,
                "unit_rate": rate,
                "amount": delta * rate,
            })

        # ── agreed extras: only once the customer approved them ────────
        for extra in self.extra_ids.filtered("approved"):
            key = "extra:%s:%s" % (self.id, extra.id)
            produced.add(key)
            self._pfl_upsert_charge(key, {
                "service": EXTRA,
                "name": extra.description,
                "basis": _("Agreed by %(who)s at %(when)s",
                           who=extra.approved_by.name or "",
                           when=extra.approved_at or ""),
                "quantity": extra.quantity,
                "uom_id": extra.uom_id.id,
                "unit_rate": extra.unit_rate,
                "amount": extra.amount,
            })

        self._pfl_prune_charges(produced)
        # The ledger was written through `sudo()`; drop the cached relation so
        # the counters on this record recompute from what is actually stored.
        self.invalidate_recordset(["charge_ids"])
        return True

    def action_pfl_review_charges(self):
        for job in self:
            job._pfl_require_confirmed(_("Reviewing charges"))
            job._pfl_build_charges()
        return True

    # ── invoicing ──────────────────────────────────────────────────────
    def _pfl_open_draft_invoice(self):
        """The job's own draft invoice, matched on the job — never on a booking.

        Matching a warehouse invoice by booking would hand a warehouse charge
        to a transportation invoice that happens to share a booking, which is
        exactly the confusion this module exists to avoid.
        """
        self.ensure_one()
        return self.invoice_ids.filtered(
            lambda m: m.state == "draft"
            and m.move_type == "out_invoice"
            and m.company_id == self.company_id)[:1]

    def _pfl_sale_line_for(self, product):
        self.ensure_one()
        if not self.sale_order_id:
            return self.env["sale.order.line"]
        return self.sale_order_id.order_line.filtered(
            lambda l: l.product_id == product)[:1]

    def action_pfl_sync_delivered(self):
        """Push the verified warehouse actuals onto the sales order.

        The service products stay on delivered-quantity invoicing, so the
        delivered column is the bridge between what the warehouse did and what
        the order says it did. Nothing is delivered until the warehouse says so.
        """
        for job in self:
            if not job.sale_order_id:
                continue
            totals = {}
            for charge in job.charge_ids.filtered(lambda c: c.product_id):
                totals.setdefault(charge.product_id, 0.0)
                totals[charge.product_id] += charge.quantity
            for product, quantity in totals.items():
                line = job._pfl_sale_line_for(product)
                if line and line.qty_delivered != quantity:
                    line.qty_delivered = quantity
        return True

    def action_pfl_create_invoice(self):
        """Create or extend the job's draft invoice. Never posts, never sends."""
        for job in self:
            job._pfl_require_confirmed(_("Invoicing"))
            job._pfl_build_charges()
            charges = job._pfl_all_charges()
            billable = charges.filtered(
                lambda c: not c.invoiced and c.amount)
            free = charges.filtered(
                lambda c: not c.invoiced and not c.amount)

            invoice = job._pfl_open_draft_invoice()
            if not billable and not invoice:
                raise UserError(_(
                    "Nothing to invoice on %(job)s yet. Release a pallet or "
                    "record the work first — a warehouse invoice follows the "
                    "actual work, it does not precede it.", job=job.name))

            if billable:
                if not invoice:
                    invoice = job.env["account.move"].create(
                        job._pfl_invoice_header_vals())
                # One line per (product, rate): the customer sees "4 × $12",
                # while the job keeps four separate charge records behind it.
                groups = {}
                for charge in billable:
                    key = (charge.product_id.id, charge.unit_rate)
                    groups[key] = groups.get(
                        key, job.env["prema.pfl.charge"]) | charge
                for (_product_id, unit_rate), charges in groups.items():
                    product = charges[0].product_id
                    quantity = sum(charges.mapped("quantity"))
                    invoice.write({"invoice_line_ids": [(0, 0,
                        job._pfl_invoice_line_vals(
                            product, quantity, unit_rate, charges))]})
                    line = invoice.invoice_line_ids[-1]
                    sale_line = job._pfl_sale_line_for(product)
                    if sale_line:
                        line.sale_line_ids = [(6, 0, sale_line.ids)]
                    charges.write({"invoiced": True, "invoice_line_id": line.id})

            # A service agreed free has nothing to bill, but it is closed so it
            # stops appearing in the uninvoiced total.
            free.write({"invoiced": True})

            job.action_pfl_sync_delivered()
            job.message_post(body=_(
                "Draft invoice %(invoice)s prepared. It is a draft: nothing is "
                "posted and nothing is sent.",
                invoice=(invoice.name or _("(new)"))))
        return True

    def _pfl_invoice_header_vals(self):
        self.ensure_one()
        return {
            "move_type": "out_invoice",
            "partner_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "currency_id": self.currency_id.id,
            "invoice_date": fields.Date.context_today(self),
            "invoice_origin": self.name,
            "ref": self.customer_reference or False,
            "prema_pfl_job_id": self.id,
        }

    def _pfl_invoice_line_vals(self, product, quantity, unit_rate, charges):
        """Build one customer-facing invoice line from a group of charges."""
        self.ensure_one()
        label = charges[0].name
        uom = charges[0].uom_id or product.uom_id
        return {
            "product_id": product.id,
            "name": "%s — %s %s" % (
                label, _pfl_format_qty(quantity), uom.name),
            "quantity": quantity,
            "price_unit": unit_rate,
            "product_uom_id": uom.id,
            "tax_ids": [(6, 0, product.taxes_id.filtered(
                lambda t: t.company_id == self.company_id).ids)],
        }

    def action_pfl_open_invoices(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Warehouse Invoices"),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("prema_pfl_job_id", "=", self.id)],
            "context": {"default_move_type": "out_invoice"},
        }

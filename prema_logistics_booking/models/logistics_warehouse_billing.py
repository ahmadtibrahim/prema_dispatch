# -*- coding: utf-8 -*-
"""Actual warehouse charges, billed from what really happened.

The quotation is an estimate. This computes what is actually owed from the
events on the pallets — when each one was received, when each one was released —
at the rates the customer already accepted, so a later catalogue price change
cannot move an agreed price.

Only warehouse service lines on a designated warehouse order are touched.
Transportation-only orders and other brands never reach this code.
"""
import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

LTL_CODE = "PFL-XD-LTL"
FTL_CODE = "PFL-XD-FTL"
STORAGE_CODE = "PFL-STOR-DAY"
LABEL_CODE = "PFL-LABEL"

#: Services whose actual quantity is measured from the dock events rather than
#: typed in by a member of staff.
MEASURED_CODES = (LTL_CODE, FTL_CODE, STORAGE_CODE)
FREE_HOURS = 24.0
CHARGE_PERIOD_HOURS = 24.0


def chargeable_storage_days(dwell_hours):
    """Started 24-hour periods AFTER the first free 24 hours.

    The first 24 hours of cross-dock holding are included, so the clock only
    starts charging once a full free day has passed:

        6 h -> 0      exactly 24 h -> 0      30 h -> 1
        exactly 48 h -> 1                    > 48 h -> 2

    Anything at or below the free window is free; after that every *started*
    period counts, which is what makes exactly 48 h one day and 48 h + 1 min
    two.
    """
    dwell_hours = max(0.0, float(dwell_hours or 0.0))
    if dwell_hours <= FREE_HOURS:
        return 0
    remaining = dwell_hours - FREE_HOURS
    periods = int(remaining // CHARGE_PERIOD_HOURS)
    if remaining % CHARGE_PERIOD_HOURS:
        periods += 1
    return periods


class SaleOrder(models.Model):
    _inherit = "sale.order"

    pfl_actual_charge_ids = fields.One2many(
        "logistics.warehouse.charge", "sale_order_id",
        string="Actual Warehouse Charges")
    pfl_actual_total = fields.Monetary(
        string="Actual Warehouse Charges", compute="_compute_pfl_actual_total",
        currency_field="currency_id")
    pfl_actual_invoice_id = fields.Many2one(
        "account.move", string="Actual Charge Invoice", copy=False)
    pfl_billing_state = fields.Selection(
        [("not_warehouse", "Not a warehouse booking"),
         ("unpriced", "Waiting for an agreed rate"),
         ("estimable", "Ready to calculate"),
         ("calculated", "Actuals calculated")],
        compute="_compute_pfl_billing_state")

    @api.depends("pfl_actual_charge_ids.amount")
    def _compute_pfl_actual_total(self):
        for order in self:
            order.pfl_actual_total = sum(order.pfl_actual_charge_ids.mapped("amount"))

    @api.depends("x_warehouse_service", "order_line.product_id")
    def _compute_pfl_billing_state(self):
        for order in self:
            if not order.x_warehouse_service:
                order.pfl_billing_state = "not_warehouse"
            elif order.pfl_unpriced_service_lines():
                order.pfl_billing_state = "unpriced"
            elif order.pfl_actual_charge_ids:
                order.pfl_billing_state = "calculated"
            else:
                order.pfl_billing_state = "estimable"

    def pfl_unpriced_service_lines(self):
        """Lines whose rate was never agreed.

        A service that needs an agreed rate is not free just because the
        catalogue carries a zero — billing has to stop and say so.
        """
        self.ensure_one()
        return self.order_line.filtered(
            lambda l: l.product_id.default_code not in (None, "")
            and l.product_id.default_code.startswith("PFL-")
            and l.price_unit == 0
            and l.product_id.default_code in (LABEL_CODE,))

    # ── measured quantities ────────────────────────────────────────────
    def _pfl_pallet_events(self):
        """Receipt and release instants for every pallet of this booking.

        One entry per native package, so a partial release keeps the right
        clock for each pallet and an internal move never restarts it.
        """
        self.ensure_one()
        Package = self.env["stock.quant.package"].sudo()
        packages = Package.search([("x_pfl_sale_order_id", "=", self.id)])
        events = []
        for package in packages:
            received = False
            released = False
            lines = self.env["stock.move.line"].sudo().search(
                [("result_package_id", "=", package.id)], order="date asc")
            for line in lines:
                if line.picking_id.picking_type_id.sequence_code == "PFL-IN" \
                        and line.state == "done":
                    received = received or line.date
                elif line.picking_id.picking_type_id.sequence_code == "PFL-OUT" \
                        and line.state == "done":
                    released = max(released or line.date, line.date)
            # the placement transfer (PFL-INT) deliberately does NOT count
            events.append({"package": package, "received": received,
                           "released": released})
        return events

    def pfl_measured_actuals(self):
        """The quantities that come from the dock rather than from a person."""
        self.ensure_one()
        events = self._pfl_pallet_events()
        received = [e for e in events if e["received"]]
        actuals = {}
        codes = self.order_line.mapped("product_id.default_code")
        if LTL_CODE in codes:
            # Charged once per pallet actually handled, in and out together.
            actuals[LTL_CODE] = float(len(received))
        if FTL_CODE in codes:
            actuals[FTL_CODE] = 1.0 if received else 0.0
        if STORAGE_CODE in codes:
            days = 0
            now = fields.Datetime.now()
            for event in received:
                start = event["received"]
                end = event["released"] or now
                days += chargeable_storage_days(
                    (end - start).total_seconds() / 3600.0)
            actuals[STORAGE_CODE] = float(days)
        return actuals, events

    # ── the calculation ────────────────────────────────────────────────
    def action_pfl_calculate_actuals(self):
        """Recompute this booking's actual charges from what really happened.

        Idempotent: the charges are rewritten in place, never appended, so
        running it twice does not double anything.
        """
        self.ensure_one()
        if not self.x_warehouse_service:
            raise UserError(_("This is not a warehouse service booking."))
        unpriced = self.pfl_unpriced_service_lines()
        if unpriced:
            raise UserError(_(
                "Billing cannot be calculated while a requested service has no "
                "agreed rate: %s. Agree the rate on the quotation first — a "
                "zero price is a placeholder, not a free service.",
                ", ".join(unpriced.mapped("product_id.display_name"))))

        measured, events = self.pfl_measured_actuals()
        # The charges are derived from this order and are read-only in the UI,
        # so the rewrite runs elevated: whoever may open the order may
        # recalculate it, and nobody edits the rows by hand.
        Charge = self.env["logistics.warehouse.charge"].sudo()
        Charge.search([("sale_order_id", "=", self.id)]).unlink()

        vals_list = []
        for line in self.order_line.filtered(
                lambda l: l.product_id.default_code in MEASURED_CODES):
            code = line.product_id.default_code
            # The accepted rate is the one on the order line, so a later
            # catalogue change cannot rewrite what was agreed.
            quantity = measured.get(code, 0.0)
            vals_list.append({
                "sale_order_id": self.id,
                "sale_order_line_id": line.id,
                "product_id": line.product_id.id,
                "charge_kind": "measured",
                "quantity": quantity,
                "price_unit": line.price_unit,
                "amount": quantity * line.price_unit,
                "detail": self._pfl_charge_detail(code, quantity, events),
            })
        for line in self.order_line.filtered(
                lambda l: l.product_id.default_code
                and l.product_id.default_code.startswith("PFL-")
                and l.product_id.default_code not in MEASURED_CODES):
            quantity = line.x_pfl_actual_qty or 0.0
            vals_list.append({
                "sale_order_id": self.id,
                "sale_order_line_id": line.id,
                "product_id": line.product_id.id,
                "charge_kind": "recorded",
                "quantity": quantity,
                "price_unit": line.price_unit,
                "amount": quantity * line.price_unit,
                "detail": _("Recorded at the dock."),
            })
        Charge.create(vals_list)
        self.message_post(body=_(
            "Actual warehouse charges recalculated: %s.",
            ", ".join("%s × %s" % (c.quantity, c.product_id.display_name)
                      for c in self.pfl_actual_charge_ids)))
        return True

    def _pfl_charge_detail(self, code, quantity, events):
        if code == STORAGE_CODE:
            lines = []
            for event in events:
                if not event["received"]:
                    continue
                end = event["released"] or fields.Datetime.now()
                hours = (end - event["received"]).total_seconds() / 3600.0
                lines.append("%s: %.1f h → %s day(s)" % (
                    event["package"].name, hours, chargeable_storage_days(hours)))
            return _("Chargeable days per pallet:\n") + "\n".join(lines)
        if code == LTL_CODE:
            return _("%s pallet(s) actually handled, charged once for in/out.")
        if code == FTL_CODE:
            return _("One flat fee for the job.")
        return ""

    # ── the invoice ────────────────────────────────────────────────────
    def action_pfl_create_actual_invoice(self):
        """One draft invoice carrying the actuals, at the accepted rates.

        Draft only: nothing is posted and nothing is emailed. A recalculation
        updates the same draft instead of adding a second one, and a posted
        invoice is never rewritten.
        """
        self.ensure_one()
        if not self.x_warehouse_service:
            raise UserError(_("This is not a warehouse service booking."))
        if not self.pfl_actual_charge_ids:
            self.action_pfl_calculate_actuals()

        existing = self.invoice_ids.filtered(lambda m: m.state == "draft")
        if existing:
            invoice = existing[0]
            invoice.invoice_line_ids.filtered(
                lambda l: l.pfl_warehouse_charge).unlink()
        else:
            # The native header (partner, currency, payment terms, fiscal
            # position, journal), then our own actual lines on top.
            invoice = self.env["account.move"].sudo().create(
                self._prepare_invoice())

        lines = []
        for charge in self.pfl_actual_charge_ids.filtered(lambda c: c.quantity):
            line = charge.sale_order_line_id
            lines.append((0, 0, {
                "product_id": charge.product_id.id,
                "name": "%s — actual" % charge.product_id.display_name,
                "quantity": charge.quantity,
                "price_unit": charge.price_unit,
                "tax_ids": [(6, 0, line.tax_id.ids)],
                "sale_line_ids": [(6, 0, [line.id])],
                "pfl_warehouse_charge": True,
            }))
        if lines:
            invoice.write({"invoice_line_ids": lines})
        self.write({"pfl_actual_invoice_id": invoice.id})
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": invoice.id,
            "view_mode": "form",
            "target": "current",
        }


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    pfl_warehouse_charge = fields.Boolean(
        string="Warehouse Actual Charge", copy=False,
        help="Created by the PF Logistics actual-charge calculation, so a "
             "recalculation can replace it without touching anything else on "
             "the draft.")


class LogisticsWarehouseCharge(models.Model):
    _name = "logistics.warehouse.charge"
    _description = "PF Logistics Actual Warehouse Charge"
    _order = "sale_order_id, id"

    sale_order_id = fields.Many2one(
        "sale.order", required=True, ondelete="cascade", index=True)
    sale_order_line_id = fields.Many2one("sale.order.line", ondelete="set null")
    product_id = fields.Many2one("product.product", required=True)
    charge_kind = fields.Selection(
        [("measured", "Measured from the dock events"),
         ("recorded", "Recorded at the dock")], required=True)
    quantity = fields.Float(digits="Product Unit of Measure")
    price_unit = fields.Float(string="Accepted Rate", digits="Product Price")
    amount = fields.Float(digits="Product Price")
    detail = fields.Text()
    currency_id = fields.Many2one(
        related="sale_order_id.currency_id", store=True)

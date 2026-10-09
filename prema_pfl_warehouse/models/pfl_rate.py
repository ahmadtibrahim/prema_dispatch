# -*- coding: utf-8 -*-
"""The accepted rates, frozen before the work starts.

The catalogue price is not the agreed price, and even when it is, it is not
agreed *forever*. If a charge is computed from `product.list_price` at the
moment of billing, then repricing the catalogue next month silently rewrites
what a customer already accepted this month — and the invoice that arrives
disagrees with the quotation they said yes to.

So the agreed rate is copied onto the job the moment the job is confirmed, and
every charge reads the copy. A later catalogue change then cannot reach a
confirmed job, which is the only behaviour a customer would recognise as fair.

The rate is sourced, in order, from the accepted sale order line for that
service, then from the catalogue. Which one it came from is recorded, so
"why is this $12?" has an answer.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..service_map import CHARGEABLE_SERVICES, SERVICE_LABELS, product_for


class PremaPflRate(models.Model):
    _name = "prema.pfl.rate"
    _description = "PF Logistics Accepted Service Rate"
    _order = "job_id, service"

    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", required=True, ondelete="cascade", index=True)
    service = fields.Selection(
        [(code, SERVICE_LABELS[code]) for code in CHARGEABLE_SERVICES],
        required=True)
    product_id = fields.Many2one(
        "product.template", string="Service Product", ondelete="restrict")
    unit_rate = fields.Float(
        string="Accepted Rate", digits="Product Price", required=True)
    uom_id = fields.Many2one(
        "uom.uom", string="Unit of Measure",
        default=lambda self: self.env.ref("uom.product_uom_unit", False))
    source = fields.Selection(
        [("sale_order", "From the accepted sale order"),
         ("catalogue", "From the catalogue at confirmation"),
         ("manual", "Entered by hand on the job"),
         ("free", "Agreed free of charge")],
        required=True, default="catalogue")
    free_service = fields.Boolean(
        string="Agreed Free",
        help="Set only when the customer has explicitly accepted this service "
             "at no charge. A zero rate without this flag is a missing price, "
             "not a free service, and will not be billed as zero.")
    currency_id = fields.Many2one(related="job_id.currency_id")
    note = fields.Char()

    _sql_constraints = [
        ("pfl_rate_service_uniq", "unique(job_id, service)",
         "This job already has an accepted rate for that service."),
    ]

    @api.model
    def _pfl_rate_for(self, job, service):
        rate = self.search(
            [("job_id", "=", job.id), ("service", "=", service)], limit=1)
        return rate

    def _pfl_billable_rate(self, service):
        """The rate to charge, or an error explaining what is missing.

        A zero rate is only billable when it was *agreed* as free. Silently
        billing $0 for a priced service is how a warehouse gives work away; the
        refusal names the service so the operator can agree a rate instead.
        """
        self.ensure_one()
        if self.free_service:
            return 0.0
        if not self.unit_rate:
            raise UserError(_(
                "No accepted rate for %(service)s on job %(job)s. The catalogue "
                "price is zero, so the job would bill nothing for work that was "
                "done. Agree a rate, or mark the service explicitly free.",
                service=SERVICE_LABELS.get(service, service),
                job=self.job_id.display_name))
        return self.unit_rate


def build_rate_vals(env, job):
    """Read the accepted rates for `job` off its sale order, then the catalogue."""
    rates = []
    order = job.sale_order_id
    for service in CHARGEABLE_SERVICES:
        product = product_for(env, service)
        if not product:
            # No catalogue row for this service: leave it absent rather than
            # inventing a rate. The charge engine will say what is missing.
            continue
        line = order.order_line.filtered(
            lambda l: l.product_id.product_tmpl_id == product)[:1] if order \
            else order.order_line
        if line:
            rate, source = line.price_unit, "sale_order"
        else:
            rate, source = product.list_price, "catalogue"
        rates.append((0, 0, {
            "service": service,
            "product_id": product.id,
            "unit_rate": rate,
            "uom_id": product.uom_id.id,
            "source": source,
        }))
    return rates

# -*- coding: utf-8 -*-
"""Keep commercial records out of the warehouse operator's reach.

**Why this is code and not an access rule.** Odoo's access is purely additive:
`ir.model.access` grants, and `ir.rule` narrows — but a record rule set for one
group is OR-ed with the rule sets of every other group the user holds, so a
"restrictive" rule can never take away what another group grants. This database
grants the commercial models to `base.group_user` itself (and `base.group_user`
here also implies twenty-one extra groups), so no rule or ACL can subtract them
without editing that shared group — which would change every internal user.
Denying therefore has to happen in code.

**One choke point.** `BaseModel._check_access(operation)` is the single method
every ORM operation passes through before reading, writing, creating or
unlinking — searches, direct reads, RPC calls, report rendering and
related-field lookups all end up here. It is only called when the environment is
not `sudo`, so privileged server code is unaffected and the guard is applied to
whoever is actually asking.

**Scoped, not global.** The policy below only ever applies to a user in the
warehouse *operator* role — a stock user who holds no commercial, accounting,
dispatch or manager role — so administrators, salespeople, accountants and
dispatch managers are untouched. Nothing is hidden by menu; these are access
errors raised at the model.
"""
from odoo import _, models
from odoo.exceptions import AccessError

#: What the operator must not reach, and in which direction.
#: `sale*` — quotations, prices, totals. `account*` — invoices and accounting.
#: `product.pricelist*` — the price lists. `crm*` — the customer pipeline.
DENIED = {
    "sale.order": ("read", "write", "create", "unlink"),
    "sale.order.line": ("read", "write", "create", "unlink"),
    "sale.order.template": ("read", "write", "create", "unlink"),
    "sale.order.template.line": ("read", "write", "create", "unlink"),
    "sale.order.option": ("read", "write", "create", "unlink"),
    "sale.report": ("read",),
    "product.pricelist": ("read", "write", "create", "unlink"),
    "product.pricelist.item": ("read", "write", "create", "unlink"),
    "account.move": ("read", "write", "create", "unlink"),
    "account.move.line": ("read", "write", "create", "unlink"),
    "account.payment": ("read", "write", "create", "unlink"),
    "account.payment.register": ("read", "write", "create"),
    "account.journal": ("read", "write", "create", "unlink"),
    "account.tax": ("read", "write", "create", "unlink"),
    "account.tax.repartition.line": ("read", "write", "create", "unlink"),
    "account.fiscal.position": ("read", "write", "create", "unlink"),
    "account.fiscal.position.tax": ("read", "write", "create", "unlink"),
    "account.account": ("read", "write", "create", "unlink"),
    "account.partial.reconcile": ("read", "write", "create", "unlink"),
    "account.bank.statement": ("read", "write", "create", "unlink"),
    "account.bank.statement.line": ("read", "write", "create", "unlink"),
    "account.invoice.report": ("read",),
    "crm.lead": ("read", "write", "create", "unlink"),
    "crm.stage": ("read", "write", "create", "unlink"),
    "crm.team": ("write", "create", "unlink"),
}

#: Holding any of these puts a user outside the operator restriction — they are
#: commercial, accounting, dispatch or managerial roles, and §4 only asks to
#: restrict the operator.
OUTSIDE_ROLES = (
    "sales_team.group_sale_salesman",
    "sales_team.group_sale_manager",
    "account.group_account_invoice",
    "account.group_account_readonly",
    "account.group_account_manager",
    "stock.group_stock_manager",
    "base.group_erp_manager",
    "base.group_system",
    "prema_dispatch.group_dispatch_manager",
)


def _holds(user, xmlid):
    group = user.env.ref(xmlid, raise_if_not_found=False)
    return bool(group) and user.has_group(xmlid)


def is_warehouse_operator(env):
    """A stock user with no commercial, accounting, dispatch or manager role."""
    user = env.user
    if user._is_public() or not _holds(user, "stock.group_stock_user"):
        return False
    return not any(_holds(user, xmlid) for xmlid in OUTSIDE_ROLES)


class BaseModel(models.AbstractModel):
    _inherit = "base"

    def _check_access(self, operation):
        denied = DENIED.get(self._name)
        if denied and operation in denied and is_warehouse_operator(self.env):
            raise AccessError(_(
                "%(model)s is commercial data and is not part of the warehouse "
                "operator role. Ask an office colleague if you need it.",
                model=self._name))
        return super()._check_access(operation)

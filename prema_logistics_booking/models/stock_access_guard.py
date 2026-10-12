# -*- coding: utf-8 -*-
"""Keep a warehouse OPERATOR out of the operations that are not theirs.

Odoo's access rules are additive: a group either grants a capability or says
nothing, and there is no deny. `stock.group_stock_user` is required to work
Barcode at all, and it happens to grant two things §4 rules out for an operator
— creating a scrap, and applying an inventory adjustment through the ORM.

Those two are therefore refused here for a user who is an operator and not a
manager. Nothing else about stock is touched: receiving, placement, internal
movements and validation never call these paths, so the Barcode workflow is
unaffected. Managers keep both capabilities untouched.
"""
from odoo import _, models
from odoo.exceptions import AccessError


def is_warehouse_operator(env):
    """A stock user who is NOT a stock manager — the role Barcode runs in."""
    user = env.user
    if user._is_public() or env.su:
        return False
    return (user.has_group("stock.group_stock_user")
            and not user.has_group("stock.group_stock_manager"))


class StockScrap(models.Model):
    _inherit = "stock.scrap"

    def create(self, vals_list):
        if is_warehouse_operator(self.env):
            raise AccessError(_(
                "Scrapping stock is not part of the warehouse operator role. "
                "Ask a warehouse manager to scrap it."))
        return super().create(vals_list)

    def action_validate(self):
        if is_warehouse_operator(self.env):
            raise AccessError(_(
                "Scrapping stock is not part of the warehouse operator role."))
        return super().action_validate()


#: Setting these is what an inventory count is; applying it is blocked below,
#: and staging it is blocked here so the operator cannot even prepare one.
INVENTORY_FIELDS = frozenset({
    "inventory_quantity", "inventory_quantity_set", "inventory_diff_quantity",
    "inventory_date", "user_id",
})


class StockQuant(models.Model):
    _inherit = "stock.quant"

    def write(self, vals):
        if is_warehouse_operator(self.env) and (set(vals) & INVENTORY_FIELDS):
            raise AccessError(_(
                "Counting stock is not part of the warehouse operator role. Ask "
                "a warehouse manager to adjust it."))
        return super().write(vals)

    def action_apply_inventory(self):
        """The 'apply' of a physical inventory count."""
        if is_warehouse_operator(self.env):
            raise AccessError(_(
                "Applying inventory adjustments is not part of the warehouse "
                "operator role. Ask a warehouse manager to adjust stock."))
        return super().action_apply_inventory()

    def _apply_inventory(self):
        if is_warehouse_operator(self.env):
            raise AccessError(_(
                "Applying inventory adjustments is not part of the warehouse "
                "operator role."))
        return super()._apply_inventory()

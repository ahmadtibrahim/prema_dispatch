# -*- coding: utf-8 -*-
"""Who counts as a PF Logistics warehouse operator.

The restriction itself is a set of **conditional global record rules**
(`data/logistics_warehouse_operator_rules.xml`), not a model override: global
rules are AND-ed with the group rules, so `[(1, '=', 1)]` for everyone else is a
true no-op while `[('id', '=', 0)]` for the operator denies the record. That is
the supported mechanism, it needs no change to `base.group_user`, and it does not
touch a single line of core Sales or Accounting code.

This module keeps only the role predicate, for the narrow endpoint checks that
record rules cannot reach (a controller that reads under `sudo()`, or through raw
SQL — neither of which any record rule or ACL check can see).
"""
from odoo import models  # noqa: F401  (kept so the module still loads cleanly)


def is_warehouse_operator(env):
    """True for a PF Logistics warehouse operator.

    Membership of the dedicated role group, and nothing else — so the rule and
    any endpoint check key on the same fact, and adding someone to the role is a
    single group membership.
    """
    user = env.user
    if user._is_public():
        return False
    return user.has_group(
        "prema_logistics_booking.group_pfl_warehouse_operator")

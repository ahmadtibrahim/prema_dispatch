# -*- coding: utf-8 -*-
"""Complete the customer-facing Sales Descriptions on the PFL services.

The eight `PFL-*` products were configured by hand and adopted by this module.
Their rates were right; their customer-facing wording was not — five of them
carried no Sales Description at all, so a quotation line showed only the
internal product name and the customer could not read what they were buying.

This sets `description_sale` only. It deliberately does not touch `list_price`
or `taxes_id`: an accepted rate is the commercial agreement and a migration
must never rewrite one. The products are matched by `default_code`, the same
natural key `hooks.py` adopts them by.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

DESCRIPTIONS = {
    "PFL-XD-LTL": (
        "LTL cross-docking: $12 per pallet for the combined in/out service. "
        "The first 24 hours of storage from actual receipt are included. "
        "After that, $2 per pallet per started 24-hour period until the "
        "pallet is released. Quotation amounts are estimates; final billing "
        "follows the pallets actually handled and the actual receipt and "
        "release times."
    ),
    "PFL-XD-FTL": (
        "FTL cross-docking: $225 flat fee for the agreed job scope — in/out "
        "handling of the whole job in one movement. The first 24 hours of "
        "storage from actual receipt are included. After that, $2 per pallet "
        "per started 24-hour period until the pallet is released. One FTL fee "
        "per job; never combined with per-pallet LTL handling for the same "
        "movement."
    ),
    "PFL-STOR-DAY": (
        "Additional dry storage: $2 per pallet per started 24-hour period, "
        "charged after the first 24 hours (which are included with "
        "cross-docking). Measured from each pallet's actual receipt to its "
        "actual release, so a pallet is billed for the time it was really "
        "here."
    ),
    "PFL-LAB-HR": (
        "Warehouse labour: $40 per person-hour, with a minimum of one hour "
        "per person per job."
    ),
    "PFL-WRAP": "Pallet shrink wrapping: $7.50 per pallet wrapped.",
    "PFL-PAL-SUP": "Pallet supply: $12 per pallet supplied.",
    "PFL-LABEL": (
        "Pallet labelling: charged at the rate agreed on the quotation. This "
        "service requires an agreed rate — a zero price is a placeholder, not "
        "a free service."
    ),
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Template = env["product.template"].with_context(active_test=False)
    written = []
    for default_code, text in DESCRIPTIONS.items():
        template = Template.search(
            [("default_code", "=", default_code)], limit=1)
        if not template:
            _logger.warning(
                "PF Logistics warehouse: service product %s not found; its "
                "Sales Description was not set.", default_code)
            continue
        if template.description_sale == text:
            continue
        template.description_sale = text
        written.append(default_code)
    _logger.info(
        "PF Logistics warehouse: Sales Descriptions set on %s product(s): %s",
        len(written), ", ".join(written) or "(none — already current)")

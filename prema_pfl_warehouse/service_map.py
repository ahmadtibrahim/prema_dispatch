# -*- coding: utf-8 -*-
"""The one place that knows which existing product delivers which service.

There is no second catalogue. `hooks.py` adopts the eight `PFL-*` products that
are already live and gives each of them an XML ID; this table is the only
mapping between a warehouse service and the product that prices it. Adding a
service means adding a row here and the product's XML ID in `hooks.py`.

Keeping this as data (not as a Many2one to a configuration model) is deliberate:
the products already exist, their prices already exist, and a configuration
record would be a second place for the same fact to be wrong.
"""

LTL = "ltl"
FTL = "ftl"
STORAGE = "storage"
LABOUR = "labour"
WRAPPING = "wrapping"
PALLET_SUPPLY = "pallet_supply"
LABELING = "labeling"
EXTRA = "extra"

#: service code -> XML ID of the adopted product template.
SERVICE_XMLIDS = {
    LTL: "prema_pfl_warehouse.pfl_product_pfl_xd_ltl",
    FTL: "prema_pfl_warehouse.pfl_product_pfl_xd_ftl",
    STORAGE: "prema_pfl_warehouse.pfl_product_pfl_stor_day",
    LABOUR: "prema_pfl_warehouse.pfl_product_pfl_lab_hr",
    WRAPPING: "prema_pfl_warehouse.pfl_product_pfl_wrap",
    PALLET_SUPPLY: "prema_pfl_warehouse.pfl_product_pfl_pal_sup",
    LABELING: "prema_pfl_warehouse.pfl_product_pfl_label",
}

#: The tracking product every customer-freight stock move uses. It is a
#: storable `consu`, so it gives real quants and real packages without
#: pretending customer freight is merchandise we own or sell.
FREIGHT_PALLET_XMLID = "prema_pfl_warehouse.pfl_product_pfl_cust_pal"

SERVICE_LABELS = {
    LTL: "Cross-Docking LTL (per handled pallet)",
    FTL: "Cross-Docking FTL (per job)",
    STORAGE: "Dry Storage (per pallet per started 24 h)",
    LABOUR: "Warehouse Labour (per person-hour)",
    WRAPPING: "Pallet Shrink Wrapping (per pallet)",
    PALLET_SUPPLY: "Pallet Supply (per pallet)",
    LABELING: "Pallet Labeling (agreed rate required)",
    EXTRA: "Agreed Extra",
}

#: Services a job can bill. `extra` is always present because an agreed extra
#: is entered by hand rather than quoted from the catalogue.
CHARGEABLE_SERVICES = [LTL, FTL, STORAGE, LABOUR, WRAPPING, PALLET_SUPPLY,
                       LABELING, EXTRA]

#: Which services the customer may be charged for, per handling mode. An FTL
#: job pays one base fee and must never also pay per-pallet LTL handling for
#: the same scope — that mutual exclusion lives here so it cannot drift.
SERVICES_BY_MODE = {
    "ltl": [LTL, STORAGE, LABOUR, WRAPPING, PALLET_SUPPLY, LABELING, EXTRA],
    "ftl": [FTL, STORAGE, LABOUR, WRAPPING, PALLET_SUPPLY, LABELING, EXTRA],
}


def product_for(env, service):
    """Return the adopted product template for `service`, or an empty recordset.

    An empty return is a real answer, not an error: a database where the
    catalogue was never created must still install and still let the job be
    opened. The charge engine reports the missing service instead of inventing
    a price for it.
    """
    xmlid = SERVICE_XMLIDS.get(service)
    if not xmlid:
        return env["product.template"]
    return env.ref(xmlid, raise_if_not_found=False) or env["product.template"]


def freight_product(env):
    """The tracking product variant behind every customer-freight stock move.

    Returns a `product.product`, not a `product.template`: the adopted XML ID
    points at the template, and a stock move needs the variant. Handing the
    template to `stock.move` does not raise — it stores a null product, the
    picking validates, and the pallet silently never becomes stock. That is a
    warehouse that believes it is tracking freight and is not.
    """
    template = env.ref(FREIGHT_PALLET_XMLID, raise_if_not_found=False)
    if not template:
        return env["product.product"]
    return template.product_variant_id

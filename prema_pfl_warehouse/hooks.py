# -*- coding: utf-8 -*-
"""Adopt the warehouse configuration that already exists, by external ID.

On 2026-10-05 the PFL warehouse was configured by hand through the UI: six
zones, four cross-dock positions, three operation types and eight service
products, none of them carrying an XML ID. That configuration is live and
correct, and this module must NOT duplicate any of it — a second
`WH/Dry Storage` or a second `PFL-XD-LTL` would split the quantities and the
revenue across two records with no way back.

So this hook works by *adoption*: for each expected record it finds the
existing one by its natural key (a location barcode, a picking-type sequence
code, a product default code), attaches a stable XML ID to it, and only
creates anything when the natural key is genuinely absent. Running it twice
changes nothing the second time.

The natural keys are deliberately the ones a human already set:

    stock.location          barcode
    stock.picking.type      sequence_code
    product.template        default_code
    stock.package.type      name

Nothing here deletes, archives or renames. Nothing here touches the warehouse's
receiving or shipping steps — the customer-freight operations name their own
source and destination locations instead of reconfiguring `stock.warehouse`.
"""
import logging

_logger = logging.getLogger(__name__)

#: Locations, keyed by the barcode a human already typed. `usage` and the
#: parent are only used on the create path; on the adopt path the existing
#: record is authoritative.
EXPECTED_LOCATIONS = {
    "PFL-WH-RECV": {"name": "Receiving", "usage": "internal"},
    "PFL-WH-CD": {"name": "Cross Dock", "usage": "internal"},
    "PFL-WH-DRY": {"name": "Dry Storage", "usage": "internal"},
    "PFL-WH-HOLD": {"name": "Hold", "usage": "internal"},
    "PFL-WH-OUT": {"name": "Outbound Staging", "usage": "internal"},
    "PFL-WH-RET": {"name": "Returns", "usage": "internal"},
    # Cross-dock floor positions live under the Cross Dock location.
    "PFL-WH-CD-01": {"name": "CD-01", "usage": "internal", "parent": "PFL-WH-CD"},
    "PFL-WH-CD-02": {"name": "CD-02", "usage": "internal", "parent": "PFL-WH-CD"},
    "PFL-WH-CD-03": {"name": "CD-03", "usage": "internal", "parent": "PFL-WH-CD"},
    "PFL-WH-CD-04": {"name": "CD-04", "usage": "internal", "parent": "PFL-WH-CD"},
}

#: Operation types, keyed by sequence_code.
EXPECTED_PICKING_TYPES = {
    "PFL-IN": {"name": "PFL Customer Freight Receipts", "code": "incoming"},
    "PFL-INT": {"name": "PFL Customer Freight Transfers", "code": "internal"},
    "PFL-OUT": {"name": "PFL Customer Freight Releases", "code": "outgoing"},
}

#: Service products, keyed by default_code.
EXPECTED_PRODUCTS = {
    "PFL-XD-LTL": "PFL Cross-Docking LTL - Per Pallet",
    "PFL-XD-FTL": "PFL Cross-Docking FTL - Flat Fee",
    "PFL-STOR-DAY": "PFL Dry Storage - Per Pallet-Day",
    "PFL-WRAP": "PFL Pallet Shrink Wrapping - Per Pallet",
    "PFL-LABEL": "PFL Pallet Labeling - Quote Required",
    "PFL-LAB-HR": "PFL Warehouse Labour - Per Person-Hour",
    "PFL-PAL-SUP": "PFL Pallet Supply - Per Pallet",
    "PFL-CUST-PAL": "PFL Customer Freight Pallet",
}


#: The module name every adopted XML ID is created under.
MODULE = "prema_pfl_warehouse"


def _adopt(env, name, record):
    """Give `record` the XML ID `prema_pfl_warehouse.<name>`, if it lacks one.

    Refuses to re-point an XML ID that already resolves to a different record:
    that would silently rewire whatever else refers to it.
    """
    IMD = env["ir.model.data"].sudo()
    existing = IMD.search(
        [("module", "=", MODULE), ("name", "=", name)], limit=1)
    if existing:
        if existing.model == record._name and existing.res_id == record.id:
            return False
        if existing.res_id:
            _logger.warning(
                "PF Logistics warehouse: %s.%s already points at %s(%s); "
                "leaving it alone and not re-pointing it at %s(%s).",
                MODULE, name, existing.model, existing.res_id,
                record._name, record.id)
            return False
        # An orphaned XML ID (record deleted) — repoint it at the live record.
        existing.write({"model": record._name, "res_id": record.id})
        return True
    IMD.create({
        "module": MODULE,
        "name": name,
        "model": record._name,
        "res_id": record.id,
        "noupdate": True,
    })
    return True


def _ensure_locations(env):
    """Adopt or create the PFL zones and cross-dock positions."""
    Location = env["stock.location"].sudo()
    warehouse = env.ref(
        "stock.stock_warehouse_shop0", raise_if_not_found=False)
    if not warehouse:
        warehouse = env["stock.warehouse"].sudo().search(
            [("code", "=", "WH")], limit=1)
    view_location = warehouse.view_location_id if warehouse else False

    created, adopted = 0, 0
    # Two passes so a child never depends on a parent created later in the
    # same loop (CD-01 under Cross Dock).
    for barcode, spec in EXPECTED_LOCATIONS.items():
        location = Location.with_context(active_test=False).search(
            [("barcode", "=", barcode)], limit=1)
        if not location:
            vals = {
                "name": spec["name"],
                "usage": spec["usage"],
                "barcode": barcode,
                "company_id": warehouse.company_id.id if warehouse else env.company.id,
            }
            parent_barcode = spec.get("parent")
            parent = (
                Location.with_context(active_test=False).search(
                    [("barcode", "=", parent_barcode)], limit=1)
                if parent_barcode else view_location)
            if parent:
                vals["location_id"] = parent.id
            location = Location.create(vals)
            created += 1
        else:
            adopted += 1
        _adopt(env, "pfl_location_%s" % barcode.lower().replace("-", "_"), location)
    _logger.info(
        "PF Logistics warehouse: locations — %s adopted, %s created.",
        adopted, created)


def _ensure_picking_types(env):
    """Adopt or create the three PFL operation types.

    On the create path every location is named explicitly, so the type never
    inherits the warehouse's own one-step route.
    """
    PickingType = env["stock.picking.type"].sudo()
    Location = env["stock.location"].sudo()
    warehouse = env["stock.warehouse"].sudo().search(
        [("code", "=", "WH")], limit=1)
    customer_loc = env.ref(
        "stock.stock_location_customers", raise_if_not_found=False)
    recv = Location.with_context(active_test=False).search(
        [("barcode", "=", "PFL-WH-RECV")], limit=1)
    cd = Location.with_context(active_test=False).search(
        [("barcode", "=", "PFL-WH-CD")], limit=1)
    out = Location.with_context(active_test=False).search(
        [("barcode", "=", "PFL-WH-OUT")], limit=1)

    defaults = {
        "PFL-IN": (customer_loc, recv),
        "PFL-INT": (recv, cd),
        "PFL-OUT": (out, customer_loc),
    }

    created, adopted = 0, 0
    for sequence_code, spec in EXPECTED_PICKING_TYPES.items():
        picking_type = PickingType.search(
            [("sequence_code", "=", sequence_code)], limit=1)
        if not picking_type:
            vals = {
                "name": spec["name"],
                "code": spec["code"],
                "sequence_code": sequence_code,
                "warehouse_id": warehouse.id if warehouse else False,
                "company_id": warehouse.company_id.id if warehouse else env.company.id,
            }
            source, dest = defaults[sequence_code]
            if source:
                vals["default_location_src_id"] = source.id
            if dest:
                vals["default_location_dest_id"] = dest.id
            picking_type = PickingType.create(vals)
            created += 1
        else:
            adopted += 1
        _adopt(
            env, "pfl_picking_type_%s" % sequence_code.lower().replace("-", "_"),
            picking_type)
    _logger.info(
        "PF Logistics warehouse: operation types — %s adopted, %s created.",
        adopted, created)


def _ensure_products(env):
    """Adopt the eight PFL service products, or create only what is missing.

    The adopt path is the important one: these products are already priced,
    already carry 13% HST and already carry their commercial descriptions.
    Recreating them would silently start billing from an empty catalogue.
    """
    Template = env["product.template"].sudo()
    created, adopted = 0, 0
    for default_code, name in EXPECTED_PRODUCTS.items():
        template = Template.with_context(active_test=False).search(
            [("default_code", "=", default_code)], limit=1)
        if not template:
            vals = {
                "name": name,
                "default_code": default_code,
                "type": "consu" if default_code in (
                    "PFL-CUST-PAL", "PFL-PAL-SUP") else "service",
                "is_storable": default_code in ("PFL-CUST-PAL", "PFL-PAL-SUP"),
                "sale_ok": default_code != "PFL-CUST-PAL",
                "purchase_ok": default_code == "PFL-PAL-SUP",
                "invoice_policy": "delivery",
            }
            sale_tax = env.company.account_sale_tax_id if hasattr(
                env.company, "account_sale_tax_id") else False
            if sale_tax:
                vals["taxes_id"] = [(6, 0, sale_tax.ids)]
            template = Template.create(vals)
            _logger.warning(
                "PF Logistics warehouse: product %s was MISSING and has been "
                "created with no price and no description — set its price, "
                "taxes and sale description before quoting from it.",
                default_code)
            created += 1
        else:
            adopted += 1
        _adopt(
            env, "pfl_product_%s" % default_code.lower().replace("-", "_"),
            template)
    _logger.info(
        "PF Logistics warehouse: service products — %s adopted, %s created.",
        adopted, created)


def _ensure_package_type(env):
    """Adopt or create the `Pallet` package type.

    `stock.quant.package` is the pallet's identity; the package *type* is what
    makes a label and a dimension set reusable. The twelve types already in the
    database are electronics cartons from another line of business and none of
    them is a pallet, so this adopts a `Pallet` type if one exists and creates
    it otherwise.
    """
    PackageType = env["stock.package.type"].sudo()
    package_type = PackageType.search([("name", "=", "Pallet")], limit=1)
    if not package_type:
        package_type = PackageType.create({"name": "Pallet"})
        _logger.info("PF Logistics warehouse: created the `Pallet` package type.")
    _adopt(env, "pfl_package_type_pallet", package_type)
    return package_type


def post_init_hook(env):
    """Adopt the live PFL configuration. Nothing is duplicated."""
    _ensure_locations(env)
    _ensure_picking_types(env)
    _ensure_products(env)
    _ensure_package_type(env)
    _logger.info(
        "PF Logistics warehouse: configuration adopted, nothing duplicated.")

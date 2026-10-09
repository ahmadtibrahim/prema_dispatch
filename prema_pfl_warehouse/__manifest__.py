# -*- coding: utf-8 -*-
{
    "name": "PF Logistics Warehouse",
    "version": "18.0.1.0.0",
    "category": "Inventory/Inventory",
    "summary": "Dry cross-dock and short-term storage job: receive, store, hold, "
               "release, charge and draft-invoice customer-owned freight.",
    "description": """
PF Logistics warehouse execution for PremaFirm Inc.

One physical pallet is one `stock.quant.package`. Native stock movements stay the
quantity authority; this module never keeps a second pallet ledger. The warehouse
job is a thin operational header over records that already exist — the accepted
sale order, the native packages, and the stock pickings the job's actions create.

What it adds
------------
* `prema.pfl.warehouse.job` — one job per accepted sales order (a transportation
  booking is optional and never required).
* Pallet identity on `stock.quant.package`: an opaque UUID QR token, the actual
  freight owner, and per-pallet received/staged/released timestamps and operators.
* `prema.pfl.custody.event` — an append-only per-pallet event log. Corrections
  are new events carrying a reason, never an overwrite.
* `prema.pfl.rate` — the accepted rates, frozen when the job is confirmed, so a
  later catalogue price change cannot alter an accepted job.
* `prema.pfl.charge` — deterministic charge lines (LTL handling, the single FTL
  base fee, storage per started period, labour, wrapping, pallet supply, agreed
  extras), each with an idempotency key so a repeated action cannot double-bill.
* `prema.pfl.barcode.alias` — a customer's own pallet barcode, scoped to that
  customer, without ever merging two pallets that share a code.
* `prema.pfl.card` — the off-pallet identification card and exclusive position
  used when a customer prohibits an attached label.

What it deliberately does not do
--------------------------------
* It does not create, rename or re-home the existing PFL products, locations or
  operation types: `hooks.py` adopts them by external ID and gives them stable
  XML IDs. Nothing is duplicated and nothing is deleted.
* It does not change the warehouse's receiving or shipping steps. Every
  customer-freight operation names its own source and destination.
* It does not post, confirm, register payment for or email an invoice. Drafts
  only — the accounting decision stays with a person.
* It does not touch MoonVella or the transportation booking/invoice paths.
""",
    "author": "PremaFirm",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "stock",
        "sale_stock",
        # Reuse of the existing dispatch configuration and its access groups.
        "prema_dispatch",
        # The transportation booking is an OPTIONAL link on the job. Declaring the
        # dependency (rather than pointing a Many2one up at a model this module
        # does not depend on) is what keeps the comodel resolvable at field-setup
        # time — see prema_logistics_booking/models/dispatch_stop_extension.py:1-11
        # for the `_unknown` failure this avoids.
        "prema_logistics_booking",
    ],
    "data": [
        "security/pfl_security.xml",
        "security/ir.model.access.csv",
        "data/pfl_sequence.xml",
        # Charge views first: the job form links to the charge action by XML ID,
        # and a forward reference would not resolve while the views load.
        "views/pfl_charge_views.xml",
        "views/pfl_job_views.xml",
        "views/pfl_package_views.xml",
        "views/pfl_alias_views.xml",
        "views/pfl_card_views.xml",
        "views/pfl_menus.xml",
        "views/pfl_warehouse_app.xml",
        "report/pfl_report_paperformat.xml",
        "report/pfl_pallet_label.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}

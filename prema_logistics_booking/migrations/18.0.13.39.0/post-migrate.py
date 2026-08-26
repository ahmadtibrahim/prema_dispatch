"""18.0.13.39.0 post-migration — Phases 11-16 (booking module side).

Seeds ONLY the configuration authorities Phases 11-16 need. Nothing
operational is touched:

1. `logistics.hub_transfer_cost` — default hub / transfer leg cost
   authority for scenario costing (50.00).
2. `logistics.minimum_margin_pct` — margin-guard threshold (10.0). The
   guard only WARNS (booking.margin_warning) — never blocks.
3. `logistics.market_buy_rate_per_km` — configured market BUY authority
   used ONLY when a carrier has no accepted offer AND no lane-rate card.
   Default 0.0 = authority OFF → such scenarios are CARRIER RATE
   REQUIRED instead of inventing a figure.
4. `logistics.allow_cross_border_subcontract` — explicit brokerage /
   interlining authority. Default False → any cross-border subcontract
   scenario is COMPLIANCE REVIEW REQUIRED.
5. Seeds the ONE "Subcontracted Freight Service" purchase product
   (default_code SUBCONTRACTED_FREIGHT_SVC, service, purchasable) —
   idempotent: existing product by default_code OR name is reused;
   company/category defaults decide accounts and taxes (never hardcoded).

Explicitly NOT done (migration rules):
  • NO mass update of historical bookings,
  • NO auto-marking of any partner as carrier,
  • NO lane rates / offers / POs / vendor bills created,
  • NO existing purchase order touched.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

DEFAULT_CODE = "SUBCONTRACTED_FREIGHT_SVC"
PRODUCT_NAME = "Subcontracted Freight Service"


def _param(env, key, value):
    existing = env["ir.config_parameter"].search([("key", "=", key)])
    if existing:
        return False  # never overwrite a manual configuration
    env["ir.config_parameter"].create({"key": key, "value": value})
    return True


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    seeded = 0

    for key, value in (
        ("logistics.hub_transfer_cost", "50.0"),
        ("logistics.minimum_margin_pct", "10.0"),
        ("logistics.market_buy_rate_per_km", "0.0"),
        ("logistics.allow_cross_border_subcontract", "False"),
    ):
        if _param(env, key, value):
            seeded += 1

    Product = env["product.product"].sudo()
    product = Product.search([("default_code", "=", DEFAULT_CODE)], limit=1)
    if not product:
        product = Product.search(
            [("name", "=", PRODUCT_NAME), ("type", "=", "service")], limit=1)
    if not product:
        product = Product.create({
            "name": PRODUCT_NAME,
            "default_code": DEFAULT_CODE,
            "type": "service",
            "purchase_ok": True,
            "sale_ok": False,
            "description": ("Freight subcontract buy-side service line — the "
                            "Carrier Rate Confirmation purchase order uses "
                            "this product. Accounts/taxes follow the "
                            "company/category defaults."),
        })
    _logger.info("18.0.13.39.0: seeded %s config params; freight product %s "
                 "(id %s)", seeded, product.name, product.id)

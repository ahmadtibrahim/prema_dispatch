# ─────────────────────────────────────────────────────────────────────
# 18.0.13.40.0 — Freight Tax Architecture (accountant guide authority)
#
# 1. Nova Scotia canonical mapping → 14% HST (sale) — the accountant's
#    rate guide effective 2026-07-30 (the 14% record already exists in
#    the chart of accounts; only the mapping was 15%).
# 2. Seed the NEW carrier BUY interlining ICP to the purchase-use
#    0% interlining tax — the buy-side authority, separate from the
#    customer sell tax.
# 3. Drop the orphaned manual-review-default param (the removed legacy
#    settings block was its only reader; the booking engine never read it).
#
# Lookups are by name/amount/use — never hardcoded ids. Idempotent.
# ─────────────────────────────────────────────────────────────────────
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    def _tax_id(amount, type_tax_use, name_filter, company_id=1):
        cr.execute(
            "SELECT id FROM account_tax "
            "WHERE amount = %s AND type_tax_use = %s AND active "
            "AND company_id = %s AND name::text ILIKE %s "
            "ORDER BY id LIMIT 1",
            (amount, type_tax_use, company_id, name_filter))
        row = cr.fetchone()
        return row[0] if row else None

    ns_tax = _tax_id(14.0, "sale", "%HST%")
    buy_tax = _tax_id(0.0, "purchase", "%Int%")

    if ns_tax:
        cr.execute(
            "UPDATE ir_config_parameter SET value = %s "
            "WHERE key = 'logistics.freight_tax_ns_id'",
            (str(ns_tax),))
        _logger.info("freight tax: logistics.freight_tax_ns_id -> %s (14%% HST)",
                     ns_tax)
    else:
        _logger.warning("freight tax: no 14%% HST sale tax found — "
                        "logistics.freight_tax_ns_id left unchanged")

    if buy_tax:
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value) "
            "VALUES ('logistics.freight_tax_buy_interlining_id', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(buy_tax),))
        _logger.info("freight tax: logistics.freight_tax_buy_interlining_id "
                     "-> %s (0%% Int purchase)", buy_tax)
    else:
        _logger.warning("freight tax: no 0%% purchase interlining tax found — "
                        "buy interlining ICP not seeded")

    # Orphaned legacy param — only the removed settings block read it.
    cr.execute(
        "DELETE FROM ir_config_parameter "
        "WHERE key = 'logistics.freight_tax_manual_review_default'")
    _logger.info("freight tax: removed orphaned "
                 "logistics.freight_tax_manual_review_default")

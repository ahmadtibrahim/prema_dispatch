# ─────────────────────────────────────────────────────────────────────
# 18.0.13.64.0 — freight tax mapping: the mapped tax must match the price
# basis the customer agreed (price_tax_mode).
#
# DEFECT: `logistics.freight_tax_ontario_id` was pointed at "13% HST
# Included" (price_include_override = 'tax_included') while every other
# jurisdiction mapping points at a tax-EXCLUDED tax. Freight is sold
# EXCLUSIVE of tax (the agreed price is the untaxed base; tax is added on
# top), so a tax-included tax made Odoo back-solve the base out of the
# agreed amount: an agreed $400 + HST became 353.98 + 46.02 = 400.00 —
# the customer was under-billed the HST and the wrong tax was remitted.
#
# REPAIR: repoint any freight-tax mapping that holds a tax-INCLUDED sale
# tax at its same-rate tax-EXCLUDED twin. Ontario is the only mapping in
# this state; the sweep covers every freight-tax key so a re-misconfigured
# jurisdiction is repaired too.
#
# SCOPE: this touches ONLY the ir_config_parameter freight-tax mappings.
# No account.tax record is edited or deactivated, no booking, invoice or
# journal entry is rewritten, and historical documents are left alone. The
# module also enforces the same rule at selection time
# (logistics.booking._tax_matching_price_mode), so the mapping is
# self-correcting from here on.
#
# Lookups are by amount/use/name — never hardcoded ids. Idempotent.
# ─────────────────────────────────────────────────────────────────────
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Every freight-tax mapping, buy-side included.
    cr.execute(
        "SELECT key, value FROM ir_config_parameter "
        "WHERE key LIKE 'logistics.freight_tax_%_id' "
        "AND value ~ '^[0-9]+$'")
    mappings = cr.fetchall()

    repaired = 0
    for key, value in mappings:
        tax_id = int(value)
        # Is the mapped tax a tax-INCLUDED one?
        cr.execute(
            "SELECT id, name::text, amount, amount_type, type_tax_use, "
            "       company_id "
            "FROM account_tax "
            "WHERE id = %s AND active AND price_include_override = 'tax_included'",
            (tax_id,))
        row = cr.fetchone()
        if not row:
            continue
        _old_id, old_name, amount, amount_type, type_tax_use, company_id = row

        # Same-rate tax-EXCLUDED twin (price_include_override NULL or
        # 'tax_excluded'), same company and use.
        cr.execute(
            "SELECT id, name::text FROM account_tax "
            "WHERE active AND company_id = %s AND amount = %s "
            "  AND amount_type = %s AND type_tax_use = %s "
            "  AND id <> %s "
            "  AND (price_include_override IS NULL "
            "       OR price_include_override <> 'tax_included') "
            "ORDER BY id LIMIT 1",
            (company_id, amount, amount_type, type_tax_use, tax_id))
        twin = cr.fetchone()
        if not twin:
            _logger.warning(
                "freight tax: %s maps to tax-included %s (%s) but no "
                "same-rate excluded twin exists — mapping left unchanged; "
                "bookings on this jurisdiction will be flagged for manual "
                "tax review.", key, old_name, tax_id)
            continue

        new_id, new_name = twin
        cr.execute(
            "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
            (str(new_id), key))
        repaired += 1
        _logger.info(
            "freight tax: %s -> %s %s (was tax-included %s %s)",
            key, new_id, new_name, tax_id, old_name)

    _logger.info(
        "18.0.13.64.0 pre-migration: freight tax mappings checked (%s), "
        "%s repointed to the tax-excluded twin", len(mappings), repaired)

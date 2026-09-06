"""18.0.13.56.0 post-migration — §6/§7 D-B2 additive columns, default backfill.

All new columns are plain additive (no data moves). The only defaults that
need backfilling are the stored enum state for EXISTING rows:

  * logistics.custom.quote.price_tax_mode     → 'exclusive' (the historical
    document basis: rates exclusive of taxes — see the old report footer).
  * logistics.booking.price_tax_mode          → 'exclusive' (same basis).
  * logistics.custom.quote.payment_method_id  stays NULL (no silent
    assignment of a payment method to historical documents).
  * quickpay_*                                NULL/False (disabled by default).

Idempotent: only NULL rows are touched, so re-running is a no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("18.0.13.56.0 post-migration: §6/§7 price-basis backfill")
    # Quote price basis — legacy Rate Confirmations were exclusive-of-tax
    # documents; the footer said so explicitly before §7 made it a field.
    cr.execute(
        "UPDATE logistics_custom_quote SET price_tax_mode = 'exclusive' "
        "WHERE price_tax_mode IS NULL"
    )
    quote_rows = cr.rowcount
    # Booking price basis — same historical basis.
    cr.execute(
        "UPDATE logistics_booking SET price_tax_mode = 'exclusive' "
        "WHERE price_tax_mode IS NULL"
    )
    booking_rows = cr.rowcount
    _logger.info(
        "18.0.13.56.0 post-migration: price-basis backfill done "
        "(quotes: %s rows, bookings: %s rows)", quote_rows, booking_rows)

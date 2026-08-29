# -*- coding: utf-8 -*-
"""Canonical temperature helpers — 18-section work order Sections 3-5.

Rules enforced here (single authority, used by portal intake, dispatcher,
driver app, engine and tests):

- CANONICAL storage is always Celsius. Fahrenheit is a display preference.
- 0°C is a VALID temperature. It must never be treated as falsy/missing.
  In Odoo, an unset Float reads back as False and a stored 0.0 reads back
  as 0.0 — `_temp_supplied` is the ONLY sanctioned "is there a value"
  check and it distinguishes the two.
- Conversions are idempotent within float rounding: F_TO_C(C_TO_F(x)) ≈ x.

NOTE: this module never uses odoo._() — these helpers run outside
request/model frames (tests, services, onchange), where the translation
helper cannot resolve a translation source and logs noisy stack dumps.
Error strings are plain English, consistent with the rest of the engine.
"""


def C_TO_F(c):
    """Celsius → Fahrenheit (exact formula)."""
    return c * 9.0 / 5.0 + 32.0


def F_TO_C(f):
    """Fahrenheit → Celsius (exact formula)."""
    return (f - 32.0) * 5.0 / 9.0


def _temp_supplied(value):
    """True iff `value` carries a real temperature. Works ONLY on RAW
    boundary values (create/write vals, parse_temperature output, form
    onchange) where unset arrives as False/None/'' and a typed 0 arrives
    as 0.0. Never use it on a field read back from the database — Odoo 18
    converts an unset Float to 0.0 on read (Float.convert_to_record), so
    reads are only trusted behind the supplied-flags (see logistics.booking
    temperature_supplied / minimum_temperature_supplied /
    maximum_temperature_supplied)."""
    return value is not False and value is not None and value != ""


def parse_temperature(value, unit="c"):
    """Parse a submitted temperature (str/float/int). '' / None → None
    (not supplied). '0' / 0 / 0.0 → 0.0 (supplied!). Non-numeric raises
    ValueError. `unit` 'f' converts to canonical Celsius."""
    if value is None or value == "":
        return None
    numeric = float(value)
    if unit == "f":
        numeric = F_TO_C(numeric)
    return numeric


def format_temp(c, unit="c", round_tenths=True):
    """One-unit display: '2°C' or '35.6°F'. None/False → '' (an unset
    temperature must render as blank, never as '0°C')."""
    if not _temp_supplied(c):
        return ""
    value = c if unit == "c" else C_TO_F(c)
    if round_tenths:
        text = f"{value:.1f}".rstrip("0").rstrip(".")
    else:
        text = f"{value:g}"
    return f"{text}°{'C' if unit == 'c' else 'F'}"


def format_dual(c, f_first=False):
    """Dual-unit string: '2°C / 35.6°F' (or the F-first mirror). Used in
    portal, driver and dispatcher surfaces. Unset → ''."""
    if not _temp_supplied(c):
        return ""
    c_txt = format_temp(c, "c")
    f_txt = format_temp(c, "f")
    return f"{f_txt} / {c_txt}" if f_first else f"{c_txt} / {f_txt}"


def validate_range(target_c, minimum_c, maximum_c, tolerance_c,
                 target_supplied=None, minimum_supplied=None,
                 maximum_supplied=None):
    """Boundary validation for a booking's temperature requirements.

    Existence comes from the supplied-flags when given (the sanctioned
    checks); raw values are identity-tested otherwise (boundary contexts).
    Returns (errors, effective_range) — effective_range is
    (range_min_c, range_max_c) or None; `errors` is empty when valid.
    0.0 values are respected throughout; tolerance 0.0 == exact target.
    """
    if target_supplied is None:
        target_supplied = _temp_supplied(target_c)
    if minimum_supplied is None:
        minimum_supplied = _temp_supplied(minimum_c)
    if maximum_supplied is None:
        maximum_supplied = _temp_supplied(maximum_c)

    errors = []
    if minimum_supplied and maximum_supplied and minimum_c > maximum_c:
        errors.append(
            "Minimum temperature must not exceed maximum temperature.")
    if target_supplied and minimum_supplied and target_c < minimum_c:
        errors.append("Target temperature is below the minimum.")
    if target_supplied and maximum_supplied and target_c > maximum_c:
        errors.append("Target temperature is above the maximum.")
    if tolerance_c not in (False, None) and tolerance_c < 0:
        errors.append("Tolerance must not be negative.")
    if errors:
        return errors, None
    if target_supplied:
        rmin = minimum_c if minimum_supplied else (
            target_c - tolerance_c
            if tolerance_c not in (False, None) and tolerance_c > 0
            else target_c
        )
        rmax = maximum_c if maximum_supplied else (
            target_c + tolerance_c
            if tolerance_c not in (False, None) and tolerance_c > 0
            else target_c
        )
        return [], (rmin, rmax)
    return [], None


def range_dual(rmin, rmax, f_first=False):
    """Dual-unit range: '1°C / 3°C (33.8°F / 37.4°F)'. Any unset bound
    renders as ''. None range → ''."""
    if rmin is None or rmax is None:
        return ""
    c_txt = f"{format_temp(rmin, 'c')} – {format_temp(rmax, 'c')}"
    f_txt = f"{format_temp(rmin, 'f')} – {format_temp(rmax, 'f')}"
    return f"{f_txt} ({c_txt})" if f_first else f"{c_txt} ({f_txt})"

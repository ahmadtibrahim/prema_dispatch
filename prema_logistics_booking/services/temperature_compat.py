"""Canonical Dry/Reefer compatibility adapter — the ONLY place that maps
legacy temperature values (chilled/frozen, pre-V4) onto the active
Dry/Reefer model. Every channel and every model must call these functions
instead of re-implementing the mapping inline.

Canonical mapping:
    dry     -> dry
    reefer  -> reefer
    chilled -> reefer   (legacy/historical value)
    frozen  -> reefer   (legacy/historical value)

0 degrees Celsius is a valid required_temperature_c value and must never be
treated as falsy/missing.
"""

from odoo.exceptions import ValidationError

DRY = "dry"
REEFER = "reefer"
CANONICAL_MODES = (DRY, REEFER)

_LEGACY_TO_CANONICAL = {
    "dry": DRY,
    "reefer": REEFER,
    "chilled": REEFER,
    "frozen": REEFER,
}


def to_canonical_temperature_mode(value):
    """Map any historical or current temperature-mode value onto dry/reefer.
    Unknown/empty values default to Dry (no temperature control requested)."""
    return _LEGACY_TO_CANONICAL.get((value or "").strip().lower(), DRY)


def vehicle_accepts(vehicle_is_reefer: bool, requested_mode: str) -> bool:
    """Dry truck accepts Dry only. Reefer truck accepts Dry or Reefer."""
    canonical = to_canonical_temperature_mode(requested_mode)
    if canonical == DRY:
        return True
    return bool(vehicle_is_reefer)


def parse_required_temperature_c(raw):
    """Parse a raw form/RPC value into a float or None. '' / None -> None.
    '0' correctly parses to 0.0, which is a valid, non-missing value."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def validate_temperature_request(temperature_mode: str, required_temperature_c):
    """Enforce: Reefer requires an explicit numeric required_temperature_c
    (0.0 is valid and must not be rejected). Dry must carry no required
    temperature. Raises ValidationError on violation."""
    canonical = to_canonical_temperature_mode(temperature_mode)
    if canonical == REEFER:
        if required_temperature_c is None:
            raise ValidationError(
                "A Reefer booking requires a numeric required temperature (°C)."
            )
    return canonical

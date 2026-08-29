# -*- coding: utf-8 -*-
"""Canonical temperature requirement block — 18-section work order §3.

One shared definition for every model that carries a temperature
REQUIREMENT along the booking chain:

    pricing session → recurring agreement / recurring job → weekly-plan
    reservation → custom quote → confirmed booking → dispatch job →
    pallet / item → Driver App → customer tracking → invoice evidence

The confirmed booking (logistics.booking) carries the full canonical set
and the legacy mirror; this mixin gives the OTHER chain models the same
canonical storage so a requirement never degrades to a bare legacy float
(or gets lost in a legacy `required_temperature_c` 0.0 read-back).

Rules (shared with logistics.booking, mirrored here verbatim):
- Celsius is the canonical storage unit; F→C conversion happens once, at
  intake, before any of these models is written (submitted_temperature_unit
  records what the requester typed).
- The supplied-flags (temperature_supplied / minimum_temperature_supplied /
  maximum_temperature_supplied) are the ONLY sanctioned existence checks —
  Odoo 18 reads an unset Float back as 0.0, so a dry record must not be
  confused with a real 0°C requirement.
- target_temperature_c mirrors the legacy required_temperature_c at the
  raw-vals boundary (either write fans out to the other).
- 0.0 is a valid reefer setpoint; dry records carry no temperature at all.
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.temperature_service import (
    _temp_supplied,
    format_dual,
    range_dual,
    validate_range,
)

# Field names this mixin manages; used by the write() guard so unrelated
# writes never touch the flags.
_TEMPERATURE_VAL_FIELDS = (
    "required_temperature_c",
    "target_temperature_c",
    "minimum_temperature_c",
    "maximum_temperature_c",
    "temperature_tolerance_c",
)


class LogisticsTemperatureMixin(models.AbstractModel):
    _name = "logistics.temperature.mixin"
    _description = "Canonical temperature requirement block (§3)"

    required_temperature_c = fields.Float(
        string="Required Temperature (°C, legacy)", readonly=False)
    target_temperature_c = fields.Float(
        string="Target Temperature (°C)",
        help="Canonical Celsius setpoint. 0.0 is a valid 0°C requirement; "
             "dry freight carries no value at all (see temperature_supplied).")
    minimum_temperature_c = fields.Float(
        string="Minimum Temperature (°C)")
    maximum_temperature_c = fields.Float(
        string="Maximum Temperature (°C)")
    temperature_tolerance_c = fields.Float(
        string="Tolerance (°C)",
        help="± tolerance around the target when no explicit min/max is set.")
    temperature_supplied = fields.Boolean(
        string="Temperature Set",
        help="Sanctioned existence check for the target. Only this flag is "
             "identity-tested — Odoo 18 reads an unset Float back as 0.0.")
    minimum_temperature_supplied = fields.Boolean(string="Minimum Set")
    maximum_temperature_supplied = fields.Boolean(string="Maximum Set")
    submitted_temperature_unit = fields.Selection(
        [("c", "°C"), ("f", "°F")], string="Submitted In", default="c",
        help="Unit the requester typed the temperature in. Storage is "
             "canonical Celsius; this only records the intake unit.")
    temperature_requirement_source = fields.Selection(
        [("customer", "Customer"), ("dispatcher", "Dispatcher"),
         ("system", "System"), ("legacy", "Legacy (pre-canonical)")],
        string="Requirement Source", default="customer")
    temperature_display = fields.Char(
        string="Temperature", compute="_compute_temperature_display")
    temperature_range_display = fields.Char(
        string="Range", compute="_compute_temperature_display")

    # ── create/write boundary (same semantics as logistics.booking) ──

    @staticmethod
    def _raw_supplied(raw_value):
        return _temp_supplied(raw_value)

    @staticmethod
    def _sync_temperature_mirror(vals):
        if "target_temperature_c" in vals and "required_temperature_c" not in vals:
            vals["required_temperature_c"] = vals["target_temperature_c"]
        elif "required_temperature_c" in vals:
            vals["target_temperature_c"] = vals["required_temperature_c"]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._sync_temperature_mirror(vals)
            target = vals.get("required_temperature_c")
            if target is None:
                target = vals.get("target_temperature_c", False)
            vals.setdefault("temperature_supplied",
                            self._raw_supplied(target))
            vals.setdefault("minimum_temperature_supplied",
                            self._raw_supplied(vals.get("minimum_temperature_c")))
            vals.setdefault("maximum_temperature_supplied",
                            self._raw_supplied(vals.get("maximum_temperature_c")))
        return super().create(vals_list)

    def write(self, vals):
        if any(k in vals for k in _TEMPERATURE_VAL_FIELDS):
            self._sync_temperature_mirror(vals)
            if "required_temperature_c" in vals or "target_temperature_c" in vals:
                target = vals.get("required_temperature_c", vals.get(
                    "target_temperature_c", False))
                vals["temperature_supplied"] = self._raw_supplied(target)
            if "minimum_temperature_c" in vals:
                vals["minimum_temperature_supplied"] = self._raw_supplied(
                    vals["minimum_temperature_c"])
            if "maximum_temperature_c" in vals:
                vals["maximum_temperature_supplied"] = self._raw_supplied(
                    vals["maximum_temperature_c"])
        return super().write(vals)

    # ── dual-unit display + boundary validation ──

    @api.depends("required_temperature_c", "target_temperature_c",
                 "minimum_temperature_c", "maximum_temperature_c",
                 "temperature_tolerance_c", "temperature_supplied",
                 "minimum_temperature_supplied",
                 "maximum_temperature_supplied")
    def _compute_temperature_display(self):
        for rec in self:
            rec.temperature_display = (
                format_dual(rec.target_temperature_c)
                if rec.temperature_supplied else "")
            _errors, effective = validate_range(
                rec.target_temperature_c, rec.minimum_temperature_c,
                rec.maximum_temperature_c, rec.temperature_tolerance_c,
                target_supplied=rec.temperature_supplied,
                minimum_supplied=rec.minimum_temperature_supplied,
                maximum_supplied=rec.maximum_temperature_supplied)
            rec.temperature_range_display = (
                range_dual(effective[0], effective[1]) if effective else "")

    @api.constrains("required_temperature_c", "target_temperature_c",
                    "minimum_temperature_c", "maximum_temperature_c",
                    "temperature_tolerance_c")
    def _check_temperature_boundaries(self):
        for rec in self:
            errors, _effective = validate_range(
                rec.target_temperature_c, rec.minimum_temperature_c,
                rec.maximum_temperature_c, rec.temperature_tolerance_c,
                target_supplied=rec.temperature_supplied,
                minimum_supplied=rec.minimum_temperature_supplied,
                maximum_supplied=rec.maximum_temperature_supplied)
            if errors:
                raise ValidationError(" ".join(errors))

# -*- coding: utf-8 -*-
"""Driver temperature display preference — 18-section work order §4.

The canonical temperature is ALWAYS stored in Celsius. This field only
chooses which unit the driver app shows FIRST ('2°C / 35.6°F' vs
'35.6°F / 2°C') — it can never change the stored value. Defaults to C.
"""

from odoo import fields, models


class ResUsersTemperaturePreference(models.Model):
    _inherit = "res.users"

    temperature_display_unit = fields.Selection(
        [("c", "Celsius first"), ("f", "Fahrenheit first")],
        string="Temperature Display Preference",
        default="c",
        help="Display-only preference for the Driver App and dispatcher "
             "surfaces. Storage is always canonical Celsius — this never "
             "converts stored values.",
    )

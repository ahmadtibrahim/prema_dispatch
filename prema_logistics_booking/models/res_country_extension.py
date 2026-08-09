"""Extend res.country with logistics network enablement.

This field controls whether a country is available for NEW Prema Logistics
operations. It does NOT affect standard Odoo country behaviour or historical
logistics records.
"""

from odoo import fields, models


class ResCountry(models.Model):
    _inherit = "res.country"

    logistics_network_enabled = fields.Boolean(
        string="Logistics Network Enabled",
        default=True,
        help=(
            "When enabled, this country is available for new Prema Logistics "
            "operations. Disable to exclude the country and all its "
            "provinces/states and regions from new bookings, corridor "
            "configuration, and region matching. Historical records are "
            "unaffected."
        ),
    )

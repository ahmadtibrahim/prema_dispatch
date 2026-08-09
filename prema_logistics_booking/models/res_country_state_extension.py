"""Extend res.country.state with logistics network enablement.

This field controls whether a province/state is available for NEW Prema
Logistics operations. It does NOT affect standard Odoo province/state
behaviour or historical logistics records.
"""

from odoo import fields, models


class ResCountryState(models.Model):
    _inherit = "res.country.state"

    logistics_network_enabled = fields.Boolean(
        string="Logistics Network Enabled",
        default=True,
        help=(
            "When enabled, this province/state is available for new Prema "
            "Logistics operations. Disable to exclude all service regions "
            "under this province/state from new bookings, corridor "
            "configuration, and region matching. Its parent country must "
            "also be enabled. Historical records are unaffected."
        ),
    )

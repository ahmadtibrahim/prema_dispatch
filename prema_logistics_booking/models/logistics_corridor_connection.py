from odoo import fields, models


class LogisticsCorridorConnection(models.Model):
    _inherit = "logistics.corridor"

    connected_corridor_ids = fields.Many2many(
        "logistics.corridor",
        "logistics_corridor_connection_rel",
        "source_corridor_id",
        "target_corridor_id",
        string="May Connect Into",
        domain="[('id', '!=', id), ('active', '=', True)]",
        help=(
            "Explicit next movements allowed after a hub/cross-dock transfer. "
            "Example: Local / Regional may connect into GTA → Quebec Eastbound. "
            "This is operational topology, not merely a pricing preference."
        ),
    )
    connection_notes = fields.Text(
        string="Connection Notes",
        help="Dispatcher notes about approved transfer relationships and cutoff expectations.",
    )

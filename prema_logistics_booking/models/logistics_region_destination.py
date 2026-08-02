"""Region Destination Matrix — what destinations are reachable from each region.

Used by the customer portal to show "Available Destinations" cards
(like airline destinations) when a customer selects their pickup city.
"""
from odoo import fields, models


class LogisticsRegionDestination(models.Model):
    _name = "logistics.region.destination"
    _description = "Region Destination Matrix"
    _order = "origin_region_id, destination_region_id"
    _sql_constraints = [
        ("origin_dest_uniq", "unique(origin_region_id, destination_region_id)",
         "This destination pair already exists."),
    ]

    origin_region_id = fields.Many2one(
        "logistics.region", string="Origin Region", required=True, index=True,
        ondelete="cascade",
    )
    destination_region_id = fields.Many2one(
        "logistics.region", string="Destination Region", required=True, index=True,
        ondelete="cascade",
    )
    transit_region_id = fields.Many2one(
        "logistics.region", string="Via / Transit Region",
        help="If set, shipments go through this transit hub. "
             "E.g., St Catharines → Montreal requires Transit Mississauga."
    )
    active = fields.Boolean(default=True)

    # Display hints for the portal
    pickup_day_label = fields.Char(
        string="Pickup Day (display)", help="e.g. 'Monday', 'Monday or Thursday'"
    )
    delivery_day_label = fields.Char(
        string="Delivery Day (display)", help="e.g. 'Tuesday', 'Wednesday'"
    )
    requires_transit = fields.Boolean(
        compute="_compute_requires_transit", store=True,
        help="True if this connection requires a hub transfer."
    )

    def _compute_requires_transit(self):
        for rec in self:
            rec.requires_transit = bool(rec.transit_region_id)

    def name_get(self):
        result = []
        for r in self:
            origin = r.origin_region_id.name
            dest = r.destination_region_id.name
            via = f" via {r.transit_region_id.name}" if r.transit_region_id else ""
            result.append((r.id, f"{origin} → {dest}{via}"))
        return result

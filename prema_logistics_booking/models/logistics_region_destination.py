"""Region Destination Matrix — ARCHIVED, read-only, derived reference only.

Superseded by RouteResolver/DepartureResolver + logistics.corridor.stop as
the live routing/schedule-day authority. This model duplicated that authority
with plain, manually-maintained fields and no compute tying it back to
lane/corridor data, so it could silently diverge. It has no menu entry and no
live callers — do not populate it manually or wire it into new features; ACL
is read-only for every group (see security/ir.model.access.csv).
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

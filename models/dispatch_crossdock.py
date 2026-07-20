from odoo import fields, models


class PremaDispatchCrossdockLocation(models.Model):
    _name = "prema.dispatch.crossdock.location"
    _description = "Cross-Dock / Hub Location"
    _order = "name asc"

    name = fields.Char(required=True)
    address = fields.Char()
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))
    active = fields.Boolean(default=True)
    notes = fields.Text()


class PremaDispatchCustodyEvent(models.Model):
    """Item chain-of-custody: records every handoff and location scan for a freight item."""
    _name = "prema.dispatch.custody.event"
    _description = "Freight Item Custody Event"
    _order = "occurred_at asc, id asc"

    item_id = fields.Many2one(
        "prema.dispatch.item", string="Freight Item",
        required=True, ondelete="cascade", index=True,
    )
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Dispatch Job",
        related="item_id.job_id", store=True, readonly=True, index=True,
    )
    event_type = fields.Selection([
        ("loaded",      "Loaded onto Truck"),
        ("cross_docked", "Cross-Docked / Stored"),
        ("reloaded",    "Reloaded from Cross-Dock"),
        ("scanned",     "Scanned at Location"),
        ("transferred", "Transferred to Another Truck"),
        ("delivered",   "Delivered"),
        ("exception",   "Exception / Damage"),
    ], required=True)
    occurred_at = fields.Datetime(default=fields.Datetime.now, required=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    location_id = fields.Many2one(
        "prema.dispatch.crossdock.location", string="Location",
        ondelete="set null",
    )
    saved_location_id = fields.Many2one(
        "prema.dispatch.location", string="Saved Location",
        ondelete="set null",
    )
    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Truck", ondelete="set null",
    )
    driver_id = fields.Many2one(
        "res.partner", string="Driver", ondelete="set null",
    )
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", ondelete="set null",
    )
    notes = fields.Char()

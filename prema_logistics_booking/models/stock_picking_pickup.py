# -*- coding: utf-8 -*-
"""Show pickup authorization to staff on the linked native outbound transfer.

Purely additive and read-only: two fields that let the standard Inventory
transfer expose the collections recorded against it. No Inventory behaviour,
field or rule is altered.
"""
from odoo import api, fields, models


class StockPicking(models.Model):
    _inherit = "stock.picking"

    pickup_collection_ids = fields.One2many(
        "logistics.pickup.collection", "picking_id",
        string="Pickup Authorizations")
    pickup_collection_count = fields.Integer(
        compute="_compute_pickup_collection_count")

    @api.depends("pickup_collection_ids")
    def _compute_pickup_collection_count(self):
        for picking in self:
            picking.pickup_collection_count = len(picking.pickup_collection_ids)

    def action_view_pickup_collections(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pickup Authorization",
            "res_model": "logistics.pickup.collection",
            "view_mode": "list,form",
            "domain": [("picking_id", "=", self.id)],
            "context": {"default_picking_id": self.id},
        }

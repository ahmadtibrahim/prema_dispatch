# -*- coding: utf-8 -*-
"""Warehouse pallet positions, seen from `stock.location`.

The configuration itself (is-this-a-position, capacity, priority, blocked,
max weight) lives on `stock.location` as **manual/Studio fields** so the
warehouse maintains it without a developer. This file only adds the read-only
helpers the position board and the allocation engine need.
"""
from odoo import api, fields, models

POSITION_FLAG = "x_pfl_is_position"
POSITION_ORDER = "x_pfl_allocation_order"
POSITION_CAPACITY = "x_pfl_capacity"
POSITION_BLOCKED = "x_pfl_blocked"
POSITION_MAX_WEIGHT = "x_pfl_max_weight"


class StockLocation(models.Model):
    _inherit = "stock.location"

    pfl_reservation_ids = fields.One2many(
        "logistics.position.reservation", "location_id",
        string="Position Reservations")
    pfl_current_reservation_id = fields.Many2one(
        "logistics.position.reservation", string="Current Reservation",
        compute="_compute_pfl_current_reservation")
    pfl_next_reservation_id = fields.Many2one(
        "logistics.position.reservation", string="Next Reservation",
        compute="_compute_pfl_current_reservation")
    pfl_occupant_package_id = fields.Many2one(
        "stock.quant.package", string="Pallet In Place",
        compute="_compute_pfl_occupancy")
    pfl_occupant_order_id = fields.Many2one(
        "sale.order", string="Occupant Booking", compute="_compute_pfl_occupancy")
    pfl_occupant_customer_id = fields.Many2one(
        "res.partner", string="Occupant Customer", compute="_compute_pfl_occupancy")
    pfl_is_occupied = fields.Boolean(
        compute="_compute_pfl_occupancy", search="_search_pfl_is_occupied")

    @api.depends("pfl_reservation_ids.state", "pfl_reservation_ids.date_start",
                 "pfl_reservation_ids.date_end")
    def _compute_pfl_current_reservation(self):
        now = fields.Datetime.now()
        for location in self:
            live = location.pfl_reservation_ids.filtered(
                lambda r: r.state == "reserved")
            location.pfl_current_reservation_id = live.filtered(
                lambda r: r.date_start <= now < r.date_end)[:1]
            location.pfl_next_reservation_id = live.filtered(
                lambda r: r.date_start > now).sorted("date_start")[:1]

    def _search_pfl_is_occupied(self, operator, value):
        """Occupancy is real stock on hand, so it has to be searched in SQL."""
        occupied = (operator == "=" and value) or (operator == "!=" and not value)
        self.env.cr.execute("""
            SELECT DISTINCT location_id FROM stock_quant WHERE quantity > 0
        """)
        ids = [row[0] for row in self.env.cr.fetchall()]
        return [("id", "in" if occupied else "not in", ids)]

    @api.depends("quant_ids.quantity", "quant_ids.package_id")
    def _compute_pfl_occupancy(self):
        Package = self.env["stock.quant.package"]
        has_link = "x_pfl_sale_order_id" in Package._fields
        for location in self:
            quants = location.quant_ids.filtered(lambda q: q.quantity > 0)
            package = quants.mapped("package_id")[:1]
            location.pfl_occupant_package_id = package
            location.pfl_is_occupied = bool(quants)
            if package and has_link:
                location.pfl_occupant_order_id = package.x_pfl_sale_order_id
                location.pfl_occupant_customer_id = package.x_pfl_freight_owner_id
            else:
                location.pfl_occupant_order_id = False
                location.pfl_occupant_customer_id = False

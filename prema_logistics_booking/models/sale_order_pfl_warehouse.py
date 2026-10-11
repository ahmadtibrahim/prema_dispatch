# -*- coding: utf-8 -*-
"""PF Logistics warehouse bookings: capacity, pallet preparation, dock side.

Only orders designated `x_warehouse_service` are touched here. Nothing in this
file runs for a transportation-only order or for another brand's order.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

RECEIPT_TYPE_SEQUENCE = "PFL-IN"
PLACEMENT_TYPE_SEQUENCE = "PFL-INT"
FREIGHT_PRODUCT_CODE = "PFL-CUST-PAL"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    pfl_reservation_ids = fields.One2many(
        "logistics.position.reservation", "sale_order_id",
        string="Position Reservations")
    pfl_reservation_count = fields.Integer(
        compute="_compute_pfl_reservation_count")
    pfl_receipt_ids = fields.One2many(
        "stock.picking", "x_service_booking_id", string="Warehouse Receipts")
    pfl_receipt_id = fields.Many2one(
        "stock.picking", string="Current Receipt", compute="_compute_pfl_receipt")
    pfl_prepared_pallets = fields.Integer(
        string="Pallet Entries Prepared", compute="_compute_pfl_pallet_counts")
    pfl_expected_pallets = fields.Integer(
        string="Expected Pallets", related="x_expected_pallets", readonly=False)
    pfl_actual_pallets = fields.Integer(
        string="Confirmed Received", compute="_compute_pfl_pallet_counts")
    pfl_capacity_state = fields.Selection(
        [("none", "No space needed"), ("reserved", "Space reserved"),
         ("partial", "Space partly reserved"), ("unreserved", "No space reserved"),
         ("released", "Space released")],
        compute="_compute_pfl_capacity_state")

    @api.depends("pfl_reservation_ids.state")
    def _compute_pfl_reservation_count(self):
        for order in self:
            order.pfl_reservation_count = len(
                order.pfl_reservation_ids.filtered(lambda r: r.state == "reserved"))

    @api.depends("pfl_receipt_ids.state")
    def _compute_pfl_receipt(self):
        for order in self:
            live = order.pfl_receipt_ids.filtered(
                lambda p: p.state not in ("done", "cancel"))
            order.pfl_receipt_id = (live or order.pfl_receipt_ids)[:1]

    @api.depends("pfl_receipt_ids.move_line_ids.quantity",
                 "pfl_receipt_ids.move_line_ids.picked",
                 "pfl_receipt_ids.move_line_ids.result_package_id")
    def _compute_pfl_pallet_counts(self):
        for order in self:
            prepared = self.env["stock.quant.package"].browse()
            received = 0.0
            for picking in order.pfl_receipt_ids:
                if picking.state == "cancel":
                    continue
                lines = picking.move_line_ids
                prepared |= lines.mapped("result_package_id")
                received += sum(lines.filtered("picked").mapped("quantity"))
            order.pfl_prepared_pallets = len(prepared)
            order.pfl_actual_pallets = int(received)

    @api.depends("x_warehouse_service", "x_expected_pallets",
                 "pfl_reservation_ids.state")
    def _compute_pfl_capacity_state(self):
        for order in self:
            if not order.x_warehouse_service:
                order.pfl_capacity_state = "none"
                return
            need = order.x_expected_pallets or 0
            if not need:
                order.pfl_capacity_state = "none"
                return
            live = order.pfl_reservation_ids.filtered(lambda r: r.state == "reserved")
            if len(live) >= need:
                order.pfl_capacity_state = "reserved"
            elif live:
                order.pfl_capacity_state = "partial"
            elif order.pfl_reservation_ids:
                order.pfl_capacity_state = "released"
            else:
                order.pfl_capacity_state = "unreserved"

    # ── capacity gate (called by the portal and by staff) ──────────────
    def pfl_check_capacity(self, arrival, departure, count=None, exclude_own=True):
        """Raise a clear UserError when the request cannot be promised space."""
        self.ensure_one()
        Reservation = self.env["logistics.position.reservation"]
        if count is None:
            count = self.x_expected_pallets or 0
        if not count:
            return True
        if not arrival or not departure:
            raise UserError(_(
                "A warehouse appointment (arrival and departure) is required to "
                "reserve space for %s pallets.", count))
        available, blocked = Reservation.pfl_availability(
            *Reservation.pfl_buffered_window(arrival, departure),
            exclude_order=self if exclude_own else None)
        if len(available) < count:
            detail = "; ".join("%s: %s" % (p.display_name, r)
                               for p, r in list(blocked.items())[:8])
            raise UserError(_(
                "The requested time lacks capacity: %(need)s pallets were "
                "requested and only %(got)s position(s) are free for that "
                "window.%(detail)s",
                need=count, got=len(available),
                detail=("\n" + detail) if detail else ""))
        return True

    def action_pfl_reserve_space(self):
        """Reserve (or top up) the booking's positions."""
        self.ensure_one()
        Reservation = self.env["logistics.position.reservation"]
        need = self.x_expected_pallets or 0
        if not need:
            return True
        live = self.pfl_reservation_ids.filtered(lambda r: r.state == "reserved")
        missing = need - len(live)
        if missing <= 0:
            return True
        receipt = self.pfl_receipt_id
        reservations = Reservation.pfl_reserve_positions(
            self, missing, self.x_requested_arrival, self.x_requested_departure,
            picking=receipt or None)
        if receipt:
            receipt.pfl_link_reservations(reservations)
        return True

    def action_pfl_reschedule(self, arrival, departure):
        """Move the booking to a new window.

        The replacement positions are secured FIRST; only then are the old
        reservations released. A failed reschedule therefore leaves the
        original space exactly as it was.
        """
        self.ensure_one()
        Reservation = self.env["logistics.position.reservation"]
        need = self.x_expected_pallets or 0
        previous = self.pfl_reservation_ids.filtered(lambda r: r.state == "reserved")
        if need:
            # Reserve against the new window while the old ones still hold.
            # Reschedule: the old reservations still hold, so the replacement
            # may legitimately reuse the same positions at the new time.
            fresh = Reservation.pfl_reserve_positions(
                self, need, arrival, departure,
                picking=self.pfl_receipt_id or None, exclude_order=True)
            if self.pfl_receipt_id:
                self.pfl_receipt_id.pfl_link_reservations(fresh)
        self.write({"x_requested_arrival": arrival,
                    "x_requested_departure": departure})
        previous.pfl_release(_("Rescheduled to a new appointment window"))
        for reservation in previous:
            if reservation.package_id:
                reservation.package_id.x_pfl_position_id = False
        if self.pfl_receipt_id:
            self.pfl_receipt_id.pfl_link_reservations(
                self.pfl_reservation_ids.filtered(lambda r: r.state == "reserved"))
        return True

    def _action_cancel(self):
        """Cancelling releases the space that is still in the future."""
        res = super()._action_cancel()
        for order in self:
            if not order.x_warehouse_service:
                continue
            future = order.pfl_reservation_ids.filtered(
                lambda r: r.state == "reserved"
                and r.date_end > fields.Datetime.now())
            for reservation in future:
                if reservation.package_id:
                    reservation.package_id.x_pfl_position_id = False
            future.pfl_release(_("Booking cancelled"))
        return res

    # ── one-click pallet preparation ───────────────────────────────────
    def action_pfl_prepare_pallets(self):
        """Everything the dock needs, in one idempotent step.

        Creates or reuses the receipt, prepares one pallet entry per expected
        pallet, gives each its own native package, links them to the booking,
        the freight owner and the reserved position — and stops there. Nothing
        is validated and no stock exists yet: preparing four packages is not a
        statement that four pallets arrived.
        """
        self.ensure_one()
        if not self.x_warehouse_service:
            raise UserError(_("This is not a warehouse service booking."))
        picking = self.pfl_receipt_id or self._pfl_create_receipt()
        if picking.state in ("done", "cancel"):
            raise UserError(_(
                "Receipt %s is already %s — prepare a new release instead.",
                picking.name, picking.state))
        picking.pfl_prepare_pallet_entries()
        self.action_pfl_reserve_space()
        picking.pfl_link_reservations(
            self.pfl_reservation_ids.filtered(lambda r: r.state == "reserved"))
        return self.action_pfl_open_receipt()

    def _pfl_create_receipt(self):
        """The linked PFL receipt, created once and reused thereafter."""
        self.ensure_one()
        Picking = self.env["stock.picking"].sudo()
        ptype = self.env["stock.picking.type"].sudo().search(
            [("sequence_code", "=", RECEIPT_TYPE_SEQUENCE)], limit=1)
        product = self.env["product.product"].sudo().search(
            [("default_code", "=", FREIGHT_PRODUCT_CODE)], limit=1)
        if not ptype or not product:
            raise UserError(_(
                "The PFL receipt operation type or the pallet tracking product "
                "is missing, so a warehouse receipt cannot be created."))
        owner = self.x_freight_owner_id or self.partner_id
        qty = self.x_expected_pallets or 1
        return Picking.create({
            "picking_type_id": ptype.id,
            "partner_id": self.partner_id.id,
            "owner_id": owner.id,
            "x_service_booking_id": self.id,
            "origin": self.name,
            "location_id": ptype.default_location_src_id.id,
            "location_dest_id": ptype.default_location_dest_id.id,
            "move_ids": [(0, 0, {
                "product_id": product.id,
                "product_uom_qty": qty,
                "name": product.display_name,
                "location_id": ptype.default_location_src_id.id,
                "location_dest_id": ptype.default_location_dest_id.id,
            })],
        })

    def action_pfl_open_receipt(self):
        self.ensure_one()
        picking = self.pfl_receipt_id
        if not picking:
            raise UserError(_("There is no warehouse receipt for this booking yet."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "stock.picking",
            "res_id": picking.id,
            "view_mode": "form",
            "target": "current",
        }

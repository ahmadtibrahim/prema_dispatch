# -*- coding: utf-8 -*-
"""The PF Logistics receipt: pallet entries, packages, confirmation, placement.

One button replaces the manual one-pallet-at-a-time routine. Preparing is
idempotent — running it twice leaves one package per pallet, never two — and it
never validates anything: expected, prepared and actually-received quantities
stay separate facts.

Receiving stays the initial physical location. The position a pallet is
*reserved* for is recorded on the pallet itself and shown next to it, and a
native internal transfer does the shelving later — a pallet is never recorded as
already shelved while it is still at the dock.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PLACEMENT_TYPE_SEQUENCE = "PFL-INT"
FREIGHT_PRODUCT_CODE = "PFL-CUST-PAL"


class StockPicking(models.Model):
    _inherit = "stock.picking"

    pfl_prepared_pallet_count = fields.Integer(
        string="Pallet Entries Prepared", compute="_compute_pfl_pallet_counts")
    pfl_received_pallet_count = fields.Integer(
        string="Confirmed Received", compute="_compute_pfl_pallet_counts")
    pfl_expected_pallet_count = fields.Integer(
        string="Expected Pallets", compute="_compute_pfl_pallet_counts")
    pfl_confirmed = fields.Boolean(string="Count Confirmed")
    pfl_confirmed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    pfl_confirmed_at = fields.Datetime(readonly=True, copy=False)
    pfl_shortage_note = fields.Text(
        string="Receiving Notes",
        help="Shortages, damage or pallets held back at the dock.")

    @api.depends("move_ids.product_uom_qty", "move_line_ids.quantity",
                 "move_line_ids.picked", "move_line_ids.result_package_id")
    def _compute_pfl_pallet_counts(self):
        for picking in self:
            lines = picking.move_line_ids.filtered(lambda l: l.state != "cancel")
            picking.pfl_prepared_pallet_count = len(
                lines.mapped("result_package_id"))
            picking.pfl_received_pallet_count = int(
                sum(lines.filtered("picked").mapped("quantity")))
            picking.pfl_expected_pallet_count = int(
                sum(picking.move_ids.filtered(lambda m: m.state != "cancel")
                    .mapped("product_uom_qty")))

    # ── the prepared pallets ───────────────────────────────────────────
    def _pfl_prepared_packages(self):
        self.ensure_one()
        lines = self.move_line_ids.filtered(lambda l: l.state != "cancel")
        return lines.sorted(lambda l: l.id).mapped("result_package_id")

    def pfl_prepare_pallet_entries(self):
        """One pallet entry and one pallet ID per expected pallet.

        Idempotent: if the receipt already holds exactly the right number of
        one-pallet entries with their own pallet IDs, it does nothing. If it is
        in the half-finished state the old manual routine left behind (some
        lines packed, some not), it is rebuilt from the demand — the empty
        packages it made go away with it, so a retry cannot duplicate them.
        """
        self.ensure_one()
        order = self.x_service_booking_id
        if not order:
            raise UserError(_(
                "This transfer is not linked to a service booking, so there is "
                "nothing to prepare."))
        if self.state in ("done", "cancel"):
            raise UserError(_(
                "%s is already %s. Preparation only applies to an open receipt.",
                self.name, self.state))
        expected = order.x_expected_pallets or 0
        if expected <= 0:
            raise UserError(_(
                "Set the expected pallet quantity on booking %s first.", order.name))

        move = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))[:1]
        if not move:
            raise UserError(_("This receipt has no open line to prepare."))

        lines = self.move_line_ids.filtered(lambda l: l.state != "cancel")
        already = (len(lines) == expected
                   and all(abs(l.quantity - 1.0) < 1e-6 for l in lines)
                   and len(lines.mapped("result_package_id")) == expected)
        if already:
            self._pfl_apply_package_links()
            return self._pfl_prepared_packages()

        # Rebuild: detach what the previous attempt left, then make it clean.
        stale = lines.mapped("result_package_id") | self.package_level_ids.mapped("package_id")
        self.package_level_ids.unlink()
        lines.unlink()
        move.write({"product_uom_qty": expected})

        product = move.product_id
        for _index in range(int(expected)):
            self.env["stock.move.line"].sudo().create({
                "move_id": move.id,
                "picking_id": self.id,
                "product_id": product.id,
                "product_uom_id": product.uom_id.id,
                "quantity": 1.0,
                "picked": False,
                "location_id": move.location_id.id,
                "location_dest_id": move.location_dest_id.id,
            })
        self.invalidate_recordset()
        for line_id in list(self.move_line_ids.ids):
            self.invalidate_recordset()
            line = self.env["stock.move.line"].sudo().browse(line_id)
            if line.exists() and not line.result_package_id:
                line.action_put_in_pack()

        # Pallet IDs the rebuild replaced are empty placeholders — drop them so a
        # retry can never leave a trail of orphaned pack numbers.
        MoveLine = self.env["stock.move.line"].sudo()
        for package in stale:
            if not package.exists() or package.quant_ids:
                continue
            if MoveLine.search_count([("result_package_id", "=", package.id)]):
                continue
            package.unlink()

        self.invalidate_recordset()
        self._pfl_apply_package_links()
        self.message_post(body=_(
            "Prepared %s pallet entries with their own pallet IDs. Nothing has "
            "been received yet — confirm the count at the dock.", expected))
        return self._pfl_prepared_packages()

    def _pfl_apply_package_links(self):
        """Put the booking, the freight owner and the customer reference on
        every prepared pallet, so a label or a scan resolves without a hunt."""
        self.ensure_one()
        order = self.x_service_booking_id
        packages = self._pfl_prepared_packages()
        if not order or not packages:
            return packages
        owner = self.owner_id or order.x_freight_owner_id or order.partner_id
        Package = self.env["stock.quant.package"].sudo()
        if "x_pfl_sale_order_id" not in Package._fields:
            return packages
        packages.write({
            "x_pfl_sale_order_id": order.id,
            "x_pfl_freight_owner_id": owner.id,
            "x_pfl_customer_ref": order.client_order_ref or False,
        })
        return packages

    def pfl_link_reservations(self, reservations):
        """Tie each reserved position to one prepared pallet."""
        self.ensure_one()
        reservations = reservations.filtered(lambda r: r.state == "reserved")
        if not reservations:
            return False
        packages = self._pfl_prepared_packages()
        Package = self.env["stock.quant.package"].sudo()
        has_position = "x_pfl_position_id" in Package._fields
        for reservation in reservations.sorted("slot_index"):
            if reservation.package_id:
                continue
            package = (packages - reservations.mapped("package_id"))[:1]
            if not package:
                break
            reservation.package_id = package.id
            if has_position:
                package.x_pfl_position_id = reservation.location_id.id
        return True

    # ── dock: confirm what actually arrived ────────────────────────────
    def action_pfl_confirm_received(self):
        """One click when the count is right; edit a line first when it is not."""
        self.ensure_one()
        if self.state in ("done", "cancel"):
            raise UserError(_("%s is already %s.", self.name, self.state))
        lines = self.move_line_ids.filtered(
            lambda l: l.state != "cancel" and l.quantity > 0)
        if not lines:
            raise UserError(_(
                "Nothing to confirm: every pallet entry is at zero. Use "
                "“Add pallet entry” for freight that arrived in excess, or "
                "leave the receipt unconfirmed if nothing arrived."))
        lines.write({"picked": True})
        self.write({
            "pfl_confirmed": True,
            "pfl_confirmed_by_id": self.env.user.id,
            "pfl_confirmed_at": fields.Datetime.now(),
        })
        self.message_post(body=_(
            "%(count)s pallet(s) confirmed as received by %(user)s.",
            count=self.pfl_received_pallet_count, user=self.env.user.name))
        return True

    def action_pfl_reset_confirmation(self):
        self.ensure_one()
        self.move_line_ids.write({"picked": False})
        self.write({"pfl_confirmed": False,
                    "pfl_confirmed_by_id": False,
                    "pfl_confirmed_at": False})
        return True

    def action_pfl_add_pallet_entry(self):
        """Freight that arrived in excess: one more entry, one more package —
        but only if a position is genuinely free for the booking's window."""
        self.ensure_one()
        order = self.x_service_booking_id
        if not order:
            raise UserError(_("This transfer is not linked to a service booking."))
        Reservation = self.env["logistics.position.reservation"]
        if order.x_requested_arrival and order.x_requested_departure:
            # One MORE position alongside the ones the booking already holds.
            order.pfl_check_capacity(
                order.x_requested_arrival, order.x_requested_departure,
                count=1, exclude_own=False)
        if self.state == "done":
            raise UserError(_(
                "%s is already validated. Freight that turns up after the "
                "receipt is closed needs its own receipt — prepare a new one "
                "from the booking so its space is checked and reserved too.",
                self.name))
        move = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))[:1]
        if not move:
            raise UserError(_("This receipt has no open line."))
        line = self.env["stock.move.line"].sudo().create({
            "move_id": move.id,
            "picking_id": self.id,
            "product_id": move.product_id.id,
            "product_uom_id": move.product_id.uom_id.id,
            "quantity": 1.0,
            "picked": False,
            "location_id": move.location_id.id,
            "location_dest_id": move.location_dest_id.id,
        })
        move.write({"product_uom_qty": (move.product_uom_qty or 0) + 1})
        line.action_put_in_pack()
        self.invalidate_recordset()
        self._pfl_apply_package_links()
        if order.x_requested_arrival and order.x_requested_departure:
            extra = Reservation.pfl_reserve_positions(
                order, 1, order.x_requested_arrival, order.x_requested_departure,
                picking=self)
            self.pfl_link_reservations(extra)
        self.message_post(body=_(
            "Excess pallet entry added at the dock and space reserved for it."))
        return True

    # ── placement: shelving is a separate, later movement ──────────────
    def action_pfl_create_placement_transfer(self):
        """A native internal transfer that shelves the received pallets.

        Created after the receipt is validated, one move per pallet so each
        pallet keeps its own reserved destination. It is deliberately left
        unvalidated: scanning the position at the rack is what completes it.
        """
        self.ensure_one()
        if self.state != "done":
            raise UserError(_(
                "Validate the receipt first — pallets cannot be shelved while "
                "they are still on the dock."))
        packages = self._pfl_prepared_packages().filtered("x_pfl_position_id")
        if not packages:
            raise UserError(_(
                "None of these pallets has a reserved position to be moved to."))
        existing = self.env["stock.picking"].sudo().search([
            ("x_service_booking_id", "=", self.x_service_booking_id.id),
            ("picking_type_id.sequence_code", "=", PLACEMENT_TYPE_SEQUENCE),
            ("state", "not in", ("done", "cancel")),
        ], limit=1)
        if existing:
            return self._pfl_open_picking(existing)
        ptype = self.env["stock.picking.type"].sudo().search(
            [("sequence_code", "=", PLACEMENT_TYPE_SEQUENCE)], limit=1)
        if not ptype:
            raise UserError(_("The PFL placement operation type is missing."))
        src = ptype.default_location_src_id or self.location_dest_id
        moves = [(0, 0, {
            "product_id": package.quant_ids.product_id[:1].id or
                          self.move_ids.product_id[:1].id,
            "product_uom_qty": 1,
            "name": package.name,
            "location_id": src.id,
            "location_dest_id": package.x_pfl_position_id.id,
        }) for package in packages]
        placement = self.env["stock.picking"].sudo().create({
            "picking_type_id": ptype.id,
            "partner_id": self.partner_id.id,
            "owner_id": self.owner_id.id,
            "x_service_booking_id": self.x_service_booking_id.id,
            "origin": self.name,
            "location_id": src.id,
            "location_dest_id": src.id,
            "move_ids": moves,
        })
        placement.action_confirm()
        placement.action_assign()
        # one move line per move, carrying the pallet that goes there
        for move, package in zip(placement.move_ids, packages):
            line = move.move_line_ids[:1]
            if line:
                line.write({"package_id": package.id,
                            "quantity": 1.0,
                            "location_dest_id": package.x_pfl_position_id.id})
        placement.message_post(body=_(
            "Placement transfer created for %s reserved position(s). Scan each "
            "position at the rack to confirm it went to the right place.",
            len(packages)))
        return self._pfl_open_picking(placement)

    def action_pfl_print_labels(self):
        """Print one label per prepared pallet (or the off-pallet cards)."""
        self.ensure_one()
        packages = self._pfl_prepared_packages()
        if not packages:
            raise UserError(_(
                "No pallet IDs to label yet — run “Prepare Pallets” first."))
        report = self.env.ref(
            "prema_logistics_booking.action_report_pfl_pallet_label")
        return report.report_action(packages)

    def action_pfl_print_cards(self):
        """The off-pallet card for customers who do not allow attached labels.

        Same package id, same exclusive position — nothing about the pallet
        changes, only where the paper lives.
        """
        self.ensure_one()
        packages = self._pfl_prepared_packages()
        if not packages:
            raise UserError(_("No pallet IDs to label yet."))
        report = self.env.ref(
            "prema_logistics_booking.action_report_pfl_pallet_card")
        return report.report_action(packages)

    def _pfl_open_picking(self, picking):
        return {
            "type": "ir.actions.act_window",
            "res_model": "stock.picking",
            "res_id": picking.id,
            "view_mode": "form",
            "target": "current",
        }

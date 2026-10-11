# -*- coding: utf-8 -*-
"""Scan a pallet label, or scan the rack position to confirm placement."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

RESULT_SELECTION = [
    ("pending", "Scan a pallet label"),
    ("found", "Pallet found"),
    ("unknown", "No pallet with that code"),
    ("placed_ok", "Placed in the reserved position"),
    ("placed_wrong", "Moved to the wrong position"),
]


class PflPalletScan(models.TransientModel):
    _name = "pfl.pallet.scan"
    _description = "PF Logistics Pallet Scan"

    code = fields.Char(string="Pallet ID", required=True,
                       help="Scan or type the pallet barcode.")
    picking_id = fields.Many2one(
        "stock.picking", string="Transfer", readonly=True,
        help="Set when the scanner was opened from a placement transfer, so a "
             "pallet that is not on that transfer is refused.")
    result = fields.Selection(RESULT_SELECTION, default="pending")
    message = fields.Text(readonly=True)

    package_id = fields.Many2one("stock.quant.package", string="Pallet ID",
                                 readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer", readonly=True)
    order_id = fields.Many2one("sale.order", string="Service Booking", readonly=True)
    customer_ref = fields.Char(string="Customer Reference", readonly=True)
    current_location_id = fields.Many2one("stock.location", string="Current Location",
                                          readonly=True)
    reserved_position_id = fields.Many2one("stock.location", string="Reserved Position",
                                           readonly=True)
    reservation_end = fields.Datetime(readonly=True)

    scanned_position_id = fields.Many2one(
        "stock.location", string="Scanned Position",
        help="Scan the position label at the rack to confirm where the pallet "
             "was actually put.")

    def _reset_result(self):
        self.write({
            "result": "pending", "message": False, "package_id": False,
            "partner_id": False, "order_id": False, "customer_ref": False,
            "current_location_id": False, "reserved_position_id": False,
            "reservation_end": False,
        })

    def action_lookup(self):
        self.ensure_one()
        code = (self.code or "").strip()
        self._reset_result()
        if not code:
            raise UserError(_("Scan or type a pallet label first."))
        package = self.env["stock.quant.package"].sudo().search(
            [("name", "=", code)], limit=1)
        if not package:
            package = self.env["stock.quant.package"].sudo().search(
                [("name", "ilike", code)], limit=1)
        if not package:
            self.write({"result": "unknown",
                        "message": _("No pallet is registered under “%s”.", code)})
            return self._reopen()
        if self.picking_id and package not in self.picking_id.move_line_ids.mapped(
                "package_id") | self.picking_id.move_line_ids.mapped("result_package_id"):
            self.write({
                "result": "unknown",
                "message": _(
                    "%(pkg)s is not on transfer %(transfer)s. Scan a pallet that "
                    "belongs to this transfer.",
                    pkg=package.name, transfer=self.picking_id.name),
            })
            return self._reopen()
        payload = package.pfl_scan_payload()
        self.write({
            "result": "found",
            "package_id": package.id,
            "partner_id": payload["customer"].id if payload["customer"] else False,
            "order_id": payload["booking_id"] or False,
            "customer_ref": payload["customer_reference"],
            "current_location_id": package.location_id.id or False,
            "reserved_position_id": payload["reserved_position_id"] or False,
            "reservation_end": (
                self.env["logistics.position.reservation"].sudo().search(
                    [("package_id", "=", package.id), ("state", "=", "reserved")],
                    limit=1).date_end or False),
            "message": _(
                "%(pkg)s belongs to %(customer)s on booking %(booking)s. It is at "
                "%(current)s and is reserved for %(position)s.",
                pkg=payload["package"],
                customer=payload["customer_name"] or _("(no owner recorded)"),
                booking=payload["booking"] or _("(no booking)"),
                current=payload["current_location"] or _("(not in a location)"),
                position=payload["reserved_position"] or _("(no position reserved)")),
        })
        return self._reopen()

    def action_confirm_placement(self):
        """Scanning the position at the rack is what confirms where it went."""
        self.ensure_one()
        if not self.package_id or not self.scanned_position_id:
            raise UserError(_("Scan a pallet label, then the position it was put in."))
        self.reserved_position_id = (
            self.env["logistics.position.reservation"].sudo().search(
                [("package_id", "=", self.package_id.id),
                 ("state", "=", "reserved")], limit=1).location_id.id or False)
        if not self.reserved_position_id:
            self.write({"result": "placed_wrong",
                        "message": _("This pallet has no reserved position, so the "
                                     "placement cannot be confirmed.")})
            return self._reopen()
        if self.scanned_position_id != self.reserved_position_id:
            self.write({
                "result": "placed_wrong",
                "message": _(
                    "WRONG POSITION. %(pkg)s is reserved for %(want)s but was "
                    "scanned at %(got)s. Move it, or correct the reservation, "
                    "before validating the placement transfer.",
                    pkg=self.package_id.name,
                    want=self.reserved_position_id.complete_name,
                    got=self.scanned_position_id.complete_name),
            })
            self.package_id.pfl_log_placement(self.message)
            return self._reopen()
        self.package_id.pfl_log_placement(_(
            "Placement confirmed at %s by %s.",
            self.scanned_position_id.complete_name, self.env.user.name))
        self.write({
            "result": "placed_ok",
            "message": _("%(pkg)s confirmed in %(pos)s.",
                         pkg=self.package_id.name,
                         pos=self.scanned_position_id.complete_name),
        })
        return self._reopen()

    def _reopen(self):
        """Keep the same wizard open so a scan session flows."""
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

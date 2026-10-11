# -*- coding: utf-8 -*-
"""Pallets as native packages, with the booking facts a label and a scan need.

A PF Logistics pallet IS a native `stock.quant.package` — it keeps the native
sequence, the native label report and the native move machinery. This adds only
the links back to the commercial record (booking, freight owner, customer
reference) and the reserved storage position, so a label can be printed and a
barcode can be resolved without hunting through pickings.

The link fields are **manual/Studio fields** (`x_pfl_*`) on
`stock.quant.package`; the helpers below degrade quietly if they are absent.
"""
import base64

from odoo import _, api, fields, models

LINK_FIELDS = ("x_pfl_sale_order_id", "x_pfl_freight_owner_id",
               "x_pfl_position_id", "x_pfl_customer_ref")


class StockQuantPackage(models.Model):
    _inherit = "stock.quant.package"

    def _pfl_has_links(self):
        return all(name in self._fields for name in LINK_FIELDS)

    # ── what a scan resolves to ────────────────────────────────────────
    def pfl_scan_payload(self):
        """Everything staff need when they scan a pallet label."""
        self.ensure_one()
        picking = self.env["stock.move.line"].sudo().search(
            [("result_package_id", "=", self.id)], limit=1).picking_id
        order = self.x_pfl_sale_order_id if self._pfl_has_links() else False
        position = self.x_pfl_position_id if self._pfl_has_links() else False
        owner = self.x_pfl_freight_owner_id if self._pfl_has_links() else False
        reservation = self.env["logistics.position.reservation"].sudo().search(
            [("package_id", "=", self.id), ("state", "=", "reserved")], limit=1)
        if not position and reservation:
            position = reservation.location_id
        return {
            "package": self.name,
            "package_id": self.id,
            "customer": (owner or (order.partner_id.commercial_partner_id if order else False)),
            "customer_name": (owner or (order.partner_id.commercial_partner_id if order else False)).name or "",
            "booking": order.name if order else "",
            "booking_id": order.id if order else False,
            "customer_reference": (self.x_pfl_customer_ref if self._pfl_has_links() else False)
                                  or (order.client_order_ref if order else "") or "",
            "current_location": self.location_id.complete_name or "",
            "reserved_position": position.complete_name or "",
            "reserved_position_id": position.id if position else False,
            "receipt": picking.name if picking else "",
            "reservation_end": (reservation.date_end.strftime("%Y-%m-%d %H:%M")
                                if reservation else ""),
        }

    def pfl_log_placement(self, message):
        """Record a placement note where it can actually live.

        A package carries no chatter of its own, so the note goes on the
        pallet's reservation — the record that owns the reserved position — and
        falls back to the transfer it sits on.
        """
        self.ensure_one()
        target = self.env["logistics.position.reservation"].sudo().search(
            [("package_id", "=", self.id)], order="id desc", limit=1)
        if not target:
            target = self.env["stock.picking"].sudo().search(
                ["|", ("move_line_ids.package_id", "=", self.id),
                 ("move_line_ids.result_package_id", "=", self.id)], limit=1)
        if target:
            target.message_post(body=message)
        return bool(target)

    def action_pfl_open_pallet(self):
        """Open the pallet form from anywhere it is shown."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "stock.quant.package",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    # ── label support ──────────────────────────────────────────────────
    def pfl_label_barcode_data_uri(self, symbology="Code128"):
        """A `data:` URI barcode, so the label never depends on an HTTP round
        trip (and so it still renders under `--no-http`)."""
        self.ensure_one()
        try:
            from reportlab.graphics.barcode import createBarcodeDrawing
            drawing = createBarcodeDrawing(
                symbology, value=self.name, format="png", width=600, height=120)
            png = drawing.asString("png")
        except Exception:                                     # pragma: no cover
            return False
        return "data:image/png;base64,%s" % base64.b64encode(png).decode()

    def pfl_label_lines(self):
        payload = self.pfl_scan_payload()
        return [
            (_("Pallet ID"), payload["package"]),
            (_("Customer / Freight Owner"), payload["customer_name"]),
            (_("Booking"), payload["booking"]),
            (_("Customer Reference"), payload["customer_reference"]),
            (_("Reserved Position"), payload["reserved_position"] or _("not assigned")),
            (_("Current Location"), payload["current_location"] or _("—")),
        ]

# -*- coding: utf-8 -*-
"""What the Barcode app needs to know about a PF Logistics receipt.

`stock_barcode` documents `_get_fields_stock_barcode()` as the place to inject
extra fields into the client action, so that is what this extends — no separate
app, no parallel record, and the native Barcode screens do the work.

The summary deliberately carries **no prices**: it is built by reading only the
operational attributes of the booking (which services were asked for and how
many), and that privileged read happens only on the picking the operator is
actually working with.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError


def _as_id(value):
    """`read(load=False)` gives a bare id; be tolerant of both shapes."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else 0
    return value or 0

_logger = logging.getLogger(__name__)

#: Warehouse instructions worth showing at the dock. Quantities only — never a
#: price, a rate or a total.
INSTRUCTION_CODES = ("PFL-XD-LTL", "PFL-XD-FTL", "PFL-WRAP", "PFL-PAL-SUP",
                     "PFL-LAB-HR", "PFL-STOR-DAY", "PFL-LABEL")


def _placement_reservation(picking, package):
    """The position reserved for a pallet on this booking, or an empty set."""
    return picking.x_service_booking_id.pfl_reservation_ids.filtered(
        lambda r: r.state == "reserved" and r.package_id == package).location_id


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    def write(self, vals):
        """A pallet on a placement transfer may only go where it is reserved.

        This bites whichever way the destination is set — scanning a location
        in Barcode, typing it, or an RPC call — so a wrong position cannot be
        accepted quietly. The module's own binder runs elevated and is exempt.
        """
        if not self.env.su and "location_dest_id" in vals:
            for line in self:
                picking = line.picking_id
                if picking.picking_type_id.sequence_code != "PFL-INT" \
                        or not picking.x_service_booking_id:
                    continue
                package = line.package_id or line.result_package_id
                if not package:
                    continue
                reserved = _placement_reservation(picking, package)
                if reserved and reserved.id != vals["location_dest_id"]:
                    raise UserError(_(
                        "Pallet %(pkg)s is reserved for %(want)s, not "
                        "%(got)s. Scan the position shown on its label.",
                        pkg=package.name, want=reserved.complete_name,
                        got=self.env["stock.location"].browse(
                            vals["location_dest_id"]).complete_name or "—"))
        return super().write(vals)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    pfl_arrived_at = fields.Datetime(string="Truck Arrived", copy=False)
    pfl_arrived_by_id = fields.Many2one("res.users", copy=False, readonly=True)
    pfl_is_warehouse_receipt = fields.Boolean(
        string="PF Logistics Receipt", compute="_compute_pfl_barcode_flags")
    pfl_operational_summary = fields.Text(
        string="Dock Instructions", compute="_compute_pfl_operational_summary")
    pfl_is_placement_transfer = fields.Boolean(
        string="PF Logistics Placement", compute="_compute_pfl_barcode_flags")
    pfl_is_release = fields.Boolean(
        string="PF Logistics Release", compute="_compute_pfl_barcode_flags")
    pfl_collection_state = fields.Selection(
        [("authorized", "Authorized"), ("collected", "Collected"),
         ("cancelled", "Cancelled")],
        string="Collection", compute="_compute_pfl_barcode_flags")

    @api.depends("x_service_booking_id", "picking_type_id",
                 "pickup_collection_ids.state")
    def _compute_pfl_barcode_flags(self):
        for picking in self:
            code = picking.picking_type_id.sequence_code
            linked = bool(picking.x_service_booking_id)
            picking.pfl_is_warehouse_receipt = bool(linked and code == "PFL-IN")
            picking.pfl_is_placement_transfer = bool(linked and code == "PFL-INT")
            picking.pfl_is_release = bool(linked and code == "PFL-OUT")
            collection = self.env["logistics.pickup.collection"].search(
                [("picking_id", "=", picking.id)], order="id desc", limit=1)
            picking.pfl_collection_state = collection.state if collection else False

    @api.depends("x_service_booking_id", "move_line_ids.result_package_id",
                 "x_service_booking_id.x_requested_arrival")
    def _compute_pfl_operational_summary(self):
        for picking in self:
            picking.pfl_operational_summary = picking._pfl_dock_summary()

    #: The ONLY booking fields the dock may see. No price, rate, tax or total
    #: can reach the summary because none of them is in this list.
    DOCK_ORDER_FIELDS = (
        "name", "client_order_ref", "x_requested_arrival", "x_expected_pallets",
        "partner_id", "x_outbound_recipient_id",
    )

    def _pfl_dock_summary(self):
        """Everything the dock needs, and nothing commercial.

        The booking is read elevated because §4 denies this role the quotation — but
        only after the scope is checked (this must be a PF Logistics receipt) and
        only through the explicit field whitelist above, so the elevation cannot
        carry a price out with it.
        """
        self.ensure_one()
        if not self.pfl_is_warehouse_receipt:
            return ""
        booking = self.x_service_booking_id.sudo()
        data = booking.read(list(self.DOCK_ORDER_FIELDS), load=False)[0]
        lines = [_("Booking: %s", data["name"])]
        if data["client_order_ref"]:
            lines.append(_("Customer reference: %s", data["client_order_ref"]))
        owner = self.owner_id or self.env["res.partner"].browse(
            _as_id(data["partner_id"]))
        lines.append(_("Freight owner: %s", owner.display_name or "—"))
        if data["x_requested_arrival"]:
            lines.append(_("Expected arrival: %s (America/Toronto)",
                           data["x_requested_arrival"].strftime("%Y-%m-%d %H:%M")))
        lines.append(_("Expected pallets: %s", data["x_expected_pallets"] or 0))
        packages = self._pfl_prepared_packages()
        if packages:
            lines.append(_("Prepared pallets:"))
            for package in packages:
                position = package.x_pfl_position_id.complete_name or _("no position")
                lines.append("   %s → %s" % (package.name, position))
        else:
            lines.append(_("No pallets prepared yet — use “Prepare Pallets”."))
        # services asked for, quantities only
        wanted = []
        for line in booking.order_line.read(["product_id", "product_uom_qty"],
                                            load=False):
            product = self.env["product.product"].browse(_as_id(line["product_id"]))
            if product.default_code in INSTRUCTION_CODES and line["product_uom_qty"]:
                wanted.append("%s × %s" % (product.default_code,
                                           line["product_uom_qty"]))
        if wanted:
            lines.append(_("Requested services: %s", ", ".join(wanted)))
        if data["x_outbound_recipient_id"]:
            recipient = self.env["res.partner"].browse(
                _as_id(data["x_outbound_recipient_id"]))
            lines.append(_("Delivery recipient: %s", recipient.display_name))
        collection = self.env["logistics.pickup.collection"].search(
            [("sale_order_id", "=", booking.id)], order="id desc", limit=1)
        if collection and collection.customer_instructions:
            lines.append(_("Handling: %s", collection.customer_instructions))
        return "\n".join(lines)

    def _get_fields_stock_barcode(self):
        """The documented hook for adding fields to the Barcode client action."""
        return super()._get_fields_stock_barcode() + [
            "x_service_booking_id",
            "pfl_is_warehouse_receipt",
            "pfl_operational_summary",
            "pfl_prepared_pallet_count",
            "pfl_received_pallet_count",
            "pfl_confirmed",
            "pfl_arrived_at",
            "pfl_shortage_note",
            "pfl_is_placement_transfer",
            "pfl_is_release",
            "pfl_collection_state",
        ]

    # ── the dock actions, reachable from Barcode ───────────────────────
    def action_pfl_mark_arrived(self):
        """Record that the truck is at the dock.

        It opens the receiving: nothing is declared received and no pallet is
        marked placed by this — the employee still confirms both at the dock.
        """
        for picking in self:
            if picking.state in ("done", "cancel"):
                raise UserError(_(
                    "%s is already %s — there is nothing to receive.",
                    picking.name, picking.state))
            if picking.pfl_arrived_at:
                continue
            picking.write({"pfl_arrived_at": fields.Datetime.now(),
                           "pfl_arrived_by_id": self.env.user.id})
            picking.message_post(body=_(
                "Truck arrived; receiving opened by %s.", self.env.user.name))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success",
                       "message": _("Arrival recorded. Receive the pallets and "
                                    "confirm the count when the truck is done."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    # ── the dock steps, each opened from Barcode ───────────────────────
    def _pfl_dialog(self, model, name):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
            "context": {"default_picking_id": self.id, "active_id": self.id},
        }

    def action_pfl_create_placement(self):
        """Create the placement transfer from the dock and open it in Barcode,
        so the operator never has to go back to a Sales or Inventory form."""
        self.ensure_one()
        action = self.action_pfl_create_placement_transfer()
        return {
            "type": "ir.actions.client",
            "tag": "stock_barcode_client_action",
            "name": _("Placement"),
            "params": {"actionId": False},
            "context": {"active_id": action["res_id"], "active_model": "stock.picking"},
        }

    def action_pfl_open_receive(self):
        """Confirm what actually arrived — the manual/exception counterpart of
        scanning, in the same native dialog."""
        self.ensure_one()
        return self._pfl_dialog("pfl.dock.receive", _("Receive pallets"))

    def action_pfl_open_pickup(self):
        """Record the driver, the company, how authority was verified, and the
        signature — on the release, at the dock."""
        self.ensure_one()
        if not self.env["logistics.pickup.collection"].search(
                [("picking_id", "=", self.id)], limit=1):
            raise UserError(_(
                "%s has no pickup authorization record yet.", self.name))
        return self._pfl_dialog("pfl.dock.pickup", _("Record the collecting driver"))

    def action_pfl_scan_placement(self):
        """Scan a pallet, then the position at the rack; a wrong position is
        refused."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Confirm placement"),
            "res_model": "pfl.pallet.scan",
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
            "context": {"default_picking_id": self.id},
        }

    def action_pfl_confirm_placement(self):
        """Complete the placement: every pallet must be going to the position
        reserved for it, then the native transfer is validated."""
        self.ensure_one()
        if self.picking_type_id.sequence_code != "PFL-INT":
            raise UserError(_("%s is not a placement transfer.", self.name))
        if not all(line.location_dest_id for line in self.move_line_ids):
            raise UserError(_("Every pallet needs a destination before the "
                              "placement can be confirmed."))
        self.pfl_bind_pallets_to_destinations()
        result = self.with_context(skip_sms=True).button_validate()
        if isinstance(result, dict) and result.get("res_model"):
            return result
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success",
                       "message": _("Placement confirmed for %s pallet(s).",
                                    self.pfl_prepared_pallet_count),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def action_pfl_complete_collection(self):
        """Close the collection — only possible once the release is validated."""
        self.ensure_one()
        collection = self.env["logistics.pickup.collection"].search(
            [("picking_id", "=", self.id)], order="id desc", limit=1)
        if not collection:
            raise UserError(_("%s has no collection recorded against it.",
                              self.name))
        collection.action_record_collection()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success",
                       "message": _("Collection completed for %s.",
                                    collection.name),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def action_pfl_proof_of_pickup(self):
        """The customer-facing Proof of Pickup, from the dock."""
        self.ensure_one()
        collection = self.env["logistics.pickup.collection"].search(
            [("picking_id", "=", self.id), ("state", "=", "collected")],
            order="id desc", limit=1)
        if not collection:
            raise UserError(_(
                "There is no completed collection on %s yet, so there is no "
                "Proof of Pickup to show.", self.name))
        return self.env.ref(
            "prema_logistics_booking.action_report_pickup_proof").report_action(
                collection)

    def action_pfl_prepare_from_barcode(self):
        """Prepare the pallet entries and labels without leaving Barcode."""
        self.ensure_one()
        if not self.pfl_is_warehouse_receipt:
            raise UserError(_("%s is not a PF Logistics receiving operation.",
                              self.name))
        self.pfl_prepare_pallet_entries()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success",
                       "message": _("%s pallet(s) prepared.",
                                    self.pfl_prepared_pallet_count),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

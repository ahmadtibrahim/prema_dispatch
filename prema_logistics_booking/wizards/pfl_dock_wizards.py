# -*- coding: utf-8 -*-
"""The dock's exception and collection screens, opened from Barcode.

These are ordinary transient models shown in the standard dialog, so the
operator stays in Barcode and never opens an Inventory or Sales form. Every one
of them reuses the existing records and methods — the receipt, the placement
transfer, the collection — and none of them can create stock: receiving is
confirmed against the prepared pallets, and placing is the validation of the
native internal transfer.

Arrival, receipt, placement and collection are four separate facts. Printing a
label or pressing Truck Arrived writes none of them.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

RECEIVE_STATES = [
    ("received", "Received"),
    ("short", "Short — did not arrive"),
    ("damaged", "Damaged / held"),
]

VERIFICATION_METHODS = [
    ("photo_id", "Government photo ID checked at the dock"),
    ("carrier_dispatched", "Collecting company confirmed the driver"),
    ("named_on_authorization", "The customer named this individual"),
    ("manager_override", "Manager override — reason recorded"),
]


class PflDockReceiveLine(models.TransientModel):
    _name = "pfl.dock.receive.line"
    _description = "Pallet on the receiving dock"

    wizard_id = fields.Many2one("pfl.dock.receive", ondelete="cascade")
    package_id = fields.Many2one("stock.quant.package", string="Pallet ID",
                                 readonly=True)
    position_name = fields.Char(string="Reserved Position", readonly=True)
    state = fields.Selection(RECEIVE_STATES, default="received", required=True)
    note = fields.Char(string="Note")


class PflDockReceive(models.TransientModel):
    _name = "pfl.dock.receive"
    _description = "Confirm the pallets actually received"

    picking_id = fields.Many2one("stock.picking", required=True)
    line_ids = fields.One2many("pfl.dock.receive.line", "wizard_id",
                               string="Pallets")
    damage_note = fields.Text(string="Damage / shortage note")
    photo = fields.Binary(string="Receiving photo")
    photo_name = fields.Char()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        picking = self.env["stock.picking"].browse(
            self.env.context.get("active_id") or self.env.context.get("default_picking_id"))
        if picking and picking.pfl_is_warehouse_receipt:
            values["picking_id"] = picking.id
            lines = []
            for line in picking.move_line_ids.filtered(
                    lambda l: l.state != "cancel" and l.result_package_id):
                package = line.result_package_id
                lines.append((0, 0, {
                    "package_id": package.id,
                    "position_name": package.x_pfl_position_id.complete_name or "",
                    # what the dock already sees: the quantity Barcode recorded
                    "state": "received" if line.quantity else "short",
                }))
            values["line_ids"] = lines
            values["damage_note"] = picking.pfl_shortage_note or ""
        return values

    def action_apply(self):
        """Record the real count, per pallet. Nothing here creates stock —
        the receipt is still the thing that does, when it is validated."""
        self.ensure_one()
        picking = self.picking_id
        if not picking.pfl_is_warehouse_receipt:
            raise UserError(_("%s is not a PF Logistics receiving operation.",
                              picking.name))
        if picking.state in ("done", "cancel"):
            raise UserError(_("%s is already %s.", picking.name, picking.state))
        short, damaged = [], []
        for entry in self.line_ids:
            line = picking.move_line_ids.filtered(
                lambda l: l.result_package_id == entry.package_id)
            if not line:
                raise UserError(_(
                    "Pallet %s is not prepared on %s. Prepare the pallets "
                    "before receiving.", entry.package_id.name, picking.name))
            if entry.state == "received":
                line.write({"quantity": 1.0, "picked": True})
            else:
                line.write({"quantity": 0.0, "picked": False})
                (short if entry.state == "short" else damaged).append(
                    entry.package_id.name)
        notes = [n for n in (self.damage_note,) if n]
        if short:
            notes.append(_("Short: %s", ", ".join(short)))
        if damaged:
            notes.append(_("Damaged/held: %s", ", ".join(damaged)))
        picking.write({"pfl_shortage_note": "\n".join(notes) or False})
        if self.photo:
            self.env["ir.attachment"].create({
                "name": self.photo_name or "receiving-photo.jpg",
                "datas": self.photo,
                "res_model": "stock.picking",
                "res_id": picking.id,
            })
        picking.message_post(body=_(
            "Receiving recorded: %(received)s received, %(short)s short, "
            "%(damaged)s damaged.",
            received=len(self.line_ids) - len(short) - len(damaged),
            short=len(short), damaged=len(damaged)))
        picking.write({"pfl_confirmed": True,
                       "pfl_confirmed_by_id": self.env.user.id,
                       "pfl_confirmed_at": fields.Datetime.now()})
        return {"type": "ir.actions.act_window_close"}


class PflDockPickup(models.TransientModel):
    _name = "pfl.dock.pickup"
    _description = "Record who actually collected the pallets"

    picking_id = fields.Many2one("stock.picking", required=True)
    collection_id = fields.Many2one("logistics.pickup.collection", required=True)
    authorized_party = fields.Char(readonly=True,
                                   related="collection_id.authorized_contact_name")
    customer_instructions = fields.Text(readonly=True,
                                        related="collection_id.customer_instructions")
    collected_by_name = fields.Char(string="Actual Driver Name", required=True)
    collected_company = fields.Char(
        string="Collecting Company", required=True,
        help="The company the driver actually collected for. Anything other "
             "than the authorized party needs a manager override.")
    # required, so it has a default: the column is NOT NULL and a False here
    # would fail at insert rather than at the dock
    verification_method = fields.Selection(VERIFICATION_METHODS, required=True,
                                           default="photo_id")
    verification_result = fields.Selection(
        [("match", "Collecting company is the authorized party"),
         ("mismatch", "Different company came to collect"),
         ("no_authorization", "No authorization on file")],
        required=True, default="match")
    truck_details = fields.Char(string="Truck / Plate")
    signature = fields.Image(string="Driver Signature", max_width=1024,
                             max_height=1024)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        picking = self.env["stock.picking"].browse(
            self.env.context.get("active_id") or self.env.context.get("default_picking_id"))
        collection = self.env["logistics.pickup.collection"].search(
            [("picking_id", "=", picking.id)], limit=1)
        if collection:
            values.update({
                "picking_id": picking.id,
                "collection_id": collection.id,
                "collected_by_name": collection.collected_by_name or "",
                "collected_company": (collection.collected_company
                                      or collection.authorized_contact_name or ""),
                "verification_result": collection.verification_result or "match",
                "truck_details": collection.truck_details or "",
            })
            if collection.verification_method:
                values["verification_method"] = collection.verification_method
        return values

    def action_apply(self):
        """The same rules as the back-office control — a claimed company name
        is not a verification, and a substituted carrier is refused."""
        self.ensure_one()
        if self.collected_company and self.authorized_party \
                and self.collected_company.strip().lower() \
                != self.authorized_party.strip().lower() \
                and self.verification_result == "match":
            raise UserError(_(
                "“%s” is not the authorized party (“%s”). Record the result as "
                "“Different company came to collect” — which then needs a "
                "manager override — or have the customer update the "
                "authorization.", self.collected_company, self.authorized_party))
        vals = {
            "collected_by_name": self.collected_by_name,
            "collected_company": self.collected_company,
            "verification_method": self.verification_method,
            "verification_result": self.verification_result,
            "truck_details": self.truck_details or False,
        }
        if self.signature:
            vals["signature"] = self.signature
        self.collection_id.write(vals)
        return {"type": "ir.actions.act_window_close"}

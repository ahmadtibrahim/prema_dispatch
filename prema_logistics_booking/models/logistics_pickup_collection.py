# -*- coding: utf-8 -*-
"""Pickup authorization and collection — trip-specific, per release.

A saved contact is a reusable address-book entry; it never authorizes a
pickup on its own. Authorization belongs to ONE booking (and, once released,
to ONE native outbound transfer), so this record carries:

* a **snapshot** of the authorized party and the driver details, frozen at the
  moment they are used — editing or archiving the saved contact afterwards
  cannot rewrite a completed collection;
* the **collection** facts staff record at the dock: who actually collected,
  the verification result, which pallets left, when, and the acknowledgement
  signature (the native `stock.picking.signature` Image pattern).

Every authorization/driver field is `tracking=True`, so who changed what, when
and from which value to which is recorded natively in the chatter — no
parallel audit model, and the history survives edits and archives.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

#: The only fields a portal customer may change: their authorization for a
#: pickup that has not happened yet. Everything a staff member records at the
#: dock is out of their reach.
PORTAL_EDITABLE_FIELDS = frozenset({
    "authorized_contact_id", "driver_name", "driver_phone", "truck_details",
    "pickup_reference", "customer_instructions",
})


class LogisticsPickupCollection(models.Model):
    _name = "logistics.pickup.collection"
    _description = "Pickup Authorization / Collection"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    booking_id = fields.Many2one(
        "logistics.booking", required=True, ondelete="cascade", index=True)
    commercial_partner_id = fields.Many2one(
        related="booking_id.commercial_partner_id", store=True, index=True)
    picking_id = fields.Many2one(
        "stock.picking", string="Outbound Transfer", ondelete="set null", index=True)

    state = fields.Selection(
        [("authorized", "Authorized"), ("collected", "Collected"),
         ("cancelled", "Cancelled")],
        default="authorized", required=True, tracking=True)

    # ── authorization — a SNAPSHOT, not a live read of the contact ─────
    authorized_contact_id = fields.Many2one(
        "logistics.private.contact", string="Saved Contact", ondelete="set null")
    authorized_contact_name = fields.Char(tracking=True)
    authorized_contact_phone = fields.Char(tracking=True)
    authorized_contact_email = fields.Char(tracking=True)
    authorized_contact_street = fields.Char(tracking=True)
    authorized_contact_city = fields.Char(tracking=True)
    driver_name = fields.Char(tracking=True)
    driver_phone = fields.Char(tracking=True)
    truck_details = fields.Char(string="Truck / Plate", tracking=True)
    pickup_reference = fields.Char(tracking=True)
    customer_instructions = fields.Text(tracking=True)

    # ── collection — recorded by staff at the dock ─────────────────────
    collected_by_name = fields.Char(string="Collected By", tracking=True)
    verification_result = fields.Selection(
        [("match", "Authorized party verified"),
         ("mismatch", "Driver does not match the authorization"),
         ("no_authorization", "No authorization on file")],
        tracking=True, copy=False)
    released_packages = fields.Text(
        string="Pallets / Packages Released", copy=False,
        help="Snapshot of the package names that left on this collection.")
    released_pallet_count = fields.Integer(copy=False)
    collection_time = fields.Datetime(copy=False)
    collected_by_user_id = fields.Many2one("res.users", string="Released By", copy=False)
    signature = fields.Image("Collection Acknowledgement", copy=False,
                             max_width=1024, max_height=1024)
    is_signed = fields.Boolean(compute="_compute_is_signed")

    # ── manager override ───────────────────────────────────────────────
    override_reason = fields.Text(copy=False)
    override_user_id = fields.Many2one("res.users", copy=False)
    override_at = fields.Datetime(copy=False)

    @api.depends("signature")
    def _compute_is_signed(self):
        for record in self:
            record.is_signed = bool(record.signature)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.name in (False, "New"):
                record.name = "PICK/%s" % (record.id,)
        return records

    # ── who may change what ────────────────────────────────────────────
    def _is_portal_user(self):
        return self.env.user.has_group("base.group_portal") and \
            not self.env.user.has_group("base.group_user")

    def write(self, vals):
        """A portal customer may edit only their own booking's authorization,
        and only while the collection has not happened."""
        if self._is_portal_user():
            forbidden = set(vals) - PORTAL_EDITABLE_FIELDS
            if forbidden:
                raise AccessError(_(
                    "You may change the pickup authorization only; %s cannot be "
                    "edited from the portal.", ", ".join(sorted(forbidden))))
            if any(record.state == "collected" for record in self):
                raise UserError(_(
                    "This pickup has already been collected — its authorization "
                    "can no longer be changed."))
        return super().write(vals)

    def action_record_collection(self):
        """Staff confirm the release: only an internal user, and only when the
        arriving party matches (or a manager overrode with a reason)."""
        self.ensure_one()
        if self._is_portal_user():
            raise AccessError(_("Only warehouse staff can record a collection."))
        if self.verification_result in (False, "mismatch", "no_authorization") \
                and not (self.override_reason and self.override_user_id):
            raise UserError(_(
                "Resolve the pickup authorization first: record a matching "
                "verification, or an override with a reason."))
        self.write({
            "state": "collected",
            "collection_time": fields.Datetime.now(),
            "collected_by_user_id": self.env.user.id,
        })
        return True

    def action_apply_snapshot(self, contact):
        """Freeze the chosen saved contact onto this collection."""
        self.ensure_one()
        contact.ensure_one()
        return self.write(dict(contact._snapshot_vals(),
                               authorized_contact_id=contact.id))

# -*- coding: utf-8 -*-
"""Pickup authorization and collection — trip-specific, per release.

A saved contact is a reusable address-book entry; it never authorizes a pickup
on its own. Authorization belongs to ONE service booking and, once released, to
ONE native outbound transfer. This record therefore carries:

* a **snapshot** of the authorized party and the driver details, frozen at the
  moment they were given — editing or archiving the saved contact afterwards
  cannot rewrite a completed collection;
* the **collection** facts staff record at the dock: who actually collected,
  the verification result, which pallets left, when, and the acknowledgement
  signature (the native `stock.picking.signature` Image pattern).

One service booking can have SEVERAL collection records — one per outbound
release — so a partial pickup keeps its own authorized party, its own pallets
and its own history, and a later driver assignment never overwrites an earlier
collection.

Every authorization/driver field is `tracking=True`, so who changed what, when,
and from which value to which is recorded natively in the chatter — no parallel
audit model, and the history survives edits and archives.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

#: The only fields a portal customer may change directly: the driver details
#: and instructions for a pickup that has not happened yet. Everything staff
#: record at the dock is out of their reach, and the authorized party / the
#: recipient are partner links — those are set by the controller under sudo
#: only AFTER it has validated that the chosen contact belongs to the caller,
#: so a raw partner id can never be injected from a form.
PORTAL_EDITABLE_FIELDS = frozenset({
    "driver_name", "driver_phone", "truck_details",
    "pickup_reference", "customer_instructions",
})


class LogisticsPickupCollection(models.Model):
    _name = "logistics.pickup.collection"
    _description = "Pickup Authorization / Collection"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    sale_order_id = fields.Many2one(
        "sale.order", string="Service Booking", required=True,
        ondelete="cascade", index=True)
    commercial_partner_id = fields.Many2one(
        related="sale_order_id.partner_id.commercial_partner_id",
        store=True, index=True)
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
    outbound_recipient_id = fields.Many2one(
        "res.partner", string="Delivery Recipient", ondelete="set null",
        help="Who the goods are going to, when that differs from the booking "
             "customer. Copied to the release's transfer partner.")
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
        # `env.su` is the framework's own elevation: trusted server-side code
        # that has already checked ownership is not "a portal user editing".
        return (not self.env.su
                and self.env.user.has_group("base.group_portal")
                and not self.env.user.has_group("base.group_user"))

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

    # ── snapshot ───────────────────────────────────────────────────────
    def action_apply_snapshot(self, contact):
        """Freeze the chosen saved contact onto this collection."""
        self.ensure_one()
        contact.ensure_one()
        return self.write(dict(contact._snapshot_vals(), authorized_contact_id=contact.id))

    # ── staff: record the release ──────────────────────────────────────
    def action_record_collection(self):
        self.ensure_one()
        if self._is_portal_user():
            raise AccessError(_("Only warehouse staff can record a collection."))
        if not self.verification_result \
                or self.verification_result in ("mismatch", "no_authorization"):
            if not (self.override_reason and self.override_user_id):
                raise UserError(_(
                    "Resolve the pickup authorization first: record a matching "
                    "verification, or a manager override with a reason."))
        self.write({
            "state": "collected",
            "collection_time": fields.Datetime.now(),
            "collected_by_user_id": self.env.user.id,
        })
        return True

    def action_apply_override(self, reason):
        self.ensure_one()
        if self._is_portal_user():
            raise AccessError(_("Only warehouse staff can override."))
        if not (reason or "").strip():
            raise UserError(_("An override reason is required."))
        self.write({"override_reason": reason.strip(),
                    "override_user_id": self.env.user.id,
                    "override_at": fields.Datetime.now()})
        return True

    def action_pull_released_packages(self):
        """Snapshot the packages that actually left on the linked transfer."""
        self.ensure_one()
        if not self.picking_id:
            return False
        packages = self.picking_id.move_line_ids.filtered(
            lambda line: line.state == "done").mapped("result_package_id")
        if not packages and self.picking_id.state != "done":
            packages = self.picking_id.move_line_ids.mapped("result_package_id")
        names = sorted({p.name for p in packages if p.name})
        return self.write({
            "released_packages": ", ".join(names) if names else "—",
            "released_pallet_count": len(names),
        })

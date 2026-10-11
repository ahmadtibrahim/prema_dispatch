# -*- coding: utf-8 -*-
"""Pickup authorization and collection — trip-specific, per release.

A saved contact is a reusable address-book entry; it never authorizes a pickup
on its own. Authorization belongs to ONE service booking and, once released, to
ONE native outbound transfer. This record therefore carries:

* a **snapshot** of the authorized party and the driver details, frozen at the
  moment they were given — editing or archiving the saved contact afterwards
  cannot rewrite a completed collection;
* the **collection** facts staff record at the dock: who actually collected and
  for which company, HOW that driver's authority was verified, which pallets
  left, when, and the acknowledgement signature (the native
  `stock.picking.signature` Image pattern).

One service booking can have SEVERAL collection records — one per outbound
release — so a partial pickup keeps its own authorized party, its own pallets
and its own history, and a later driver assignment never overwrites an earlier
collection.

Every authorization/driver field is `tracking=True`, so who changed what, when,
and from which value to which is recorded natively in the chatter — no parallel
audit model, and the history survives edits and archives.

**A blank record is never "authorized".** A customer may authorize a company
without knowing the driver, but a collection with no authorized party on file is
reported as pending, refuses to be recorded as collected, and blocks the linked
stock release — it is never presented as an authorization the customer gave.
"""
from datetime import timezone
from zoneinfo import ZoneInfo

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

#: Warehouse appointments and dock times are read in the warehouse's own zone;
#: storage is always UTC.
WAREHOUSE_TZ = "America/Toronto"

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

#: How staff established that the person at the dock may collect for the
#: authorized company. Chosen explicitly — a claimed company name on its own is
#: NOT a verification, which is why this is a required, recorded fact.
VERIFICATION_METHODS = [
    ("photo_id", "Government photo ID checked at the dock"),
    ("carrier_dispatched", "Collecting company confirmed the driver to PF Logistics"),
    ("named_on_authorization", "The customer named this individual in the authorization"),
    ("manager_override", "Manager override — reason recorded below"),
]


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
    picking_state = fields.Selection(
        related="picking_id.state", string="Release Status", readonly=True)

    state = fields.Selection(
        [("authorized", "Authorized"), ("collected", "Collected"),
         ("cancelled", "Cancelled")],
        default="authorized", required=True, tracking=True)

    # ── authorization — a SNAPSHOT, not a live read of the contact ─────
    authorized_contact_id = fields.Many2one(
        "logistics.private.contact", string="Saved Contact", ondelete="set null")
    authorized_contact_name = fields.Char(
        string="Authorized Company / Person", tracking=True)
    authorized_contact_phone = fields.Char(tracking=True)
    authorized_contact_email = fields.Char(tracking=True)
    authorized_contact_street = fields.Char(tracking=True)
    authorized_contact_city = fields.Char(tracking=True)
    outbound_recipient_id = fields.Many2one(
        "res.partner", string="Delivery Recipient", ondelete="set null",
        help="Who the goods are going to, when that differs from the booking "
             "customer. Copied to the release's transfer partner.")
    driver_name = fields.Char(
        string="Driver Name", tracking=True,
        help="Optional. Only filled in when the customer knows which driver is "
             "coming; the company authorization stands on its own without it.")
    driver_phone = fields.Char(tracking=True)
    truck_details = fields.Char(string="Truck / Plate", tracking=True)
    pickup_reference = fields.Char(tracking=True)
    customer_instructions = fields.Text(tracking=True)

    #: True once the customer has named a party. A record can exist with no
    #: party (the form was opened, nothing was authorized yet) — that is
    #: "pending", never "authorized".
    has_authorized_party = fields.Boolean(
        string="Party Named", compute="_compute_has_authorized_party")
    display_status = fields.Char(
        string="Status", compute="_compute_display_status")
    authorization_label = fields.Char(
        string="Authorized Party", compute="_compute_authorization_label")

    # ── collection — recorded by staff at the dock ─────────────────────
    collected_by_name = fields.Char(string="Collected By", tracking=True)
    collected_company = fields.Char(
        string="Collecting Company", tracking=True,
        help="The company the driver actually collected for. Usually the "
             "authorized party; anything else needs a manager override.")
    verification_method = fields.Selection(
        VERIFICATION_METHODS, string="How Authority Was Verified",
        tracking=True, copy=False)
    verification_result = fields.Selection(
        [("match", "Collecting company is the authorized party"),
         ("mismatch", "Different company came to collect"),
         ("no_authorization", "No authorization on file")],
        string="Verification Result", tracking=True, copy=False)
    released_packages = fields.Text(
        string="Pallets Released", copy=False,
        help="Snapshot of the pallet IDs that left on this collection.")
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

    @api.depends("authorized_contact_name", "authorized_contact_id")
    def _compute_has_authorized_party(self):
        for record in self:
            record.has_authorized_party = bool(
                record.authorized_contact_name or record.authorized_contact_id)

    @api.depends("state", "authorized_contact_name", "authorized_contact_id")
    def _compute_display_status(self):
        for record in self:
            if record.state == "collected":
                record.display_status = _("Collected")
            elif record.state == "cancelled":
                record.display_status = _("Cancelled")
            elif record.has_authorized_party:
                record.display_status = _("Authorized")
            else:
                record.display_status = _("Authorization pending — no party named")

    @api.depends("authorized_contact_name")
    def _compute_authorization_label(self):
        for record in self:
            record.authorization_label = (
                record.authorized_contact_name or _("Not authorized yet"))

    @api.depends("signature")
    def _compute_is_signed(self):
        for record in self:
            record.is_signed = bool(record.signature)

    def pickup_time_label(self):
        """The collection instant as the warehouse reads it, zone-labelled.

        Rendered into the Proof of Pickup, where an unlabelled timestamp would
        be ambiguous: the stored value is UTC and the dock works in Toronto.
        """
        self.ensure_one()
        if not self.collection_time:
            return ""
        local = (self.collection_time.replace(tzinfo=timezone.utc)
                 .astimezone(ZoneInfo(WAREHOUSE_TZ)))
        return "%s (%s)" % (local.strftime("%Y-%m-%d %I:%M %p"), WAREHOUSE_TZ)

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

    # ── staff: is this release allowed to leave? ───────────────────────
    def _override_approved(self):
        self.ensure_one()
        return bool(self.override_reason and self.override_user_id)

    def company_matches_authorization(self):
        """Does the company that actually turned up match the authorized party?

        No company recorded yet is not a mismatch — the customer's own company
        name is already on the authorization.
        """
        self.ensure_one()
        if not self.collected_company or not self.authorized_contact_name:
            return True
        return (self.collected_company.strip().lower()
                == self.authorized_contact_name.strip().lower())

    def is_authorized_and_verified(self):
        """True when the customer's authorization exists AND a member of staff
        has recorded a verification that holds up (or an approved manager
        override).

        A "match" only counts when the company that actually collected is the
        authorized party: otherwise a substituted carrier could clear the
        release by ticking "match" and the pallets would be gone before anyone
        noticed.

        Deliberately says nothing about the collection being *recorded*: that
        happens after the stock release is validated, and tying the two
        together would deadlock the dock.
        """
        self.ensure_one()
        if not self.has_authorized_party:
            return False
        if (self.verification_result == "match" and self.verification_method
                and self.company_matches_authorization()):
            return True
        return self._override_approved()

    # ── staff: record the release ──────────────────────────────────────
    def action_record_collection(self):
        self.ensure_one()
        if self._is_portal_user():
            raise AccessError(_("Only warehouse staff can record a collection."))
        if not self.has_authorized_party:
            raise UserError(_(
                "There is no authorized pickup party on this booking. Ask the "
                "customer to name the company allowed to collect, or record a "
                "manager override with a reason."))
        if not self.verification_method:
            raise UserError(_(
                "Record how the collecting driver's authority was verified. A "
                "company name on its own is not a verification."))
        if not self.verification_result:
            raise UserError(_("Record the verification result."))
        if self.verification_result != "match" and not self._override_approved():
            raise UserError(_(
                "This was not the authorized party. Ask the customer to update "
                "the authorization, or record a manager override with a reason."))
        if self.verification_result == "match" \
                and not self.company_matches_authorization():
            raise UserError(_(
                "“%s” is not the authorized party (“%s”). Record the result as "
                "“Different company came to collect” with a manager override, or "
                "have the customer update the authorization.",
                self.collected_company, self.authorized_contact_name))
        if not self.picking_id:
            raise UserError(_(
                "This authorization has no linked stock release yet. Create the "
                "PFL release for the booking first, so the pallets that leave "
                "are the ones recorded."))
        if self.picking_id.state != "done":
            raise UserError(_(
                "The linked release %s is not validated yet. Hand the pallets "
                "over, validate the transfer, and then record the collection.",
                self.picking_id.display_name))
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
        packages = self.picking_id.move_line_ids.mapped("result_package_id")
        names = sorted({p.name for p in packages if p.name})
        return self.write({
            "released_packages": ", ".join(names) if names else "—",
            "released_pallet_count": len(names),
        })

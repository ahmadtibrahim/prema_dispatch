# -*- coding: utf-8 -*-
"""Private contacts — a portal user's own address book.

`res.partner` remains the SINGLE SOURCE for the reusable contact details
(name, company, phone, email, address): every field exposed here is a
`related` field that writes straight through to the Contact, so there is never
a second editable copy to drift.

This model carries only what is genuinely private to the portal user:

* `user_id`     — who owns this entry (the isolation key),
* `contact_type`— company/carrier, owner-operator or individual,
* `active`      — archive.

Two users may save the same company and each gets their own row pointing at
their own Contact. Nothing is merged, and nothing is visible across users.
"""
from odoo import api, fields, models

CONTACT_TYPES = [
    ("company", "Company / Carrier"),
    ("owner_operator", "Owner-Operator"),
    ("individual", "Individual"),
]

#: Fields copied from the private contact onto the Contact on create/update.
_DETAIL_FIELDS = (
    "name", "phone", "email", "street", "street2", "city", "zip",
    "state_id", "country_id",
)


class LogisticsPrivateContact(models.Model):
    _name = "logistics.private.contact"
    _description = "Private Contact"
    _order = "name, id"

    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, index=True,
        ondelete="cascade", default=lambda self: self.env.user)

    #: The native Contact. Always created by this model — never adopt an
    #: arbitrary existing partner id from a request.
    partner_id = fields.Many2one(
        "res.partner", string="Contact", required=True,
        ondelete="cascade", index=True)

    contact_type = fields.Selection(CONTACT_TYPES, required=True, default="company")
    active = fields.Boolean(default=True)

    #: For a company, the person to ask for — a native child Contact.
    contact_person_id = fields.Many2one(
        "res.partner", string="Contact Person",
        domain="[('parent_id', '=', partner_id)]", ondelete="set null")

    company_id = fields.Many2one(related="partner_id.company_id", store=True)

    # ── related details (write-through to the native Contact) ──────────
    name = fields.Char(related="partner_id.name", readonly=False)
    phone = fields.Char(related="partner_id.phone", readonly=False)
    email = fields.Char(related="partner_id.email", readonly=False)
    street = fields.Char(related="partner_id.street", readonly=False)
    street2 = fields.Char(related="partner_id.street2", readonly=False)
    city = fields.Char(related="partner_id.city", readonly=False)
    zip = fields.Char(related="partner_id.zip", readonly=False)
    state_id = fields.Many2one(related="partner_id.state_id", readonly=False)
    country_id = fields.Many2one(related="partner_id.country_id", readonly=False)

    #: Read-through helpers for the child Contact. They are deliberately NOT
    #: `readonly=False` computed fields: such a field is recomputed from its
    #: dependency and silently discards a value supplied at create time. The
    #: write path below takes the values from `vals` instead.
    contact_person_name = fields.Char(
        string="Contact Person Name", related="contact_person_id.name")
    contact_person_phone = fields.Char(
        string="Contact Person Phone", related="contact_person_id.phone")
    contact_person_email = fields.Char(
        string="Contact Person Email", related="contact_person_id.email")

    _sql_constraints = [
        ("logistics_private_contact_uniq", "unique(user_id, partner_id)",
         "That contact is already in your list."),
    ]

    # ── contact person (a native child Contact) ────────────────────────
    CONTACT_PERSON_KEYS = ("contact_person_name", "contact_person_phone",
                           "contact_person_email")

    def _sync_contact_person(self, values):
        """Create or update the child Contact from the submitted values.

        The values come from `vals` (what the caller actually sent), never from
        the related display fields — a related field reflects the child, so
        reading it back here would be circular.
        """
        for contact in self:
            name = (values.get("contact_person_name") or "").strip()
            phone = (values.get("contact_person_phone") or "").strip()
            email = (values.get("contact_person_email") or "").strip()
            if not (name or phone or email):
                continue
            Partner = self.env["res.partner"].sudo()
            if not contact.contact_person_id:
                contact.contact_person_id = Partner.create({
                    "name": name or contact.partner_id.name,
                    "parent_id": contact.partner_id.id,
                    "type": "contact",
                    "company_id": False,
                    "phone": phone or False,
                    "email": email or False,
                }).id
            else:
                child_vals = {}
                if name:
                    child_vals["name"] = name
                if phone:
                    child_vals["phone"] = phone
                if email:
                    child_vals["email"] = email
                if child_vals:
                    contact.contact_person_id.sudo().write(child_vals)

    # ── the Contact is created here, never adopted ─────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        Partner = self.env["res.partner"].sudo()
        people = [{key: vals.pop(key) for key in self.CONTACT_PERSON_KEYS if key in vals}
                  for vals in vals_list]
        for vals in vals_list:
            if not vals.get("partner_id"):
                partner_vals = {
                    key: vals.pop(key) for key in list(vals)
                    if key in _DETAIL_FIELDS and vals.get(key) is not None
                }
                partner_vals.setdefault("name", "New Contact")
                partner_vals["company_type"] = (
                    "company" if vals.get("contact_type") == "company" else "person")
                # A private contact must never join the customer's company
                # hierarchy or the accounting tree.
                partner_vals.update({"company_id": False, "parent_id": False,
                                     "customer_rank": 0, "supplier_rank": 0})
                vals["partner_id"] = Partner.create(partner_vals).id
            else:
                # Only ever accept a Contact this model already owns.
                raise ValueError("partner_id is managed by the private contact")
        contacts = super().create(vals_list)
        for contact, person in zip(contacts, people):
            contact._sync_contact_person(person)
        return contacts

    def write(self, vals):
        person = {key: vals.pop(key) for key in self.CONTACT_PERSON_KEYS if key in vals}
        res = super().write(vals)
        if person:
            self._sync_contact_person(person)
        return res

    # ── reusability ────────────────────────────────────────────────────
    def _snapshot_vals(self):
        """The frozen copy written onto a collection record.

        Completed collections keep these values forever, so editing or
        archiving the saved contact can never rewrite history.
        """
        self.ensure_one()
        return {
            "authorized_contact_name": self.name or "",
            "authorized_contact_phone": self.phone or "",
            "authorized_contact_email": self.email or "",
            "authorized_contact_street": self.street or "",
            "authorized_contact_city": self.city or "",
        }

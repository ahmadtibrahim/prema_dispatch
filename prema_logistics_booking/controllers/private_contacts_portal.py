# -*- coding: utf-8 -*-
"""Portal — My Contacts (private, per portal user).

Ownership is the INDIVIDUAL portal user. Every route resolves the caller from
`request.env.user` and then re-checks ownership of any submitted id before
touching a record, so a foreign id posted directly is rejected rather than
trusted. The `base.group_portal` ir.rule
(`ir_rule_private_contact_portal_own`) is the second layer — dropdown
filtering alone is never relied on.
"""
import json
import logging

from odoo import http
from odoo.http import request
from werkzeug.exceptions import NotFound

_logger = logging.getLogger(__name__)

CONTACT_FIELDS = (
    "name", "contact_type", "phone", "email", "street", "city",
    "contact_person_name", "contact_person_phone",
)


class PrivateContactsPortal(http.Controller):

    # ── ownership helpers (the security core) ──────────────────────────
    def _user(self):
        return request.env.user

    def _own_domain(self, extra=None):
        domain = [("user_id", "=", self._user().id)]
        return domain + (extra or [])

    def _own_contact(self, contact_id, raise_if_missing=True):
        """Resolve a submitted id to a row THIS user owns.

        A missing, malformed or foreign id is simply not found — the caller
        can never act on another user's contact.
        """
        try:
            contact_id = int(contact_id or 0)
        except (TypeError, ValueError):
            contact_id = 0
        contact = request.env["logistics.private.contact"].search(
            self._own_domain([("id", "=", contact_id)]), limit=1)
        if not contact and raise_if_missing:
            raise NotFound()
        return contact

    def _vals_from_post(self, post, existing=None):
        vals = {}
        name = (post.get("name") or "").strip()
        if name:
            vals["name"] = name
        contact_type = post.get("contact_type")
        if contact_type in dict(
                request.env["logistics.private.contact"]._fields["contact_type"].selection):
            vals["contact_type"] = contact_type
        for key in ("phone", "email", "street", "city",
                    "contact_person_name", "contact_person_phone"):
            if key in post:
                vals[key] = (post.get(key) or "").strip()
        return vals

    # ── pages ──────────────────────────────────────────────────────────
    @http.route("/my/contacts", type="http", auth="user", website=True, sitemap=False)
    def my_contacts(self, **kwargs):
        contacts = request.env["logistics.private.contact"].search(
            self._own_domain(), order="name")
        return request.render("prema_logistics_booking.portal_my_contacts", {
            "contacts": contacts,
            "page_name": "contacts",
            "msg": kwargs.get("msg"),
        })

    @http.route("/my/contacts/new", type="http", auth="user", website=True, sitemap=False)
    def my_contact_new(self, **kwargs):
        return request.render("prema_logistics_booking.portal_my_contact_form", {
            "contact": request.env["logistics.private.contact"].browse(),
            "page_name": "contacts",
        })

    @http.route("/my/contacts/<int:contact_id>/edit", type="http", auth="user",
                website=True, sitemap=False)
    def my_contact_edit(self, contact_id, **kwargs):
        contact = self._own_contact(contact_id)
        return request.render("prema_logistics_booking.portal_my_contact_form", {
            "contact": contact,
            "page_name": "contacts",
        })

    @http.route("/my/contacts/save", type="http", auth="user", website=True,
                methods=["POST"], csrf=True, sitemap=False)
    def my_contact_save(self, **post):
        contact_id = post.get("contact_id")
        vals = self._vals_from_post(post)
        if contact_id:
            contact = self._own_contact(contact_id)   # rejects a foreign id
            contact.write(vals)
            return request.redirect("/my/contacts?msg=saved")
        if not vals.get("name"):
            return request.redirect("/my/contacts/new?msg=name_required")
        contact = request.env["logistics.private.contact"].create(
            dict(vals, user_id=self._user().id))
        return request.redirect("/my/contacts?msg=created&highlight=%s" % contact.id)

    @http.route("/my/contacts/<int:contact_id>/archive", type="http", auth="user",
                website=True, methods=["POST"], csrf=True, sitemap=False)
    def my_contact_archive(self, contact_id, **post):
        contact = self._own_contact(contact_id)       # rejects a foreign id
        contact.write({"active": False})
        return request.redirect("/my/contacts?msg=archived")

    # ── selector support (booking form) ────────────────────────────────
    @http.route("/my/contacts/search", type="http", auth="user", website=True,
                sitemap=False)
    def my_contacts_search(self, q=None, **kwargs):
        """Suggestions for the booking selectors — OWN contacts only."""
        domain = self._own_domain()
        term = (q or "").strip()
        if term:
            domain += ["|", ("name", "ilike", term), ("contact_person_name", "ilike", term)]
        contacts = request.env["logistics.private.contact"].search(domain, limit=20)
        payload = {"results": [{
            "id": c.id,
            "name": c.name or "",
            "contact_type": c.contact_type,
            "label": c.name or "",
            "phone": c.phone or "",
        } for c in contacts]}
        return request.make_response(
            json.dumps(payload), headers=[("Content-Type", "application/json")])

    @http.route("/my/contacts/quick-create", type="http", auth="user", website=True,
                methods=["POST"], csrf=True, sitemap=False)
    def my_contact_quick_create(self, **post):
        """Create a contact from the booking form and return it so the form can
        select it immediately, keeping everything already entered."""
        vals = self._vals_from_post(post)
        if not vals.get("name"):
            return request.make_response(
                json.dumps({"error": "name_required"}),
                headers=[("Content-Type", "application/json")])
        contact = request.env["logistics.private.contact"].create(
            dict(vals, user_id=self._user().id))
        return request.make_response(json.dumps({
            "id": contact.id,
            "name": contact.name or "",
            "contact_type": contact.contact_type,
        }), headers=[("Content-Type", "application/json")])

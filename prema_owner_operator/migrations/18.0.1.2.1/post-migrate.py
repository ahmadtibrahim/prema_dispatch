# -*- coding: utf-8 -*-
"""18.0.1.2.1 — refresh module mail templates from their XML definition.

The templates were created with noupdate=1, so a body fix never reached the
database. This migration reads the module's own XML and updates the template
bodies/subjects (and clears the noupdate flag for future upgrades). No email
is sent.
"""
import os

from lxml import etree

from odoo import SUPERUSER_ID, api


def _refresh_templates(env):
    module_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.join(module_root, "data", "mail_templates.xml")
    if not os.path.exists(path):
        return 0
    tree = etree.parse(path)
    updated = 0
    for record in tree.xpath('//record[@model="mail.template"]'):
        xmlid = record.get("id")
        if not xmlid:
            continue
        data = env["ir.model.data"].sudo().search([
            ("module", "=", "prema_owner_operator"),
            ("name", "=", xmlid),
        ], limit=1)
        if not data:
            continue
        data.noupdate = False
        template = env["mail.template"].sudo().browse(data.res_id)
        vals = {}
        for field in record.xpath("field"):
            name = field.get("name")
            if name == "body_html":
                # The body contains QWeb elements (<t t-out=...>), not just
                # text; field.text alone would truncate at the first one.
                parts = [field.text or ""]
                for child in field:
                    parts.append(etree.tostring(
                        child, encoding="unicode", method="html"))
                vals[name] = "".join(parts)
            elif name in ("subject", "email_from", "partner_to"):
                vals[name] = field.text or ""
        if vals:
            template.write(vals)
            updated += 1
    return updated


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _refresh_templates(env)

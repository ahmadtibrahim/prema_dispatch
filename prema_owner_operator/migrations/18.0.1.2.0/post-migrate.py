# -*- coding: utf-8 -*-
"""18.0.1.2.0 — revised pipeline, multi-selects and notification fields.

Deliberately silent: no applicant is advanced and no email is sent. Existing
applicants on a removed stage are moved to New; the old stage records are
kept. Historical single selections are copied into the new multi-select
fields, and the legacy WhatsApp answer is interpreted WITHOUT assuming the
contact phone is a WhatsApp number.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.prema_owner_operator.application_mapping import (
    SERVICE_AREA_CODES, WHATSAPP_CONSENT_TEXT)
from odoo.addons.prema_owner_operator.hooks import (
    ensure_pipeline, ensure_verification_alias)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_pipeline(env)
    ensure_verification_alias(env)

    Area = env["prema.owner.operator.service.area"].sudo()
    Pref = env["prema.owner.operator.work.preference"].sudo()
    apps = env["prema.owner.operator.application"].sudo().search([])
    for app in apps:
        vals = {}
        if not app.preferred_service_area_ids and app.preferred_service_area:
            code = SERVICE_AREA_CODES.get(app.preferred_service_area)
            area = Area.search([("code", "=", code)], limit=1) if code else Area
            if area:
                vals["preferred_service_area_ids"] = [(6, 0, area.ids)]
                vals["preferred_lanes"] = app.preferred_lanes or area.name
        if not app.work_preference_ids and app.work_preference:
            pref = Pref.search(
                [("code", "=", app.work_preference)], limit=1)
            if pref:
                vals["work_preference_ids"] = [(6, 0, pref.ids)]
        if not app.load_notification_channel and app.whatsapp_consent:
            if app.whatsapp_consent == "yes":
                vals.update({
                    "load_notification_channel": "whatsapp",
                    "whatsapp_consent_granted": True,
                    "whatsapp_consent_text": WHATSAPP_CONSENT_TEXT,
                    "whatsapp_consent_version": "legacy",
                    "whatsapp_consent_at": app.write_date,
                    # Never infer that the contact phone is WhatsApp-enabled.
                })
            else:
                vals.update({
                    "load_notification_channel": "email",
                    "load_notification_email": app.email or False,
                    "whatsapp_consent_granted": False,
                })
        if vals:
            app.with_context(oo_skip_notification_log=True).write(vals)

# -*- coding: utf-8 -*-
"""18.0.1.2.6 — normalize legacy combined answers into the new columns.

Fills only EMPTY columns. Legacy values (equipment_model,
truck_plate_jurisdiction, licence_jurisdiction) are preserved. No applicant,
stage, attachment, COI version or document is moved, and no email is sent.
"""
import re

from odoo import api

PROVINCES = {
    "ON": "Ontario", "QC": "Quebec", "AB": "Alberta",
    "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
    "NT": "Northwest Territories", "NU": "Nunavut",
    "PE": "Prince Edward Island", "SK": "Saskatchewan", "YT": "Yukon",
}
NAME_TO_CODE = {name.lower(): code for code, name in PROVINCES.items()}


def _province_code(value):
    raw = (value or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in PROVINCES:
        return upper
    return NAME_TO_CODE.get(raw.lower())


def _split_make_model(value):
    parts = (value or "").strip().split(None, 1)
    if not parts:
        return None, None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, 1, {})
    App = env["prema.owner.operator.application"].sudo()

    for app in App.search([]):
        vals = {}
        if not app.home_province_code and app.home_province:
            code = _province_code(app.home_province)
            if code:
                vals["home_province_code"] = code
        if not app.licence_province and app.licence_jurisdiction:
            code = _province_code(app.licence_jurisdiction)
            if code:
                vals["licence_province"] = code
        if not app.plate_province:
            code = _province_code(app.plate_jurisdiction)
            if not code and app.truck_plate_jurisdiction:
                parts = re.split(r"\s*[/,]\s*",
                                 app.truck_plate_jurisdiction)
                if len(parts) > 1:
                    code = _province_code(" ".join(parts[1:]))
            if code:
                vals["plate_province"] = code
        if not app.truck_make and not app.truck_model \
                and app.equipment_model:
            make, model = _split_make_model(app.equipment_model)
            if make:
                vals["truck_make"] = make
            if model:
                vals["truck_model"] = model
        if not app.reefer_applicable:
            if app.trailer_type == "reefer":
                vals["reefer_applicable"] = "yes"
            elif app.cargo_body_type == "reefer":
                vals["reefer_applicable"] = "yes"
            else:
                vals["reefer_applicable"] = "not_applicable"
        if vals:
            app.write(vals)

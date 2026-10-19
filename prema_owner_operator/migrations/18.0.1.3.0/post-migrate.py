# -*- coding: utf-8 -*-
"""18.0.1.3.0 — TDG status + CVOR extraction, legacy-safe."""
from odoo import api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, 1, {})
    apps = env["prema.owner.operator.application"].sudo().search([])
    for app in apps:
        vals = {}
        if app.tdg_training == "yes" and app.tdg_status != "current":
            vals["tdg_status"] = "current"
        elif app.tdg_training == "expired" and app.tdg_status != "expired":
            vals["tdg_status"] = "expired"
        elif app.tdg_training == "no" and not app.tdg_status:
            vals["tdg_status"] = "none"
        if vals:
            app.write(vals)

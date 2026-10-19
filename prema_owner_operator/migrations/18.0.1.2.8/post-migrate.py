# -*- coding: utf-8 -*-
"""18.0.1.2.8 — cargo-height unit contract.

1.2.7 wrote inches into the native ``x_box_interior_height_ft`` field, which
is declared in FEET. This migration converts values already written on
OPERATOR-LINKED vehicles only and preserves the truthful inches in the new
``oo_cargo_height_in`` field. Company trucks are never touched.
"""
from odoo import api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, 1, {})
    vehicles = env["fleet.vehicle"].sudo().search(
        [("operator_id", "!=", False)])
    for vehicle in vehicles:
        height = vehicle.x_box_interior_height_ft or 0.0
        if height > 20.0:
            # Values above 20 cannot be a plausible feet height; 1.2.7 wrote
            # the application's inches here.
            vehicle.write({
                "oo_cargo_height_in": height,
                "x_box_interior_height_ft": height / 12.0,
            })
        elif height > 0.0:
            vehicle.write({"oo_cargo_height_in": height * 12.0})

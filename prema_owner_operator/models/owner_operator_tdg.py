# -*- coding: utf-8 -*-
"""Canadian TDG dangerous-goods classes (Part 6 activity/class coverage)."""
from odoo import fields, models


class OwnerOperatorTDGClass(models.Model):
    _name = "prema.owner.operator.tdg.class"
    _description = "Owner-Operator TDG Class"
    _order = "sequence, id"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("tdg_class_code_unique", "UNIQUE(code)", "TDG class codes are unique."),
    ]

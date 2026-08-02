from odoo import fields, models


class LogisticsFsaZone(models.Model):
    """Reference catalog of delivery-zone percentages (Zone 0-3), used as a
    convenience lookup when staff creates logistics.fsa.rate.adjustment rows
    with calc_type='percentage'. Deliberately NOT linked to any logistics.fsa
    record yet -- do not assign real FSAs to a zone until the authoritative
    FSA dataset is loaded (see CLAUDE.md blockers)."""

    _name = "logistics.fsa.zone"
    _description = "Delivery-zone percentage reference catalog"
    _order = "sequence"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    percentage = fields.Float(required=True, default=0.0, help="e.g. 5.0 = +5%.")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Zone code must be unique."),
    ]

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

TEMPERATURE_MODE_SELECTION = [
    ("dry", "Dry"),
    ("reefer", "Reefer"),
]

# 'both' is no longer a creatable value — migration 18.0.4.6.0 (and 18.0.4.5.0
# before it) split every 'both' offering into explicit ltl/ftl rows. Load
# type must always be exactly one of these two going forward (brief §7).
SHIPMENT_TYPE_SELECTION = [
    ("ltl", "LTL"),
    ("ftl", "FTL"),
]


class LogisticsServiceOffering(models.Model):
    _name = "logistics.service.offering"
    _description = "One bookable service on one lane (e.g. R1->R7 Next Day)"
    _order = "lane_id, service_level_id"

    lane_id = fields.Many2one("logistics.lane", required=True, index=True, ondelete="cascade")
    service_level_id = fields.Many2one("logistics.service.level", required=True, index=True)
    active = fields.Boolean(default=True)
    # Temperature does NOT participate in offering identity/naming/pricing —
    # Dry and Reefer share one offering and one price (brief §5/§6/§7).
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, default="dry", required=True)
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, default="ltl", required=True)
    name = fields.Char(compute="_compute_name", store=True)

    # NOTE: no _sql_constraints unique() here — a plain unique() would block
    # archiving-and-recreating history (Odoo's `active` column doesn't
    # participate in a plain unique index). Active-only uniqueness is
    # enforced below in Python AND by a partial unique index created in
    # migration 18.0.4.6.0 (belt-and-suspenders against races).
    @api.constrains("lane_id", "service_level_id", "shipment_type", "active")
    def _check_active_uniqueness(self):
        for rec in self:
            if not rec.active:
                continue
            dupe = self.search([
                ("id", "!=", rec.id),
                ("lane_id", "=", rec.lane_id.id),
                ("service_level_id", "=", rec.service_level_id.id),
                ("shipment_type", "=", rec.shipment_type),
                ("active", "=", True),
            ], limit=1)
            if dupe:
                raise ValidationError(_(
                    "An active offering for this lane, service level and load "
                    "type already exists (ID %s)."
                ) % dupe.id)

    @api.depends("lane_id.name", "service_level_id.name", "shipment_type")
    def _compute_name(self):
        for rec in self:
            st = dict(SHIPMENT_TYPE_SELECTION).get(rec.shipment_type, rec.shipment_type)
            rec.name = f"{rec.lane_id.name or '?'} — {rec.service_level_id.name or '?'} — {st}"

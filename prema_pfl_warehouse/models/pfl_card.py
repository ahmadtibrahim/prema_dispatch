# -*- coding: utf-8 -*-
"""The off-pallet identification card.

Some customers forbid a label on their pallet — a mark on the packaging is a
mark on their product. The instruction "don't label it" must not degrade into
"don't identify it": a pallet that cannot carry a label still has to be
findable, still has to have every movement recorded, and must never be located
by a photograph or by someone's memory of where they put it.

So the identity moves off the pallet: a card stands next to it, and the card
claims an *exclusive* position. Exclusive is the load-bearing word — if two
pallets could claim one position, the position would stop identifying anything.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PremaPflCard(models.Model):
    _name = "prema.pfl.card"
    _description = "PF Logistics Off-Pallet Identification Card"
    _order = "name"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Card Reference", required=True, copy=False,
        default=lambda self: _("New"))
    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet", required=True,
        ondelete="restrict", index=True)
    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", string="Warehouse Job",
        related="package_id.pfl_job_id", store=True, index=True)
    position_location_id = fields.Many2one(
        "stock.location", string="Position", required=True,
        ondelete="restrict", index=True,
        help="The physical position this card claims. One card per position — "
             "the position is what identifies the pallet when no label may be "
             "attached to it.")
    note = fields.Text()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("pfl_card_package_uniq", "unique(package_id)",
         "This pallet already has an identification card."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "prema.pfl.card") or _("New")
        return super().create(vals_list)

    @api.constrains("position_location_id", "package_id", "active")
    def _check_exclusive_position(self):
        """A position identifies one pallet, so only one live card may hold it.

        A released pallet no longer occupies anything, so its card stops
        blocking the position — which is what lets a position be reused the
        next day without anyone having to remember to tidy up.
        """
        for card in self:
            if not card.active or not card.position_location_id:
                continue
            rivals = self.search([
                ("id", "!=", card.id),
                ("position_location_id", "=", card.position_location_id.id),
                ("active", "=", True),
                ("package_id.pfl_state", "!=", "released"),
            ])
            if rivals:
                raise UserError(_(
                    "Position %(position)s is already identifying pallet "
                    "%(pallet)s (card %(card)s). A position can only identify "
                    "one pallet at a time.",
                    position=card.position_location_id.display_name,
                    pallet=rivals[0].package_id.name,
                    card=rivals[0].name))

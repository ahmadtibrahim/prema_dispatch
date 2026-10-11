# -*- coding: utf-8 -*-
"""Show pickup authorization to staff on the linked native outbound transfer,
and make the native Validate path honour it.

Additive except for one guard: a picking that carries pickup authorizations
(i.e. a PFL release created for a service booking) cannot be validated until at
least one of them is authorized AND verified. Everything else about Inventory
is untouched — ordinary receipts, deliveries and internal transfers have no
linked collection and are never inspected.

The guard deliberately checks *authorization + verification*, never "the
collection is recorded". Recording the collection requires the release to be
validated, so requiring it here would deadlock the dock.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    pickup_collection_ids = fields.One2many(
        "logistics.pickup.collection", "picking_id",
        string="Pickup Authorizations")
    pickup_collection_count = fields.Integer(
        compute="_compute_pickup_collection_count")
    pickup_authorization_state = fields.Selection(
        [("none", "No collection record"),
         ("pending", "Authorization pending"),
         ("verified", "Authorized and verified")],
        compute="_compute_pickup_authorization_state")

    @api.depends("pickup_collection_ids")
    def _compute_pickup_collection_count(self):
        for picking in self:
            picking.pickup_collection_count = len(picking.pickup_collection_ids)

    @api.depends("pickup_collection_ids.has_authorized_party",
                 "pickup_collection_ids.verification_result",
                 "pickup_collection_ids.verification_method",
                 "pickup_collection_ids.override_reason",
                 "pickup_collection_ids.override_user_id")
    def _compute_pickup_authorization_state(self):
        for picking in self:
            collections = picking.pickup_collection_ids
            if not collections:
                picking.pickup_authorization_state = "none"
            elif any(c.is_authorized_and_verified() for c in collections):
                picking.pickup_authorization_state = "verified"
            else:
                picking.pickup_authorization_state = "pending"

    def action_view_pickup_collections(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pickup Authorization",
            "res_model": "logistics.pickup.collection",
            "view_mode": "list,form",
            "domain": [("picking_id", "=", self.id)],
            "context": {"default_picking_id": self.id},
        }

    # ── the native Validate path ───────────────────────────────────────
    def _check_pickup_authorization(self):
        for picking in self:
            collections = picking.pickup_collection_ids
            if not collections:
                continue                      # not a pickup release
            if any(collection.is_authorized_and_verified()
                   for collection in collections):
                continue
            if not any(collection.has_authorized_party for collection in collections):
                raise UserError(_(
                    "%s is a customer pickup release, but no pickup "
                    "authorization is on file for booking %s. The customer must "
                    "name the company allowed to collect before the pallets "
                    "leave.", picking.display_name, picking.origin or "—"))
            raise UserError(_(
                "%s still needs the dock verification recorded: state how the "
                "collecting driver's authority was verified (or record a manager "
                "override with a reason) before validating the release.",
                picking.display_name))

    def button_validate(self):
        self._check_pickup_authorization()
        return super().button_validate()

    def _action_done(self):
        # The single choke point where stock actually moves. button_validate
        # already checked; this closes every other caller of the same path.
        self.filtered(lambda p: p.state != "done")._check_pickup_authorization()
        return super()._action_done()

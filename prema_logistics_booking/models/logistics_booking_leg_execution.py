# ════════════════════════════════════════════════════════════════════
# Phases 11-16 — Leg EXECUTION layer.
#   CUSTOMER SHIPMENT != OPERATIONAL EXECUTION (RULE 1).
#   Each physical section is an OPERATIONAL LEG (RULE 2) with its own
#   execution mode, carrier, buy cost. The frozen customer leg price
#   (frozen_leg_price) is NEVER touched by execution changes.
#   SELL (customer price) and BUY (carrier cost) are separate authorities
#   (RULE 3) — carrier payout is never derived from customer price.
# ════════════════════════════════════════════════════════════════════
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class LogisticsBookingLegExecution(models.Model):
    _inherit = "logistics.booking.leg"

    # ── Phase 12.1 — execution fields ───────────────────────────────
    execution_mode = fields.Selection([
        ("own_fleet", "Own Fleet"),
        ("subcontracted", "Subcontracted"),
        ("unassigned", "Unassigned / Requires Dispatch"),
    ], string="Execution Mode", default="unassigned", tracking=True,
       help="How this physical section is executed. Never changes the "
            "frozen customer leg price.")
    executing_carrier_id = fields.Many2one(
        "res.partner", string="Executing Carrier", ondelete="restrict",
        index=True, tracking=True,
        domain="[('is_transport_carrier', '=', True)]")
    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Vehicle", index=True, tracking=True)
    driver_id = fields.Many2one(
        "res.partner", string="Driver", index=True,
        help="Own-fleet driver. Subcontracted legs NEVER require a "
             "PREMAFIRM driver or truck.")
    estimated_leg_cost = fields.Float(
        string="Estimated Leg Cost", digits=(12, 2), readonly=True,
        help="Own-fleet estimate or subcontract estimate at scenario "
             "generation time. Never rewritten after acceptance.")
    accepted_buy_rate = fields.Float(
        string="Accepted Buy Rate", digits=(12, 2), tracking=True,
        help="The agreed carrier buy rate (what PREMAFIRM pays). BUY — "
             "never derived from the customer SELL price.")
    actual_leg_cost = fields.Float(
        string="Actual Leg Cost", digits=(12, 2), readonly=True,
        help="Actual approved vendor-bill transportation cost once the "
             "carrier bill is reviewed.")
    cost_source = fields.Selection([
        ("own_cost_estimate", "Own Cost Estimate"),
        ("carrier_rate_card", "Carrier Rate Card"),
        ("carrier_quote", "Carrier Quote"),
        ("carrier_accepted", "Carrier Accepted"),
        ("vendor_bill", "Vendor Bill"),
        ("manual", "Manual"),
    ], string="Cost Source", default="own_cost_estimate")
    purchase_order_id = fields.Many2one(
        "purchase.order", string="Rate Confirmation / PO", readonly=True,
        ondelete="restrict", index=True,
        help="Native Odoo Purchase Order representing the agreed carrier "
             "buy cost — the Carrier Rate Confirmation is a presentation "
             "of this PO, never a parallel financial model.")
    carrier_offer_state = fields.Selection([
        ("draft", "Draft"),
        ("availability_requested", "Availability Requested"),
        ("negotiating", "Negotiating"),
        ("accepted", "Accepted"),
        ("declined", "Declined"),
        ("no_response", "No Response"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ], string="Carrier Offer State", readonly=True)

    # ── Phase 15 — external execution / POD / vendor bill ───────────
    execution_status = fields.Selection([
        ("assigned", "Assigned"),
        ("confirmed", "Confirmed"),
        ("picked_up", "Picked Up"),
        ("in_transit", "In Transit"),
        ("at_hub", "At Hub"),
        ("delivered", "Delivered"),
        ("exception", "Exception"),
    ], string="Carrier Status", default="assigned", tracking=True,
       help="External-carrier execution status — dispatcher-entered, no "
            "carrier mobile app required.")
    pod_required = fields.Boolean(string="POD Required", default=True)
    pod_received = fields.Boolean(string="POD Received", tracking=True)
    pod_attachment_ids = fields.Many2many(
        "ir.attachment", string="Carrier POD",
        help="POD intake by email / WhatsApp / manual upload — the "
             "existing attachment infrastructure is the authority.")
    carrier_invoice_received = fields.Boolean(
        string="Carrier Invoice Received", tracking=True)
    carrier_invoice_variance = fields.Float(
        string="Carrier Invoice Variance", digits=(12, 2), readonly=True,
        help="Vendor bill vs accepted PO rate. Flagged for review — the "
             "accepted buy rate is never silently rewritten.")
    carrier_detention_amount = fields.Float(
        string="Carrier Detention Cost", digits=(12, 2),
        help="BUY-side detention payable to the carrier — an independent "
             "authority from customer SELL detention (Phase 10).")
    custody_event = fields.Selection([
        ("transferred", "Transferred"),
        ("cross_docked", "Cross-Docked"),
        ("reloaded", "Reloaded"),
    ], string="Custody Event",
       help="Pallet identity is preserved across legs — a subcontracted "
            "transfer never creates another customer pallet.")
    connection_exception = fields.Boolean(
        string="Connection Exception", tracking=True,
        help="Freight at the hub missed its onward departure — dispatcher "
             "action required. Custody is preserved; never marked "
             "delivered, never silently reassigned.")
    hub_transfer_cost = fields.Float(
        string="Hub / Transfer Cost", digits=(12, 2), readonly=True)

    # ── Effective cost helpers (estimated vs actual, kept distinct) ──
    def _effective_leg_cost(self):
        """Actual first, then accepted buy rate, then the estimate."""
        return (self.actual_leg_cost or self.accepted_buy_rate
                or self.estimated_leg_cost or 0.0)

    def _create_freight_purchase_order(self, offer, rate, carrier=None):
        """Native purchase RFQ for an accepted offer (Phase 14)."""
        self.ensure_one()
        return self.env["purchase.order"].sudo()._create_freight_po_from_leg(
            self, offer, rate, carrier=carrier)

    def action_record_carrier_status(self, status):
        for leg in self:
            if status == "delivered":
                leg.write({"execution_status": "delivered"})
            else:
                leg.write({"execution_status": status})

    def action_attach_carrier_pod(self, attachment):
        for leg in self:
            leg.write({
                "pod_attachment_ids": [(4, attachment.id)],
                "pod_received": True,
            })

    def action_mark_connection_exception(self):
        for leg in self:
            leg.write({"connection_exception": True,
                       "execution_status": "exception"})

    def action_clear_connection_exception(self):
        for leg in self:
            leg.write({"connection_exception": False,
                       "execution_status": "assigned"})

    def action_recommend_next_departure(self):
        """After a missed connection, recommend the next valid onward
        departure (existing authority). Never silently moves freight —
        the dispatcher decides."""
        self.ensure_one()
        if not self.connection_exception:
            return False
        from ..services.shipment_routing_service import ShipmentRoutingService
        svc = ShipmentRoutingService(self.env)
        if self.departure_id:
            next_dep = svc._next_departure_after(
                self.departure_id.departure_date,
                self.destination_region_id)
            return next_dep.id if next_dep else False
        return False

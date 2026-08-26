# ════════════════════════════════════════════════════════════════════
# Phase 13.3-13.6 — Carrier Offer / Negotiation.
#   A carrier recommendation is NOT acceptance. The system walks
#   Candidate → Availability Requested → Negotiating → Accepted /
#   Declined / No Response / Expired. Full offer history is preserved;
#   at most ONE offer is the accepted execution authority per leg.
#   This is an OPERATIONAL model — never an accounting model.
# ════════════════════════════════════════════════════════════════════
import logging

from odoo import _, api, exceptions, fields, models

_logger = logging.getLogger(__name__)


class LogisticsBookingLegCarrierOffer(models.Model):
    _name = "logistics.booking.leg.carrier.offer"
    _description = "Carrier Offer / Negotiation"
    _order = "booking_leg_id, id"

    name = fields.Char(string="Offer #", compute="_compute_name", store=True)
    booking_leg_id = fields.Many2one(
        "logistics.booking.leg", string="Booking Leg", required=True,
        ondelete="cascade", index=True)
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking",
        related="booking_leg_id.booking_id", store=True, index=True)
    carrier_id = fields.Many2one(
        "res.partner", string="Carrier", required=True, ondelete="restrict",
        index=True, domain="[('is_transport_carrier', '=', True)]")
    requested_at = fields.Datetime(string="Requested At", readonly=True)
    requested_by = fields.Many2one(
        "res.users", string="Requested By", readonly=True)
    target_buy_rate = fields.Float(
        string="Target Buy Rate", digits=(12, 2),
        help="What PREMAFIRM aims to pay for this leg (BUY).")
    carrier_counter_rate = fields.Float(
        string="Carrier Counter Rate", digits=(12, 2))
    agreed_rate = fields.Float(string="Agreed Rate", digits=(12, 2))
    currency_id = fields.Many2one(
        "res.currency", readonly=True,
        default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ("draft", "Draft"),
        ("availability_requested", "Availability Requested"),
        ("negotiating", "Negotiating"),
        ("accepted", "Accepted"),
        ("declined", "Declined"),
        ("no_response", "No Response"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ], string="State", default="draft", required=True, tracking=True)
    response_at = fields.Datetime(string="Response At", readonly=True)
    notes = fields.Text(string="Notes")
    expiry = fields.Datetime(string="Offer Expiry")
    acceptance_method = fields.Selection([
        ("phone", "Phone"),
        ("email", "Email"),
        ("signed", "Signed Rate Confirmation"),
        ("portal", "Portal"),
    ], string="Acceptance Method")
    accepted_at = fields.Datetime(string="Accepted At", readonly=True)
    recorded_by = fields.Many2one(
        "res.users", string="Recorded By", readonly=True)

    @api.depends("booking_leg_id", "carrier_id")
    def _compute_name(self):
        for offer in self:
            offer.name = "OFFER/%s-%s" % (
                offer.booking_leg_id.id, offer.carrier_id.id)

    # ── State transitions ───────────────────────────────────────────

    def action_request_availability(self):
        for offer in self:
            if offer.state not in ("draft", "cancelled", "expired"):
                raise exceptions.UserError(
                    _("Offer %s is already %s.") % (offer.name, offer.state))
            offer.write({
                "state": "availability_requested",
                "requested_at": fields.Datetime.now(),
                "requested_by": self.env.user.id,
            })

    def action_negotiate(self, counter_rate=None):
        for offer in self:
            if offer.state == "accepted":
                raise exceptions.UserError(
                    _("Offer %s is already accepted.") % offer.name)
            vals = {"state": "negotiating"}
            if counter_rate:
                vals["carrier_counter_rate"] = counter_rate
            offer.write(vals)

    def action_decline(self):
        self.write({"state": "declined",
                    "response_at": fields.Datetime.now()})
        # 16.8 — Carrier decline fallback: any CHOSEN scenario that
        # depended on this carrier is un-chosen and the booking falls
        # back to requiring a fresh decision. The engine never silently
        # re-subscribes another carrier — the dispatcher re-chooses.
        for offer in self:
            if not offer.booking_id:
                continue
            for sc in offer.booking_id.execution_scenario_ids.filtered(
                    lambda s: s.chosen):
                plan = sc.execution_plan or []
                if any(isinstance(p, dict)
                       and p.get("carrier_id") == offer.carrier_id.id
                       for p in plan):
                    sc.write({"chosen": False})
                    _logger.info(
                        "Offer %s declined → scenario %s unchosen for "
                        "booking %s", offer.name, sc.id,
                        offer.booking_id.id)
            if offer.booking_id.execution_scenario_id:
                offer.booking_id.write({"execution_scenario_id": False})

    def action_no_response(self):
        self.write({"state": "no_response",
                    "response_at": fields.Datetime.now()})

    def action_expire(self):
        self.write({"state": "expired"})

    def action_cancel(self):
        self.write({"state": "cancelled"})

    def action_accept(self):
        """Acceptance consequence (13.6): one accepted authority per leg,
        leg execution fields set, buy rate fixed, native freight PO
        created, booking margin recalculated. Nothing is sent — sending
        is a separate explicit action."""
        for offer in self:
            if offer.state == "accepted":
                raise exceptions.UserError(
                    _("Offer %s is already accepted.") % offer.name)
            rate = offer.agreed_rate or offer.carrier_counter_rate \
                or offer.target_buy_rate
            if not rate:
                raise exceptions.UserError(
                    _("Set an Agreed Rate before accepting offer %s.")
                    % offer.name)
            leg = offer.booking_leg_id
            # Only ONE accepted offer may be the leg's execution authority.
            other = self.search([
                ("booking_leg_id", "=", leg.id),
                ("state", "=", "accepted"),
            ])
            if other and other.id != offer.id:
                raise exceptions.UserError(
                    _("Leg %s already has an accepted offer (%s). Decline "
                      "or cancel it before accepting another.")
                    % (leg.id, other.name))
            now = fields.Datetime.now()
            offer.write({
                "state": "accepted",
                "agreed_rate": rate,
                "response_at": now,
                "accepted_at": now,
                "recorded_by": self.env.user.id,
            })
            leg.write({
                "execution_mode": "subcontracted",
                "executing_carrier_id": offer.carrier_id.id,
                "accepted_buy_rate": rate,
                "cost_source": "carrier_accepted",
                "carrier_offer_state": "accepted",
                "execution_status": "confirmed",
            })
            # Native Purchase RFQ (Phase 14) — never a parallel model.
            po = leg._create_freight_purchase_order(offer, rate)
            if po:
                leg.write({"purchase_order_id": po.id})
                _logger.info(
                    "Leg %s accepted offer %s → freight PO %s (id %s)",
                    leg.id, offer.name, po.name, po.id)
            booking = leg.booking_id
            if booking and hasattr(booking, "action_recompute_margin"):
                booking.action_recompute_margin()
        return True

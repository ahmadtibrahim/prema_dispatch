from odoo import fields, models

from ..services.pricing_service import PricingService

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("chilled", "Chilled"), ("frozen", "Frozen")]


class LogisticsRateSimulator(models.TransientModel):
    """Internal staff tool. Calls the exact same PricingService the customer
    portal and booking confirmation use -- no parallel calculator."""

    _name = "logistics.rate.simulator"
    _description = "Rate Simulator"

    pickup_postal_code = fields.Char(required=True)
    delivery_postal_code = fields.Char(required=True)
    pallets = fields.Integer(default=1, required=True)
    weight_lbs = fields.Float()
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, default="ltl", required=True)
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, default="dry", required=True)
    liftgate_pickup = fields.Boolean()
    liftgate_delivery = fields.Boolean()
    appointment = fields.Boolean()
    residential = fields.Boolean()
    same_day_requested = fields.Boolean()
    partner_id = fields.Many2one("res.partner")

    result_text = fields.Text(readonly=True)

    def action_simulate(self):
        self.ensure_one()
        Fsa = self.env["logistics.fsa"]
        pickup_fsa = Fsa.resolve_from_postal(self.pickup_postal_code)
        delivery_fsa = Fsa.resolve_from_postal(self.delivery_postal_code)
        lines = []

        if not pickup_fsa:
            lines.append(f"Pickup FSA not found/invalid for '{self.pickup_postal_code}'.")
        else:
            lines.append(f"Pickup FSA: {pickup_fsa.fsa} ({pickup_fsa.display_city or '?'}) "
                         f"-> Region {pickup_fsa.region_id.display_name if pickup_fsa.region_id else 'UNMAPPED'}")
        if not delivery_fsa:
            lines.append(f"Delivery FSA not found/invalid for '{self.delivery_postal_code}'.")
        else:
            lines.append(f"Delivery FSA: {delivery_fsa.fsa} ({delivery_fsa.display_city or '?'}) "
                         f"-> Region {delivery_fsa.region_id.display_name if delivery_fsa.region_id else 'UNMAPPED'}")

        if pickup_fsa and delivery_fsa:
            result = PricingService(self.env).calculate(
                pickup_fsa, delivery_fsa, self.shipment_type, self.temperature_mode,
                self.pallets, self.weight_lbs, self.liftgate_pickup, self.liftgate_delivery,
                self.appointment, self.residential, self.same_day_requested,
                partner=self.partner_id or None,
            )
            if not result.available:
                lines.append(f"\nNOT AVAILABLE -- reason: {result.reason}")
            else:
                lines.append(f"\nLane: {result.lane.name}")
                lines.append(f"Service Offering: {result.service_offering.name}")
                lines.append(f"Rate Plan: {result.rate_plan.name}")
                lines.append(f"Next Pickup: {result.pickup_date}")
                lines.append(f"Estimated Delivery: {result.delivery_date_estimate}")
                lines.append("\nPrice Breakdown:")
                for line in result.price_lines:
                    lines.append(f"  {line['label']:<35s} {line['amount']:>10.2f}")

        self.result_text = "\n".join(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.rate.simulator",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

"""Test wizard for the Direct Delivery Matrix."""

from odoo import _, api, fields, models


class LogisticsTestRoutingDecision(models.TransientModel):
    _name = "logistics.test.routing.decision"
    _description = "Test Routing Decision"

    origin_region_id = fields.Many2one(
        "logistics.region", string="Origin Region", required=True,
        domain="[('is_official_ltl_region','=',True)]",
    )
    destination_region_id = fields.Many2one(
        "logistics.region", string="Destination Region", required=True,
        domain="[('is_official_ltl_region','=',True)]",
    )
    pickup_day = fields.Selection([
        ("monday", "Monday"), ("tuesday", "Tuesday"),
        ("wednesday", "Wednesday"), ("thursday", "Thursday"),
        ("friday", "Friday"), ("saturday", "Saturday"),
    ], string="Pickup Day")
    pickup_time = fields.Float(string="Pickup Time (hrs)", help="e.g. 8.5 = 8:30 AM")
    road_distance_km = fields.Float(string="Road Distance (km)")

    # Results
    decision = fields.Char(string="Decision", readonly=True)
    reason_text = fields.Char(string="Reason", readonly=True)
    reason_code = fields.Char(string="Reason Code", readonly=True)
    direct_allowed = fields.Boolean(string="Direct Allowed", readonly=True)
    hub_transfer_required = fields.Boolean(string="Hub Transfer", readonly=True)

    def action_test(self):
        """Run the DirectDeliveryService and display results."""
        from ..services.direct_delivery_service import DirectDeliveryService

        svc = DirectDeliveryService(self.env)
        result = svc.decide(
            origin_region_id=self.origin_region_id.id,
            destination_region_id=self.destination_region_id.id,
            pickup_day=self.pickup_day,
            road_distance_km=self.road_distance_km or None,
            pickup_time=self.pickup_time or None,
        )

        self.write({
            "decision": result.decision,
            "reason_text": result.reason_text,
            "reason_code": result.reason_code,
            "direct_allowed": result.direct_allowed,
            "hub_transfer_required": result.hub_transfer_required,
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": result.decision,
                "message": result.reason_text,
                "type": "success" if result.decision == "DIRECT_ALLOWED" else "warning",
                "sticky": True,
            },
        }

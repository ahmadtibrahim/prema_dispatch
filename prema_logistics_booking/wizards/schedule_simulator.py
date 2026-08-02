from odoo import fields, models

from ..services.schedule_service import ScheduleService

SHIPMENT_TYPE_SELECTION = [("ltl", "LTL"), ("ftl", "FTL")]
TEMPERATURE_MODE_SELECTION = [("dry", "Dry"), ("chilled", "Chilled"), ("frozen", "Frozen")]


class LogisticsScheduleSimulator(models.TransientModel):
    """Lightweight staff tool: 'what happens if a customer books this lane
    at this exact date/time?' -- exercises the same ScheduleService the
    pricing engine uses."""

    _name = "logistics.schedule.simulator"
    _description = "Schedule Simulator"

    pickup_postal_code = fields.Char(required=True)
    delivery_postal_code = fields.Char(required=True)
    temperature_mode = fields.Selection(TEMPERATURE_MODE_SELECTION, default="dry", required=True)
    shipment_type = fields.Selection(SHIPMENT_TYPE_SELECTION, default="ltl", required=True)
    reference_datetime = fields.Datetime(default=fields.Datetime.now, required=True)

    result_text = fields.Text(readonly=True)

    def action_simulate(self):
        self.ensure_one()
        Fsa = self.env["logistics.fsa"]
        pickup_fsa = Fsa.resolve_from_postal(self.pickup_postal_code)
        delivery_fsa = Fsa.resolve_from_postal(self.delivery_postal_code)
        lines = []

        if not pickup_fsa or not delivery_fsa or not pickup_fsa.region_id or not delivery_fsa.region_id:
            lines.append("Could not resolve one or both FSAs to a region.")
        else:
            lane = self.env["logistics.lane"].search([
                ("origin_region_id", "=", pickup_fsa.region_id.id),
                ("destination_region_id", "=", delivery_fsa.region_id.id),
            ], limit=1)
            if not lane:
                lines.append(f"No lane configured: {pickup_fsa.region_id.code} -> {delivery_fsa.region_id.code}")
            else:
                offerings = self.env["logistics.service.offering"].search([
                    ("lane_id", "=", lane.id), ("active", "=", True),
                    ("temperature_mode", "=", self.temperature_mode),
                    "|", ("shipment_type", "=", self.shipment_type), ("shipment_type", "=", "both"),
                ])
                if self.temperature_mode in ("chilled", "frozen"):
                    offerings = offerings.filtered(lambda o: o.service_level_id.reefer_food_eligible)
                if not offerings:
                    reason = " (reefer same-day/next-day rule blocks longer-transit offerings)" \
                        if self.temperature_mode != "dry" else ""
                    lines.append(f"No matching service offering for this temperature/shipment type{reason}.")
                svc = ScheduleService(self.env)
                for offering in offerings:
                    result = svc.next_pickup_and_delivery(offering, self.reference_datetime)
                    lines.append(f"\nOffering: {offering.name}")
                    if not result.available:
                        lines.append(f"  NOT AVAILABLE -- {result.reason}")
                    else:
                        lines.append(f"  Cutoff: {result.schedule.cutoff_time}")
                        lines.append(f"  Next Pickup: {result.pickup_date}")
                        lines.append(f"  Estimated Delivery: {result.delivery_date}")

        self.result_text = "\n".join(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.schedule.simulator",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

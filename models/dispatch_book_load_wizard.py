from odoo import api, fields, models
from odoo.exceptions import UserError


class PremaDispatchBookLoadWizard(models.TransientModel):
    _name = "prema.dispatch.book.load.wizard"
    _description = "Book Dispatch Load"

    move_id = fields.Many2one("account.move", required=True, ondelete="cascade")
    partner_id = fields.Many2one("res.partner")
    service_type = fields.Selection([("local", "Local"), ("ltl", "LTL"), ("ftl", "FTL"), ("dedicated", "Dedicated"), ("other", "Other")], default="ltl")
    equipment_type = fields.Selection([("dry", "Dry Van"), ("reefer", "Reefer"), ("flatbed", "Flatbed"), ("other", "Other")], default="dry")
    requires_reefer = fields.Boolean()
    requires_liftgate = fields.Boolean()
    commodity = fields.Char()
    temperature_requirement = fields.Char()
    expected_skids = fields.Integer()
    total_weight_lbs = fields.Float()
    scheduled_pickup = fields.Datetime()
    pickup_window_type = fields.Selection([("flexible", "Flexible — Any Time"), ("window", "Time Window"), ("exact", "Exact Appointment")], default="flexible")
    pickup_earliest = fields.Datetime()
    pickup_latest = fields.Datetime()
    pickup_exact_time = fields.Datetime()
    route_definition_mode = fields.Selection([("exact_stops", "Exact Stops Known"), ("stops_pending", "Stops Pending")], default="exact_stops", required=True)
    planned_route_name = fields.Char()
    planned_route_corridor = fields.Selection([("EAST", "East"), ("WEST", "West"), ("NORTH", "North"), ("SOUTH", "South"), ("LOCAL", "Local / GTA"), ("CUSTOM", "Custom")])
    planned_delivery_area = fields.Char()
    pickup_saved_location_id = fields.Many2one("prema.dispatch.location")
    reserve_capacity = fields.Boolean()
    vehicle_id = fields.Many2one("fleet.vehicle")
    driver_id = fields.Many2one("res.partner", domain="[('x_is_driver','=',True)]")
    customer_reference = fields.Char()
    purchase_order = fields.Char()
    bol_reference = fields.Char(string="BOL / Reference")
    general_notes = fields.Text()

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        move = self.env["account.move"].browse(self.env.context.get("active_id") or vals.get("move_id"))
        if move.exists():
            vals.update({"move_id": move.id, "partner_id": move.partner_id.id, "customer_reference": move.ref or move.name, "purchase_order": getattr(move, "premafirm_po", "") or "", "bol_reference": getattr(move, "premafirm_bol", "") or "", "scheduled_pickup": move._resolve_scheduled_pickup()})
        return vals

    def action_confirm(self):
        self.ensure_one()
        move = self.move_id
        if move.dispatch_job_ids:
            job = move.dispatch_job_ids[:1]
            return {"type": "ir.actions.act_window", "name": "Dispatch Job", "res_model": "prema.dispatch.job", "res_id": job.id, "view_mode": "form"}
        if self.route_definition_mode == "stops_pending":
            if not self.pickup_saved_location_id:
                raise UserError("Pickup Saved Location is required for Stops Pending bookings.")
            if self.expected_skids <= 0:
                raise UserError("Expected skids must be greater than zero for Stops Pending bookings.")
            if not self.scheduled_pickup:
                raise UserError("Pickup date/time is required for Stops Pending bookings.")
            if not (self.planned_route_name or self.planned_route_corridor):
                raise UserError("Planned Route Name or Planned Corridor is required for Stops Pending bookings.")
        draft_stage = self.env["prema.dispatch.stage"].search([("stage_type", "=", "draft")], limit=1)
        job = self.env["prema.dispatch.job"].create({
            "invoice_id": move.id, "partner_id": self.partner_id.id, "ref": self.customer_reference or move.ref or move.name,
            "stage_id": draft_stage.id if draft_stage else False, "company_id": move.company_id.id, "dispatcher_id": self.env.uid,
            "source_model": "account.move", "source_res_id": move.id, "service_type": self.service_type, "equipment_type": self.equipment_type,
            "requires_reefer": self.requires_reefer, "requires_liftgate": self.requires_liftgate, "commodity": self.commodity,
            "temp_requirement": self.temperature_requirement, "approximate_skids": self.expected_skids, "scheduled_pickup": self.scheduled_pickup,
            "pickup_window_type": self.pickup_window_type, "pickup_earliest": self.pickup_earliest, "pickup_latest": self.pickup_latest,
            "pickup_exact_time": self.pickup_exact_time, "route_definition_mode": self.route_definition_mode,
            "stops_confirmation_state": "pending" if self.route_definition_mode == "stops_pending" else "confirmed",
            "planned_route_name": self.planned_route_name, "planned_route_corridor": self.planned_route_corridor,
            "planned_delivery_area": self.planned_delivery_area, "pickup_saved_location_id": self.pickup_saved_location_id.id,
            "reserve_capacity": self.reserve_capacity, "vehicle_id": self.vehicle_id.id, "driver_id": self.driver_id.id,
            "bol_number": self.bol_reference, "po_number": self.purchase_order, "internal_notes": self.general_notes,
        })
        if self.pickup_saved_location_id and not job.stop_ids.filtered(lambda s: s.stop_type == "pickup"):
            self.env["prema.dispatch.stop"].create({"job_id": job.id, "sequence": 10, "stop_type": "pickup", "scheduled_time": self.scheduled_pickup, "saved_location_id": self.pickup_saved_location_id.id, "pallets_in": self.expected_skids})
        if self.vehicle_id and self.scheduled_pickup and self.reserve_capacity:
            plan = self.env["prema.dispatch.load.plan"].get_or_create_for_vehicle_date(self.vehicle_id.id, self.scheduled_pickup.date().isoformat(), self.driver_id.id if self.driver_id else None)
            lp = self.env["prema.dispatch.load.plan"].browse(plan["id"] if isinstance(plan, dict) else plan.id)
            link = self.env["prema.dispatch.load.plan.job"].search([("load_plan_id", "=", lp.id), ("job_id", "=", job.id)], limit=1)
            if not link:
                link = self.env["prema.dispatch.load.plan.job"].create({"load_plan_id": lp.id, "job_id": job.id})
            link.write({"reserve_capacity": True, "reserved_floor_positions": self.expected_skids, "reservation_source": "booking_estimate"})
        return {"type": "ir.actions.act_window", "name": "Dispatch Job", "res_model": "prema.dispatch.job", "res_id": job.id, "view_mode": "form"}

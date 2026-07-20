"""
Feasibility Checker Wizard — "Can We Do This Today?"
Dispatcher enters job details; system returns Yes/No/Risky with ETAs.
"""
from odoo import api, fields, models


class DispatchFeasibilityWizard(models.TransientModel):
    _name = "prema.dispatch.feasibility.wizard"
    _description = "Dispatch Feasibility Check"

    job_id = fields.Many2one("prema.dispatch.job", string="Related Job")

    # Inputs
    pickup_address = fields.Char(string="Pickup Address", required=True)
    dropoff_address = fields.Char(string="Delivery Address", required=True)
    check_date = fields.Date(string="Date", default=fields.Date.today)

    pickup_earliest = fields.Datetime(string="Pickup Earliest")
    pickup_latest = fields.Datetime(string="Pickup Latest (Deadline)")
    delivery_deadline = fields.Datetime(string="Delivery Deadline")

    pallets = fields.Integer(string="Pallets")
    weight_lbs = fields.Float(string="Weight (lbs)", digits=(10, 1))
    requires_reefer = fields.Boolean(string="Reefer Required")
    requires_liftgate = fields.Boolean(string="Liftgate Required")
    service_time_pickup_min = fields.Integer(string="Service Time at Pickup (min)", default=20)
    service_time_delivery_min = fields.Integer(string="Service Time at Delivery (min)", default=15)

    # Results (populated after check)
    result_verdict = fields.Selection([
        ("feasible",     "✅ Yes — Feasible"),
        ("risky",        "⚠️ Risky"),
        ("not_feasible", "🚫 Not Feasible"),
    ], string="Verdict", readonly=True)
    result_reason = fields.Text(string="Risk / Block Reason", readonly=True)
    result_best_truck = fields.Char(string="Best Truck", readonly=True)
    result_pickup_eta = fields.Char(string="Pickup ETA", readonly=True)
    result_delivery_eta = fields.Char(string="Delivery ETA", readonly=True)
    result_buffer_minutes = fields.Integer(string="Buffer Before Deadline (min)", readonly=True)
    result_distance_to_pickup = fields.Float(string="Distance to Pickup (km)", digits=(10, 1), readonly=True)
    result_extra_distance = fields.Float(string="Extra Route Distance (km)", digits=(10, 1), readonly=True)
    result_summary = fields.Text(string="Full Summary", readonly=True)
    result_checked = fields.Boolean(readonly=True)

    def action_run_check(self):
        """Run the feasibility check and populate result fields."""
        self.ensure_one()

        from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService

        payload = {
            "pickup_address": self.pickup_address,
            "dropoff_address": self.dropoff_address,
            "check_date": self.check_date.isoformat() if self.check_date else None,
            "pickup_earliest": self.pickup_earliest,
            "pickup_latest": self.pickup_latest,
            "delivery_deadline": self.delivery_deadline,
            "pallets": self.pallets,
            "weight_lbs": self.weight_lbs,
            "requires_reefer": self.requires_reefer,
            "requires_liftgate": self.requires_liftgate,
            "service_time_pickup_min": self.service_time_pickup_min,
            "service_time_delivery_min": self.service_time_delivery_min,
        }

        svc = DispatchFeasibilityService(self.env)
        result = svc.check(payload)

        best = result.get("best") or {}
        options = result.get("options", [])

        # Build summary table
        lines = [f"{'TRUCK':<25} {'STATUS':<15} {'PICKUP ETA':<12} {'DELIVERY ETA':<14} {'BUFFER':>8}"]
        lines.append("-" * 80)
        for opt in options[:5]:
            status_icon = {"feasible": "✅", "risky": "⚠️", "not_feasible": "🚫"}.get(opt["verdict"], "")
            buf = f"{opt['deadline_buffer_minutes']} min" if opt.get("deadline_buffer_minutes") is not None else "—"
            lines.append(
                f"{opt['truck_name']:<25} {status_icon + opt['verdict']:<15} "
                f"{opt.get('pickup_eta','—'):<12} {opt.get('delivery_eta','—'):<14} {buf:>8}"
            )

        self.write({
            "result_verdict": result.get("verdict", "not_feasible"),
            "result_reason": result.get("reason", ""),
            "result_best_truck": best.get("truck_name", "—"),
            "result_pickup_eta": best.get("pickup_eta", "—"),
            "result_delivery_eta": best.get("delivery_eta", "—"),
            "result_buffer_minutes": best.get("deadline_buffer_minutes") or 0,
            "result_distance_to_pickup": best.get("distance_to_pickup_km") or 0,
            "result_extra_distance": best.get("extra_distance_km") or 0,
            "result_summary": "\n".join(lines),
            "result_checked": True,
        })

        # If linked to a job, update feasibility_status
        if self.job_id:
            verdict_map = {
                "feasible": "feasible",
                "risky": "risky",
                "not_feasible": "not_feasible",
            }
            self.job_id.write({
                "feasibility_status": verdict_map.get(result.get("verdict"), "unknown"),
                "feasibility_notes": result.get("reason", ""),
                "recommended_truck_id": best.get("truck_id") if best else False,
            })

        # Stay open to show results
        return {
            "type": "ir.actions.act_window",
            "res_model": "prema.dispatch.feasibility.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_assign_best_truck(self):
        """Assign the recommended truck to the linked job."""
        self.ensure_one()
        if not self.job_id or not self.result_checked:
            return
        if not self.result_best_truck or self.result_best_truck == "—":
            return

        from odoo.addons.prema_dispatch.services.feasibility_service import DispatchFeasibilityService
        payload = {
            "pickup_address": self.pickup_address,
            "dropoff_address": self.dropoff_address,
            "check_date": self.check_date.isoformat() if self.check_date else None,
            "requires_reefer": self.requires_reefer,
            "requires_liftgate": self.requires_liftgate,
            "pallets": self.pallets,
        }
        svc = DispatchFeasibilityService(self.env)
        result = svc.check(payload)
        best = result.get("best") or {}
        if best.get("truck_id"):
            self.job_id.write({"vehicle_id": best["truck_id"]})
        return {"type": "ir.actions.act_window_close"}

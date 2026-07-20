"""
Mid-Day Load Finder Wizard.

Dispatcher gets a call for a new pickup. This wizard shows which trucks can
take it right now based on GPS location, current schedule, and capacity.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DispatchAdhocWizard(models.TransientModel):
    _name = "prema.dispatch.adhoc.wizard"
    _description = "Find Available Truck — Mid-Day Load"

    # ── Input ─────────────────────────────────────────────────────

    pickup_address = fields.Char(string="Pickup Address", required=True)
    delivery_address = fields.Char(string="Delivery Address", required=True)
    pallets = fields.Integer(string="Pallets", default=0)
    requires_reefer = fields.Boolean(string="Reefer Required")
    requires_liftgate = fields.Boolean(string="Liftgate Required")
    pickup_by = fields.Char(
        string="Needed By",
        help="e.g. '2:00 PM today' or 'ASAP'. Used for display only.",
    )
    commodity = fields.Char(string="Commodity")

    # ── Results ───────────────────────────────────────────────────

    result_checked = fields.Boolean(readonly=True)
    recommendation = fields.Text(string="Recommendation", readonly=True)

    suitable_truck_ids = fields.One2many(
        "prema.dispatch.adhoc.result", "wizard_id",
        string="Available Trucks", readonly=True,
    )
    later_truck_ids = fields.One2many(
        "prema.dispatch.adhoc.result", "wizard_id",
        string="Available Later",
        domain=[("is_available_later", "=", True)],
        readonly=True,
    )

    # ── Actions ───────────────────────────────────────────────────

    def action_find_trucks(self):
        self.ensure_one()
        from odoo.addons.prema_dispatch.services.adhoc_load_service import AdhocLoadService

        svc = AdhocLoadService(self.env)
        result = svc.find_suitable_trucks(
            pickup_address=self.pickup_address,
            delivery_address=self.delivery_address,
            pallets=self.pallets or 0,
            requires_reefer=self.requires_reefer,
            requires_liftgate=self.requires_liftgate,
            pickup_by=self.pickup_by,
        )

        # Clear previous results
        self.env["prema.dispatch.adhoc.result"].search(
            [("wizard_id", "=", self.id)]
        ).unlink()

        # Write suitable trucks
        for rank, t in enumerate(result["suitable"], 1):
            equip = []
            if t["has_reefer"]:
                equip.append("Reefer")
            if t["has_liftgate"]:
                equip.append("Liftgate")

            gps_label = "No GPS"
            if t.get("gps_age_minutes") is not None:
                age = t["gps_age_minutes"]
                if age < 10:
                    gps_label = "Live GPS"
                elif age < 30:
                    gps_label = f"GPS {age}m ago"
                else:
                    gps_label = f"GPS stale ({age}m)"

            self.env["prema.dispatch.adhoc.result"].create({
                "wizard_id": self.id,
                "rank": rank,
                "truck_id": t["truck_id"],
                "driver_name": t["driver_name"] or "",
                "on_route": t["on_route"],
                "dist_to_pickup_km": t["dist_to_pickup_km"] or 0,
                "eta_to_pickup_min": t["eta_to_pickup_min"] or 0,
                "detour_km": t["detour_km"] or 0,
                "available_capacity": t["available_capacity"],
                "pallet_capacity": t["pallet_capacity"],
                "fit_description": t["fit_description"],
                "gps_status": gps_label,
                "equipment": ", ".join(equip) or "Dry Van",
                "is_available_later": False,
            })

        # Write available-later trucks
        for t in result["available_later"]:
            equip = []
            if t["has_reefer"]:
                equip.append("Reefer")
            if t["has_liftgate"]:
                equip.append("Liftgate")
            self.env["prema.dispatch.adhoc.result"].create({
                "wizard_id": self.id,
                "rank": 99,
                "truck_id": t["truck_id"],
                "driver_name": t["driver_name"] or "",
                "fit_description": t["reason"],
                "available_from": t["available_from"] or "",
                "equipment": ", ".join(equip) or "Dry Van",
                "is_available_later": True,
            })

        self.write({
            "result_checked": True,
            "recommendation": result["recommendation"],
        })

        return {
            "type": "ir.actions.act_window",
            "name": "Find Available Truck",
            "res_model": "prema.dispatch.adhoc.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_assign_to_job(self):
        """Open new dispatch job with the best truck pre-filled."""
        self.ensure_one()
        best = self.suitable_truck_ids.filtered(lambda r: not r.is_available_later)[:1]
        ctx = {
            "default_pickup_city": self.pickup_address,
            "default_commodity": self.commodity or "",
            "default_requires_reefer": self.requires_reefer,
            "default_requires_liftgate": self.requires_liftgate,
            "default_approximate_skids": self.pallets or 0,
        }
        if best and best.truck_id:
            ctx["default_vehicle_id"] = best.truck_id.id
        return {
            "type": "ir.actions.act_window",
            "name": "New Dispatch Job",
            "res_model": "prema.dispatch.job",
            "view_mode": "form",
            "context": ctx,
        }


class DispatchAdhocResult(models.TransientModel):
    _name = "prema.dispatch.adhoc.result"
    _description = "Adhoc Load Finder Result Row"
    _order = "rank asc, dist_to_pickup_km asc"

    wizard_id = fields.Many2one("prema.dispatch.adhoc.wizard", ondelete="cascade")
    is_available_later = fields.Boolean(default=False)

    rank = fields.Integer(default=99)
    truck_id = fields.Many2one("fleet.vehicle", string="Truck", readonly=True)
    driver_name = fields.Char(string="Driver", readonly=True)
    on_route = fields.Boolean(string="On Route", readonly=True)
    dist_to_pickup_km = fields.Float(string="Dist to Pickup (km)", readonly=True, digits=(6, 1))
    eta_to_pickup_min = fields.Integer(string="ETA (min)", readonly=True)
    detour_km = fields.Float(string="Detour (km)", readonly=True, digits=(6, 1))
    available_capacity = fields.Integer(string="Avail. Pallets", readonly=True)
    pallet_capacity = fields.Integer(string="Max Pallets", readonly=True)
    fit_description = fields.Char(string="Assessment", readonly=True)
    gps_status = fields.Char(string="GPS", readonly=True)
    equipment = fields.Char(string="Equipment", readonly=True)
    available_from = fields.Char(string="Available From", readonly=True)

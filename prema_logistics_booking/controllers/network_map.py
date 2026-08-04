"""Network Map and Price Matrix — owner-friendly pricing visualization."""
from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden


def _require_dispatch_staff():
    user = request.env.user
    if not user.has_group("prema_dispatch.group_dispatcher") and \
       not user.has_group("prema_dispatch.group_dispatch_manager"):
        raise Forbidden()


class LogisticsNetworkMap(http.Controller):

    @http.route("/logistics/network-map", type="http", auth="user", website=False)
    def network_map(self, **kwargs):
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        Vehicle = request.env["fleet.vehicle"].sudo()
        RatePlan = request.env["logistics.rate.plan"].sudo()

        ops_vehicles = Vehicle.search([("active","=",True),("x_operational_logistics","=",True)])
        phase = min(len(ops_vehicles), 4)
        truck_names = [v.name or v.license_plate for v in ops_vehicles]

        regions = []
        for r in Region.search([("active","=",True)], order="code"):
            lanes_out = Lane.search_count([("origin_region_id","=",r.id),("active","=",True)])
            regions.append({
                "code": r.code, "name": r.name, "hub": r.hub_name or "",
                "lanes_out": lanes_out, "active": lanes_out > 0,
            })

        return request.render("prema_logistics_booking.logistics_network_map_page", {
            "phase": phase, "truck_count": len(ops_vehicles), "trucks": truck_names,
            "regions": regions,
        })

    @http.route("/logistics/price-matrix", type="http", auth="user", website=False)
    def price_matrix(self, **kwargs):
        _require_dispatch_staff()
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        RatePlan = request.env["logistics.rate.plan"].sudo()
        Schedule = request.env["logistics.lane.schedule"].sudo()
        Vehicle = request.env["fleet.vehicle"].sudo()

        ops_vehicles = Vehicle.search([("active","=",True),("x_operational_logistics","=",True)])
        phase = min(len(ops_vehicles), 4)

        region_codes = Region.search([("active","=",True)], order="code").mapped("code")

        matrix = {}
        for orig in region_codes:
            matrix[orig] = {}
            o = Region.search([("code","=",orig)],limit=1)
            for dest in region_codes:
                d = Region.search([("code","=",dest)],limit=1)
                lane = Lane.search([("origin_region_id","=",o.id),("destination_region_id","=",d.id)],limit=1)
                if lane and lane.active:
                    plan = RatePlan.search([("service_offering_id.lane_id","=",lane.id)],limit=1)
                    sched = Schedule.search([("service_offering_id.lane_id","=",lane.id)],limit=1)
                    day = ""
                    if sched:
                        days = []
                        for i, f in enumerate(["pickup_monday","pickup_tuesday","pickup_wednesday","pickup_thursday","pickup_friday"]):
                            if sched[f]: days.append(["Mon","Tue","Wed","Thu","Fri"][i])
                        day = "/".join(days) if days else ""
                    matrix[orig][dest] = {
                        "target": lane.revenue_target or 0,
                        "active": True,
                        "phase_ok": True,
                        "schedule_day": day,
                    }
                elif lane:
                    matrix[orig][dest] = {
                        "target": lane.revenue_target or 0,
                        "active": False,
                        "phase_ok": False,
                        "schedule_day": "",
                    }

        return request.render("prema_logistics_booking.logistics_price_matrix_page", {
            "phase": phase, "truck_count": len(ops_vehicles),
            "region_codes": region_codes, "matrix": matrix,
        })

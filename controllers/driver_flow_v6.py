from odoo import http
from odoo.http import request


class DriverFlowV6Controller(http.Controller):
    """Small workflow endpoints used by the guided Driver App state machine.

    Return-to-base intentionally uses configuration/company data instead of a
    new database model so upgrading this workflow is non-destructive.  Ops may
    set the following ir.config_parameter values when the terminal pin needs to
    differ from the company address/geocode:

      prema_dispatch.home_base_name
      prema_dispatch.home_base_address
      prema_dispatch.home_base_lat
      prema_dispatch.home_base_lng
      prema_dispatch.home_base_radius_m
    """

    @http.route("/dispatch/driver/work/base", type="json", auth="user", methods=["POST"])
    def driver_work_base(self, **kwargs):
        user = request.env.user
        allowed_groups = [
            "prema_dispatch.group_dispatch_driver",
            "prema_dispatch.group_dispatch_manager",
            "prema_dispatch.group_dispatcher",
            "base.group_system",
        ]
        if not any(user.has_group(group) for group in allowed_groups):
            return {"success": False, "error": "Not authorized"}

        params = request.env["ir.config_parameter"].sudo()
        company = request.env.company.sudo()
        partner = company.partner_id

        def _float_param(key, fallback=0.0):
            raw = params.get_param(key, "")
            if raw not in (None, ""):
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
            return float(fallback or 0.0)

        partner_lat = partner.partner_latitude if "partner_latitude" in partner._fields else 0.0
        partner_lng = partner.partner_longitude if "partner_longitude" in partner._fields else 0.0
        address = params.get_param("prema_dispatch.home_base_address", "") or partner.contact_address or ""
        name = params.get_param("prema_dispatch.home_base_name", "") or company.name or "Home Base"
        radius = max(50.0, min(1000.0, _float_param("prema_dispatch.home_base_radius_m", 200.0)))

        return {
            "success": True,
            "base": {
                "name": name,
                "address": address,
                "lat": _float_param("prema_dispatch.home_base_lat", partner_lat),
                "lng": _float_param("prema_dispatch.home_base_lng", partner_lng),
                "radius_m": radius,
            },
        }

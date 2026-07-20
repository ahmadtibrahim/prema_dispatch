import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        _logger.exception("Warehouse RPC failed")
        return {"success": False, "error": str(exc)}


class WarehouseAppController(http.Controller):

    @http.route("/dispatch/warehouse", type="http", auth="public", website=False)
    def warehouse_app(self, **kwargs):
        user = request.env.user
        if user._is_public():
            return request.redirect("/web/login?redirect=/dispatch/warehouse")
        allowed = [
            "prema_dispatch.group_dispatch_warehouse",
            "prema_dispatch.group_dispatch_manager",
            "prema_dispatch.group_dispatcher",
            "base.group_system",
        ]
        if not any(user.has_group(g) for g in allowed):
            return request.redirect("/web/login?redirect=/dispatch/warehouse")
        vehicles = request.env["fleet.vehicle"].search([])
        return request.render("prema_dispatch.warehouse_app_page", {
            "user_name": user.name,
            "vehicles": [{"id": v.id, "name": v.name} for v in vehicles],
        })

    @http.route("/dispatch/warehouse/loadplan/get", type="json", auth="user", methods=["POST"])
    def get_load_plan(self, vehicle_id, operating_date, **kw):
        LP = request.env["prema.dispatch.load.plan"]
        return _safe(lambda: LP.get_or_create_for_vehicle_date_warehouse(vehicle_id, operating_date))

    # Mutating actions reuse the existing (warehouse-aware, see
    # services/dispatch_auth.py) load-plan model methods directly — no
    # second copy of assign/move/mark_loaded/exception/upload logic.
    @http.route("/dispatch/warehouse/loadplan/mark_loaded", type="json", auth="user", methods=["POST"])
    def mark_loaded(self, load_plan_id, item_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        def _do():
            plan.mark_pallet_loaded(item_id, version)
            return plan.get_load_plan_for_warehouse()
        return _safe(_do)

    @http.route("/dispatch/warehouse/loadplan/move", type="json", auth="user", methods=["POST"])
    def move(self, load_plan_id, item_id, position_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        def _do():
            plan.move_pallet(item_id, position_id, version)
            return plan.get_load_plan_for_warehouse()
        return _safe(_do)

    @http.route("/dispatch/warehouse/loadplan/exception", type="json", auth="user", methods=["POST"])
    def report_exception(self, load_plan_id, item_id, exception_type, notes="", photo_attachment_ids=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        def _do():
            plan.report_exception(item_id, exception_type, notes, photo_attachment_ids)
            return plan.get_load_plan_for_warehouse()
        return _safe(_do)

    @http.route("/dispatch/warehouse/document/upload", type="json", auth="user", methods=["POST"])
    def upload_document(self, load_plan_id, document_type, filename, data_b64, stop_id=None, item_id=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.upload_document(document_type, filename, data_b64, stop_id, item_id))

    # ── Public QR route — read-only, minimal, non-sensitive ───────────
    @http.route("/dispatch/pallet/<string:token>", type="http", auth="public", website=False)
    def pallet_public(self, token, **kw):
        data = request.env["prema.dispatch.item"].sudo().get_public_pallet_summary(token)
        if not data.get("success"):
            return request.make_response(
                "<html><body style='font-family:sans-serif;padding:40px;text-align:center'>"
                "<h2>Pallet not found</h2></body></html>",
                headers=[("Content-Type", "text/html")], status=404,
            )
        stops = ", ".join(str(s) for s in data.get("stop_numbers") or []) or "—"
        html = f"""<html><head><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Pallet {data['reference']}</title></head>
        <body style="font-family:-apple-system,sans-serif;padding:24px;max-width:420px;margin:0 auto">
            <h2 style="margin-bottom:4px">📦 {data['reference']}</h2>
            <div style="color:#666;margin-bottom:20px">Status: {data['status']}</div>
            <table style="width:100%;border-collapse:collapse;font-size:15px">
                <tr><td style="padding:8px 0;color:#888">Truck</td><td style="text-align:right;font-weight:700">{data.get('truck') or '—'}</td></tr>
                <tr><td style="padding:8px 0;color:#888">Position</td><td style="text-align:right;font-weight:700">{data.get('position') or 'Unassigned'}</td></tr>
                <tr><td style="padding:8px 0;color:#888">Stop(s)</td><td style="text-align:right;font-weight:700">{stops}</td></tr>
                <tr><td style="padding:8px 0;color:#888">Shared skid</td><td style="text-align:right;font-weight:700">{'Yes' if data.get('shared_skid') else 'No'}</td></tr>
                <tr><td style="padding:8px 0;color:#888">Exception</td><td style="text-align:right;font-weight:700">{data.get('exception_state') or 'none'}</td></tr>
            </table>
            <div style="margin-top:24px;padding:14px;background:#f5f7fa;border-radius:10px;font-size:13px;color:#555">
                Log in to view notes, photos, and take action.
                <div style="margin-top:8px"><a href="/web/login?redirect=/dispatch/warehouse">Log in →</a></div>
            </div>
        </body></html>"""
        return request.make_response(html, headers=[("Content-Type", "text/html")])

import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _safe(fn):
    """Driver App convention: never raise to the browser, never leak a raw
    traceback — return {success: False, error: ...} like every other
    driver_* endpoint. Server-side detail is logged, not exposed."""
    try:
        return fn()
    except Exception as exc:
        _logger.exception("Load Plan driver RPC failed")
        return {"success": False, "error": str(exc)}


class LoadPlanDriverController(http.Controller):

    @http.route("/dispatch/driver/loadplan/get", type="json", auth="user", methods=["POST"])
    def get_load_plan(self, vehicle_id, operating_date, driver_id=None, **kw):
        LP = request.env["prema.dispatch.load.plan"]
        return _safe(lambda: LP.get_or_create_for_vehicle_date(vehicle_id, operating_date, driver_id))

    @http.route("/dispatch/driver/loadplan/assign", type="json", auth="user", methods=["POST"])
    def assign(self, load_plan_id, item_id, position_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.assign_pallet_to_position(item_id, position_id, version))

    @http.route("/dispatch/driver/loadplan/move", type="json", auth="user", methods=["POST"])
    def move(self, load_plan_id, item_id, position_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.move_pallet(item_id, position_id, version))

    @http.route("/dispatch/driver/loadplan/swap", type="json", auth="user", methods=["POST"])
    def swap(self, load_plan_id, item_id_a, item_id_b, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.swap_pallets(item_id_a, item_id_b, version))

    @http.route("/dispatch/driver/loadplan/unassign", type="json", auth="user", methods=["POST"])
    def unassign(self, load_plan_id, item_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.unassign_pallet(item_id, version))

    @http.route("/dispatch/driver/loadplan/mark_loaded", type="json", auth="user", methods=["POST"])
    def mark_loaded(self, load_plan_id, item_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.mark_pallet_loaded(item_id, version))

    @http.route("/dispatch/driver/loadplan/confirm", type="json", auth="user", methods=["POST"])
    def confirm(self, load_plan_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.confirm_loading(version))

    @http.route("/dispatch/driver/loadplan/exception", type="json", auth="user", methods=["POST"])
    def report_exception(self, load_plan_id, item_id, exception_type, notes="", photo_attachment_ids=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.report_exception(item_id, exception_type, notes, photo_attachment_ids))

    @http.route("/dispatch/driver/loadplan/document/upload", type="json", auth="user", methods=["POST"])
    def upload_document(self, load_plan_id, document_type, filename, data_b64, stop_id=None, item_id=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.upload_document(document_type, filename, data_b64, stop_id, item_id))

    @http.route("/dispatch/driver/loadplan/documents", type="json", auth="user", methods=["POST"])
    def get_documents(self, load_plan_id, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.get_documents())

    @http.route("/dispatch/driver/loadplan/assign_stops", type="json", auth="user", methods=["POST"])
    def assign_stops(self, load_plan_id, item_id, stop_allocations=None, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.assign_stops_to_pallet(item_id, stop_allocations or [], version))

    @http.route("/dispatch/driver/loadplan/remove_stop", type="json", auth="user", methods=["POST"])
    def remove_stop(self, load_plan_id, item_id, stop_id, version=None, **kw):
        plan = request.env["prema.dispatch.load.plan"].browse(load_plan_id)
        return _safe(lambda: plan.remove_stop_from_pallet(item_id, stop_id, version))

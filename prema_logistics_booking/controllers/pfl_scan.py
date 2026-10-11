# -*- coding: utf-8 -*-
"""Pallet scanning endpoints for the barcode app and the backend scanner.

Scans are staff-only: a package id must never resolve for a customer or an
anonymous caller. Everything returned is scoped to the pallet that was scanned
— there is no way to enumerate another customer's pallets through these routes.
"""
import json
import logging

from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden, NotFound

_logger = logging.getLogger(__name__)


class PflScanController(http.Controller):

    def _check_staff(self):
        user = request.env.user
        if user._is_public() or not (
                user.has_group("stock.group_stock_user")
                or user.has_group("base.group_system")):
            raise Forbidden("Scanning is restricted to warehouse staff.")
        return user

    def _json(self, payload, status=200):
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json")], status=status)

    @http.route("/pfl/scan/package/<string:code>", type="http", auth="user",
                methods=["GET"], csrf=False)
    def scan_package(self, code, **kwargs):
        """Resolve a pallet label to its customer, booking, location and target."""
        self._check_staff()
        code = (code or "").strip()
        package = request.env["stock.quant.package"].sudo().search(
            [("name", "=", code)], limit=1)
        if not package and code:
            package = request.env["stock.quant.package"].sudo().search(
                [("name", "ilike", code)], limit=1)
        if not package:
            raise NotFound("No pallet with that label.")
        payload = package.pfl_scan_payload()
        payload["ok"] = True
        return self._json(payload)

    @http.route("/pfl/scan/placement", type="http", auth="user",
                methods=["POST"], csrf=False)
    def scan_placement(self, **post):
        """Confirm that a pallet was put in the position reserved for it."""
        self._check_staff()
        code = (post.get("package") or post.get("code") or "").strip()
        position_code = (post.get("position") or post.get("location") or "").strip()
        if not code or not position_code:
            return self._json(
                {"ok": False, "error": "package and position are required"}, 400)
        Package = request.env["stock.quant.package"].sudo()
        Location = request.env["stock.location"].sudo()
        package = Package.search([("name", "=", code)], limit=1)
        if not package:
            return self._json({"ok": False, "error": "unknown pallet"}, 404)
        position = Location.search(
            [("name", "=", position_code), ("usage", "=", "internal")], limit=1)
        if not position:
            return self._json({"ok": False, "error": "unknown position"}, 404)
        reservation = request.env["logistics.position.reservation"].sudo().search(
            [("package_id", "=", package.id), ("state", "=", "reserved")], limit=1)
        expected = reservation.location_id
        matches = bool(expected) and expected.id == position.id
        result = {
            "ok": matches,
            "package": package.name,
            "scanned_position": position.complete_name,
            "reserved_position": expected.complete_name if expected else "",
            "customer": package.pfl_scan_payload()["customer_name"],
            "booking": package.pfl_scan_payload()["booking"],
        }
        if not expected:
            result["error"] = "no position is reserved for this pallet"
        elif not matches:
            result["error"] = "wrong position"
        else:
            package.message_post(body=(
                "Placement confirmed at %s by %s."
                % (position.complete_name, request.env.user.name)))
        return self._json(result, 200 if matches else 409)

"""Backward-compatible redirects to the single maintained network screens."""

from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden


def _require_dispatch_staff():
    user = request.env.user
    if not user.has_group("prema_dispatch.group_dispatcher") and not user.has_group(
        "prema_dispatch.group_dispatch_manager"
    ):
        raise Forbidden()


class LogisticsNetworkMap(http.Controller):

    @http.route("/logistics/network-map", type="http", auth="user", website=False)
    def network_map(self, **kwargs):
        """Old bookmarks now open the maintained Where We Go client action."""
        del kwargs
        _require_dispatch_staff()
        return request.redirect(
            "/web#action=prema_logistics_booking.action_where_we_go"
        )

    @http.route("/logistics/price-matrix", type="http", auth="user", website=False)
    def price_matrix(self, **kwargs):
        """The duplicate price matrix was retired; Corridors own pricing."""
        del kwargs
        _require_dispatch_staff()
        return request.redirect(
            "/web#action=prema_logistics_booking.action_logistics_corridor"
        )

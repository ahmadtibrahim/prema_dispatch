from odoo import http
from odoo.http import request


class DispatchManualController(http.Controller):

    @http.route("/dispatch/manual", type="http", auth="user", website=False)
    def dispatch_manual(self, **kwargs):
        """Serve the standalone, printable Prema Dispatch user manual."""
        return request.render("prema_dispatch.dispatch_manual_page", {})

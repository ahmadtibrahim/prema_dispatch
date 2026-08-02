"""Weekly Schedule Board — redirect to the consolidated Dispatch Planner.

The standalone server-rendered weekly schedule board has been merged into
prema_dispatch's Dispatch Planner (dispatch_board.js). This controller now
redirects to maintain backwards compatibility for any saved bookmarks.
"""
from odoo import http
from odoo.http import request


class WeeklyScheduleBoard(http.Controller):

    @http.route("/dispatch/schedule-board", type="http", auth="user", website=True)
    def schedule_board(self, **kw):
        """Redirect to the consolidated Dispatch Planner which now includes
        corridor/lane schedule data previously shown here."""
        return request.redirect("/web#action=prema_dispatch.action_dispatch_planner_board&active_id=dispatch_planner")

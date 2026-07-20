import glob
import os

from odoo import fields, models

MONITOR_DIR = "/var/spool/asterisk/monitor"


class VoipCall(models.Model):
    _inherit = "voip.call"

    recording_filename = fields.Char(
        compute="_compute_recording_filename",
        string="Recording File",
        help="Best-effort match against Asterisk's MixMonitor recordings "
             "(filename convention: inbound/outbound-{number}-{uniqueid}.gsm) "
             "by caller number + call time. Not a database link — recordings "
             "live on the filesystem, this just finds the most likely file.",
    )

    def _compute_recording_filename(self):
        for call in self:
            call.recording_filename = call._find_recording_file()

    def _find_recording_file(self):
        self.ensure_one()
        if not self.phone_number or not os.path.isdir(MONITOR_DIR):
            return False
        digits = "".join(c for c in self.phone_number if c.isdigit())
        if not digits:
            return False
        prefix = "inbound" if self.direction == "incoming" else "outbound"
        matches = glob.glob(os.path.join(MONITOR_DIR, f"{prefix}-*{digits[-10:]}*.gsm"))
        if not matches:
            matches = glob.glob(os.path.join(MONITOR_DIR, f"*{digits[-10:]}*.gsm"))
        if not matches:
            return False
        call_ts = self.start_date or self.create_date
        if call_ts:
            call_epoch = call_ts.timestamp()
            matches.sort(key=lambda f: abs(os.path.getmtime(f) - call_epoch))
        return os.path.basename(matches[0])

    def action_play_recording(self):
        self.ensure_one()
        filename = self._find_recording_file()
        if not filename:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "No recording found", "type": "warning"},
            }
        return {
            "type": "ir.actions.act_url",
            "url": f"/prema_dispatch/call_recording/{self.id}",
            "target": "self",
        }

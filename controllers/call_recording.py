import logging
import os
import subprocess
import tempfile

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)
MONITOR_DIR = "/var/spool/asterisk/monitor"


class CallRecordingController(http.Controller):

    @http.route("/prema_dispatch/call_recording/<int:call_id>", type="http", auth="user")
    def call_recording(self, call_id, **kwargs):
        """Stream a call's matched Asterisk recording as MP3 (browsers can't
        play the raw GSM codec MixMonitor records in), same ffmpeg approach
        the call-transcriber service already uses for Whisper uploads."""
        call = request.env["voip.call"].browse(call_id)
        if not call.exists():
            return request.not_found()
        filename = call._find_recording_file()
        if not filename:
            return request.not_found()
        src_path = os.path.join(MONITOR_DIR, filename)
        if not os.path.isfile(src_path):
            return request.not_found()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src_path, "-ac", "1", "-b:a", "32k", tmp_path],
                capture_output=True, timeout=30, check=True,
            )
            with open(tmp_path, "rb") as f:
                data = f.read()
        except Exception:
            _logger.exception("Failed to transcode call recording %s", src_path)
            return request.not_found()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        return request.make_response(
            data,
            headers=[("Content-Type", "audio/mpeg"),
                     ("Content-Disposition", f'inline; filename="{filename}.mp3"')],
        )

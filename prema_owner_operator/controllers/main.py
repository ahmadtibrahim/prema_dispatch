# -*- coding: utf-8 -*-
"""Public application form + private document delivery.

Two routes, two very different trust levels:

* ``/owner-operator/apply`` — public, rate-limited, CSRF-protected, with an
  upload allowlist. It never returns any stored data.
* ``/owner_operator/document/<id>`` — the ONLY way a document is served. It
  requires a logged-in onboarding user, and the attachment must belong to a
  document row this user can read. There is no public URL and no access
  token: nothing here is reachable without a session.
"""
import base64
import logging
import time
from datetime import datetime, timedelta

from werkzeug.exceptions import Forbidden, NotFound

from odoo import _, http
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)

ALLOWED_MIMETYPES = {
    "application/pdf", "image/jpeg", "image/png", "image/webp",
}
MAX_BYTES = 10 * 1024 * 1024
MAGIC = (
    (b"%PDF", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

# Per-process guard. The cross-worker guard is the DB count below; this one
# only stops a single worker from being hammered.
_HITS = {}
_WINDOW = 60
_LIMIT = 6


def _sniff_mimetype(payload):
    for magic, mimetype in MAGIC:
        if payload.startswith(magic):
            return mimetype
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


class OwnerOperatorController(http.Controller):

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    def _require_reviewer(self):
        if not http.request.env.user.has_group(
                "prema_owner_operator.group_owner_operator"):
            raise AccessError(_("Not available to this user."))
        return http.request.env.user

    def _throttled(self, key):
        now = time.time()
        hits = [t for t in _HITS.get(key, []) if now - t < _WINDOW]
        if len(hits) >= _LIMIT:
            _HITS[key] = hits
            return True
        hits.append(now)
        _HITS[key] = hits
        return False

    def _client_ip(self):
        return (http.request.httprequest.headers.get("X-Forwarded-For")
                or http.request.httprequest.remote_addr or "?").split(",")[0].strip()

    # ------------------------------------------------------------------
    # public form
    # ------------------------------------------------------------------
    @http.route("/owner-operator/apply", type="http", auth="public",
                website=True, csrf=True, sitemap=False)
    def apply(self, **post):
        values = {"errors": [], "post": post}
        job = http.request.env["prema.owner.operator.application"].sudo(
        )._public_job()
        values["job"] = job
        if http.request.httprequest.method == "GET" or not post:
            return http.request.render(
                "prema_owner_operator.apply_form", values)

        errors = self._validate(post)
        if errors:
            values["errors"] = errors
            return http.request.render(
                "prema_owner_operator.apply_form", values)

        Application = http.request.env["prema.owner.operator.application"].sudo()
        app = Application._create_from_public(post)
        # initial uploads: one row per uploaded file, matched to its slot
        for slot in ("cvor", "coi_applicant", "business_registration",
                     "photo_front", "photo_rear", "photo_left", "photo_right",
                     "photo_cargo_interior"):
            upload = post.get("file_%s" % slot)
            if upload and getattr(upload, "filename", None):
                self._store_upload(app, slot, upload)
        try:
            http.request.env.ref(
                "prema_owner_operator.mail_template_ack"
            ).sudo().send_mail(app.id, force_send=False)
        except Exception:  # noqa: BLE001 — an email must not lose the record
            _logger.warning("owner-operator acknowledgement failed", exc_info=True)
        return http.request.redirect("/owner-operator/thanks")

    @http.route("/owner-operator/thanks", type="http", auth="public",
                website=True, csrf=True, sitemap=False)
    def thanks(self, **kw):
        return http.request.render("prema_owner_operator.apply_thanks")

    def _validate(self, post):
        errors = []
        required = {
            "contact_name": "full name",
            "email": "email address",
            "phone": "phone number",
            "home_city": "home city",
            "home_province": "home province",
            "equipment_type": "equipment type",
            "vin": "VIN",
        }
        for key, label in required.items():
            if not (post.get(key) or "").strip():
                errors.append(_("Please provide your %s.") % label)
        email = (post.get("email") or "").strip()
        if email and "@" not in email:
            errors.append(_("That email address does not look valid."))
        # per-email cap — survives workers and restarts, unlike a memory hit
        if email:
            since = datetime.now() - timedelta(hours=1)
            recent = http.request.env["prema.owner.operator.application"].sudo(
            ).search_count([
                ("email", "=ilike", email),
                ("create_date", ">=", since),
            ])
            if recent >= 3:
                errors.append(_(
                    "Several applications were already received from this "
                    "address. Please wait before submitting again."))
        if self._throttled(self._client_ip()):
            errors.append(_("Too many attempts — please try again shortly."))
        for slot in ("cvor", "coi_applicant", "business_registration",
                     "photo_front", "photo_rear", "photo_left", "photo_right",
                     "photo_cargo_interior"):
            upload = post.get("file_%s" % slot)
            if upload and getattr(upload, "filename", None):
                problem = self._check_upload(upload)
                if problem:
                    errors.append("%s: %s" % (slot, problem))
        return errors

    def _check_upload(self, upload):
        data = upload.read()
        upload.seek(0)
        if len(data) > MAX_BYTES:
            return _("file is larger than 10 MB")
        sniffed = _sniff_mimetype(data)
        if sniffed not in ALLOWED_MIMETYPES:
            return _("only PDF, JPG, PNG or WEBP files are accepted")
        if (upload.mimetype or "") not in ALLOWED_MIMETYPES:
            return _("file type is not accepted")
        return None

    def _store_upload(self, app, doc_type, upload):
        payload = upload.read()
        attachment = http.request.env["ir.attachment"].sudo().create({
            "name": upload.filename,
            "datas": base64.b64encode(payload),
            "mimetype": _sniff_mimetype(payload) or "application/octet-stream",
            "res_model": "prema.owner.operator.document",
            "res_id": 0,
            "public": False,
        })
        doc = app.document_ids.filtered(
            lambda d: d.document_type == doc_type)[:1]
        if not doc:
            doc = http.request.env["prema.owner.operator.document"].sudo(
            ).create({"application_id": app.id, "document_type": doc_type})
        attachment.write({"res_id": doc.id})
        doc.sudo().write({
            "attachment_id": attachment.id,
            "status": "received",
            "source": "applicant",
            "received_on": http.request.env["prema.owner.operator.document"]._fields["received_on"].now(),
        })
        return doc

    # ------------------------------------------------------------------
    # private document delivery
    # ------------------------------------------------------------------
    @http.route("/owner_operator/document/<int:document_id>", type="http",
                auth="user", website=False, csrf=False, sitemap=False)
    def document(self, document_id, download=0, **kw):
        self._require_reviewer()
        doc = http.request.env["prema.owner.operator.document"].browse(
            document_id)
        if not doc.exists() or not doc.attachment_id:
            raise NotFound()
        try:
            doc.check_access("read")
        except AccessError:
            raise NotFound()
        attachment = doc.attachment_id.sudo()
        filename = attachment.name or "document"
        disposition = "attachment" if download else "inline"
        return http.request.make_response(attachment.raw or b"", headers=[
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", "%s; filename=\"%s\"" % (
                disposition, filename.replace('"', ""))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, no-store"),
        ])

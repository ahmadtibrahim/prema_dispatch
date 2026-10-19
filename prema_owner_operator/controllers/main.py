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
from odoo.addons.website_hr_recruitment.controllers.main import (
    WebsiteHrRecruitment)
from odoo.exceptions import AccessError, ValidationError
from werkzeug.urls import url_quote

from ..application_mapping import (
    PROVINCE_CHOICES, PROVINCES, UPLOAD_MAP, normalise_public)

try:  # the canonical Places integration lives in Prema Dispatch
    from odoo.addons.prema_logistics_booking.services.google_places_service \
        import GooglePlacesService
except ImportError:  # pragma: no cover — module not installed
    GooglePlacesService = None

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
        user = http.request.env.user
        allowed = (
            "prema_owner_operator.group_owner_operator",
            "hr_recruitment.group_hr_recruitment_manager",
            "fleet.fleet_group_manager",
        )
        if not any(user.has_group(group) for group in allowed):
            raise AccessError(_("Not available to this user."))
        return user

    def _throttled(self, key, limit=None, window=None):
        now = time.time()
        limit = _LIMIT if limit is None else limit
        window = _WINDOW if window is None else window
        hits = [t for t in _HITS.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _HITS[key] = hits
            return True
        hits.append(now)
        _HITS[key] = hits
        return False

    def _client_ip(self):
        return (http.request.httprequest.headers.get("X-Forwarded-For")
                or http.request.httprequest.remote_addr or "?").split(",")[0].strip()

    def _resolve_home_city(self, vals, errors, env=None):
        """Resolve the selected Google city server-side.

        A Place ID is never trusted on its own: the canonical Places service
        must return a Canadian city with a usable coordinate pair. When it
        cannot, the manual city/province is kept and the application is
        flagged for review instead of being lost.
        """
        env = env or http.request.env
        place_id = vals.get("home_city_place_id")
        if not place_id:
            vals.setdefault("home_location_review", True)
            vals.setdefault(
                "home_location_review_reason",
                "Location not verified — entered manually.")
            return vals
        resolved = None
        if GooglePlacesService is not None:
            try:
                resolved = GooglePlacesService(
                    env).resolve_place(place_id)
            except Exception:  # noqa: BLE001 — never lose the application
                _logger.warning(
                    "owner-operator city resolution failed", exc_info=True)
        if resolved and (resolved.get("country_code") or "").upper() == "CA" \
                and resolved.get("city"):
            code = (resolved.get("province_code") or "").upper()
            vals.update({
                "home_city": resolved["city"],
                "home_city_place_id": resolved.get("place_id") or place_id,
                "home_latitude": resolved["latitude"],
                "home_longitude": resolved["longitude"],
                "home_location_verified": True,
                "home_location_review": False,
                "home_location_review_reason": False,
            })
            if code in PROVINCES:
                vals["home_province"] = PROVINCES[code]
                vals["home_province_code"] = code
        else:
            vals.update({
                "home_location_verified": False,
                "home_location_review": True,
                "home_location_review_reason": (
                    "Google could not verify the selected Canadian city; "
                    "manual review required."),
            })
        return vals

    def _places_candidates(self, query, limit=8):
        if GooglePlacesService is None:
            return []
        try:
            return GooglePlacesService(
                http.request.env).search_address_candidates(query, limit=limit)
        except Exception:  # noqa: BLE001 — availability must not break the form
            _logger.warning("owner-operator Places search failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # public form
    # ------------------------------------------------------------------
    def _apply_impl(self, job, post):
        """Shared implementation for both entry points: the dedicated route
        and the job's own /jobs/apply/<job> page."""
        values = {"errors": [], "post": post, "job": job,
                  "provinces": PROVINCES,
                  "province_choices": PROVINCE_CHOICES}
        if http.request.httprequest.method == "GET" or not post:
            return http.request.render(
                "prema_owner_operator.apply_form", values)

        vals, errors = normalise_public(post, strict=True)
        vals = self._resolve_home_city(vals, errors)

        email = vals.get("email")
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

        # Validate every upload BEFORE creating anything. A rejected
        # submission must leave no candidate, applicant or application.
        uploads = {}
        for raw_name, doc_type in UPLOAD_MAP.items():
            upload = post.get(raw_name)
            if upload and getattr(upload, "filename", None):
                problem = self._check_upload(upload)
                if problem:
                    errors.append("%s: %s" % (raw_name, problem))
                else:
                    uploads[doc_type] = upload

        if errors:
            values["errors"] = errors
            return http.request.render(
                "prema_owner_operator.apply_form", values)

        Application = http.request.env["prema.owner.operator.application"].sudo()
        try:
            app = Application._create_from_public(vals)
        except ValidationError as exc:
            values["errors"] = [str(exc)]
            return http.request.render(
                "prema_owner_operator.apply_form", values)
        for doc_type, upload in uploads.items():
            self._store_upload(app, doc_type, upload)
        try:
            http.request.env.ref(
                "prema_owner_operator.mail_template_ack"
            ).sudo().send_mail(app.id, force_send=False)
        except Exception:  # noqa: BLE001 — an email must not lose the record
            _logger.warning("owner-operator acknowledgement failed", exc_info=True)
        return http.request.redirect("/owner-operator/thanks")

    @http.route("/owner-operator/apply", type="http", auth="public",
                website=True, csrf=True, sitemap=False)
    def apply(self, **post):
        job = http.request.env["prema.owner.operator.application"].sudo(
        )._public_job()
        return self._apply_impl(job, post)

    @http.route("/owner-operator/places/cities", type="http", auth="public",
                website=True, csrf=False, sitemap=False)
    def places_cities(self, q=None, **kw):
        """Canadian city autocomplete backed by the shared Places service."""
        query = (q or "").strip()
        if len(query) < 2 or self._throttled(
                "places:" + self._client_ip(), limit=60, window=60):
            return http.request.make_json_response([])
        out = []
        seen = set()
        for cand in self._places_candidates(query):
            if (cand.get("country_code") or "").upper() != "CA":
                continue
            city = (cand.get("city") or "").strip()
            code = (cand.get("province_code") or "").upper()
            place_id = cand.get("place_id")
            if not city or code not in PROVINCES or not place_id:
                continue
            key = (place_id, city, code)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "place_id": place_id,
                "city": city,
                "province": PROVINCES[code],
                "province_code": code,
                "latitude": cand.get("latitude"),
                "longitude": cand.get("longitude"),
                "label": "%s, %s" % (city, code),
            })
            if len(out) >= 8:
                break
        return http.request.make_json_response(out)

    @http.route("/owner-operator/thanks", type="http", auth="public",
                website=True, csrf=True, sitemap=False)
    def thanks(self, **kw):
        job = http.request.env["prema.owner.operator.application"].sudo(
        )._public_job()
        return http.request.render(
            "prema_owner_operator.apply_thanks", {"job": job})

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
    @http.route("/owner_operator/coi_version/<int:version_id>", type="http",
                auth="user", website=False, csrf=False, sitemap=False)
    def coi_version(self, version_id, download=0, **kw):
        """Private delivery for a stored COI version (current or historical)."""
        self._require_reviewer()
        version = http.request.env[
            "prema.owner.operator.coi.version"].browse(version_id)
        if not version.exists() or not version.attachment_id:
            raise NotFound()
        try:
            version.check_access("read")
            version.application_id.check_access("read")
        except AccessError:
            raise NotFound()
        attachment = version.attachment_id.sudo()
        filename = attachment.name or "coi"
        previewable = (attachment.mimetype or "").startswith(
            ("application/pdf", "image/"))
        wants_download = str(download).lower() not in ("", "0", "false", "none")
        disposition = "inline" if previewable and not wants_download \
            else "attachment"
        return http.request.make_response(attachment.raw or b"", headers=[
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", "%s; filename=\"%s\"" % (
                disposition, filename.replace('"', ""))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, no-store"),
        ])

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
        # Only PDFs and images are shown inline; anything else is offered as
        # a download even if the caller omits ?download=1.
        previewable = (attachment.mimetype or "").startswith(
            ("application/pdf", "image/"))
        wants_download = str(download).lower() not in ("", "0", "false", "none")
        disposition = "inline" if previewable and not wants_download \
            else "attachment"
        return http.request.make_response(attachment.raw or b"", headers=[
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", "%s; filename=\"%s\"" % (
                disposition, filename.replace('"', ""))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, no-store"),
        ])

    @http.route("/owner_operator/documents/<int:document_id>", type="http",
                auth="user", website=False, csrf=False, sitemap=False)
    def owner_operator_document(self, document_id, download=0, **kw):
        """Private delivery for an approval-created Documents record."""
        self._require_reviewer()
        document = http.request.env["documents.document"].browse(document_id)
        if not document.exists() or not document.attachment_id \
                or not document.oo_application_id:
            raise NotFound()
        try:
            document.check_access("read")
        except AccessError:
            raise NotFound()
        attachment = document.attachment_id.sudo()
        filename = attachment.name or "document"
        previewable = (attachment.mimetype or "").startswith(
            ("application/pdf", "image/"))
        wants_download = str(download).lower() not in ("", "0", "false", "none")
        disposition = "inline" if previewable and not wants_download \
            else "attachment"
        return http.request.make_response(attachment.raw or b"", headers=[
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", "%s; filename=\"%s\"" % (
                disposition, filename.replace('"', ""))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, no-store"),
        ])


class OwnerOperatorJobApply(WebsiteHrRecruitment):
    """Make the job's own apply page the custom application.

    Re-decorating ``jobs_apply`` with an empty ``@http.route()`` keeps the
    original route and binds this implementation to it, so
    /jobs/apply/owner-operator-5 stops showing Odoo's résumé/LinkedIn form.
    Every OTHER job keeps the standard form untouched.
    """

    @http.route()
    def jobs_apply(self, job, **post):
        Application = http.request.env[
            "prema.owner.operator.application"].sudo()
        target = Application._public_job()
        if not target or job.id != target.id:
            return super().jobs_apply(job, **post)
        handler = OwnerOperatorController()
        return handler._apply_impl(job, post)

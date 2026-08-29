"""
Shared upload validation for Driver App file uploads (POD/POP evidence,
entrance photos, and their scanner variant — all of which funnel through
this one helper instead of each duplicating its own base64/MIME/size
logic).

Confirmed by direct code audit before this fix: the only MIME detection
that existed was a filename-suffix guess (".jpg/.jpeg" -> image/jpeg,
else "application/octet-stream") with no signature check, no size cap,
no duplicate detection, and no filename sanitization anywhere in the
upload path. This module replaces that guess with real content-signature
detection, without changing where the resulting bytes get stored
(ir.attachment, same res_model/res_id conventions as before).

Deliberately NOT introduced here: the Phase 2 document-metadata model.
Duplicate detection instead reuses whatever attachments are already
linked to the target record's existing category field (e.g.
stop.pod_attachment_ids) — no new storage.
"""
import base64
import hashlib
import io
import re
import unicodedata
from datetime import datetime

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # Pillow is a hard dependency of this Odoo install,
    Image = None      # but degrade gracefully rather than crash the module
    UnidentifiedImageError = Exception

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB — see Phase 1C spec

ALLOWED_MIMES = {
    "image/jpeg", "image/png", "image/heic", "image/heif", "application/pdf",
}

_FILENAME_UNSAFE_RE = re.compile(r'[\\/:\*\?"<>\|\x00-\x1f]')
_MAX_FILENAME_LEN = 150

_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"}


class UploadError(Exception):
    """Raised with a structured (code, message) pair — callers convert
    this to the {"success": False, "code": ..., "message": ...} response
    shape rather than leaking a raw traceback to the driver."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _detect_signature(data):
    """Return the MIME type implied by the actual bytes, or None if none
    of the 5 supported formats' signatures match. Never trusts a
    filename extension."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "image/heic"  # HEIC/HEIF/AVIF all share this ISO-BMFF box structure
    return None


def _verify_decodable(data, mimetype):
    """For JPEG/PNG, actually decode with Pillow (not just check the
    magic bytes) — catches truncated/corrupt files that happen to start
    with a valid signature. PDF/HEIC are checked by signature only per
    the Phase 1C spec (no full parse, no attempted HEIC decode since this
    server has no HEIF plugin — confirmed absent, see project memory)."""
    if mimetype not in ("image/jpeg", "image/png") or Image is None:
        return True
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def sanitize_filename(raw_name, category="file", ext_hint=""):
    """Strip path separators/control chars, cap length, and — if that
    leaves nothing usable — synthesize a safe name from the category and
    current timestamp (e.g. "pod_2026-07-19_143522.jpg")."""
    name = unicodedata.normalize("NFKC", (raw_name or "")).strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # strip any path component
    name = _FILENAME_UNSAFE_RE.sub("_", name)
    name = name.strip(" .")[:_MAX_FILENAME_LEN]
    if name and name not in (".", ".."):
        return name
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    ext = ext_hint or "bin"
    return f"{category}_{ts}.{ext}"


def _ext_for_mime(mimetype):
    return {
        "image/jpeg": "jpg", "image/png": "png",
        "image/heic": "heic", "image/heif": "heif",
        "application/pdf": "pdf",
    }.get(mimetype, "bin")


def decode_and_validate(data_b64, filename, category="file", max_bytes=MAX_UPLOAD_BYTES):
    """Decode + validate an uploaded file. Returns a dict:
        {data, mimetype, filename, checksum_sha256, preview_available}
    or raises UploadError with one of the structured codes from the
    Phase 1C spec (invalid_base64, empty_file, file_too_large,
    unsupported_type, mime_mismatch, invalid_signature)."""
    if not data_b64:
        raise UploadError("empty_file", "No file data was received.")
    # Callers pass str (JSON) in production, but internal/test callers may
    # pass bytes — b64decode accepts both while str-methods on bytes raise
    # TypeError, so normalize first (found while wiring the §12 tests).
    if isinstance(data_b64, bytes):
        data_b64 = data_b64.decode("ascii", errors="ignore")
    try:
        # Odoo's own attachment.datas convention: plain base64, no data:
        # URL prefix. Strip one if the caller sent one anyway.
        b64 = data_b64.split(",", 1)[1] if data_b64.startswith("data:") else data_b64
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise UploadError("invalid_base64", "The uploaded file data was corrupted.")

    if not data:
        raise UploadError("empty_file", "The uploaded file is empty.")
    if len(data) > max_bytes:
        mb = max_bytes / (1024 * 1024)
        raise UploadError("file_too_large", f"This file is larger than the {mb:.0f} MB limit.")

    detected = _detect_signature(data)
    if detected is None:
        raise UploadError("unsupported_type", "Upload a JPG, PNG, HEIC, or PDF file.")
    if detected not in ALLOWED_MIMES:
        raise UploadError("unsupported_type", "Upload a JPG, PNG, HEIC, or PDF file.")
    if not _verify_decodable(data, detected):
        raise UploadError("invalid_signature", "This file looks corrupted — please retake or re-select it.")

    declared_ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    declared_mime_by_ext = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "heic": "image/heic", "heif": "image/heic", "pdf": "application/pdf",
    }.get(declared_ext)
    # Only flag a mismatch when the extension clearly claims a DIFFERENT
    # supported type than what the bytes actually are (e.g. a .png that's
    # really a PDF) — a missing/unknown extension is not itself an error,
    # since the app also generates extension-less names in a few places.
    if declared_mime_by_ext and declared_mime_by_ext != detected:
        raise UploadError("mime_mismatch", "This file's content doesn't match its extension.")

    safe_name = sanitize_filename(filename, category=category, ext_hint=_ext_for_mime(detected))
    checksum = hashlib.sha256(data).hexdigest()
    preview_available = detected in ("image/jpeg", "image/png")

    return {
        "data": data,
        "mimetype": detected,
        "filename": safe_name,
        "checksum_sha256": checksum,
        "preview_available": preview_available,
    }


def find_duplicate(env, candidate_attachments, checksum_sha256):
    """candidate_attachments: the ir.attachment recordset already linked
    to the target record under the relevant category (e.g.
    stop.pod_attachment_ids) — deliberately scoped to just that small
    set, not a global search, both for correctness (duplicate means
    "already on this stop/category", not "exists anywhere in the
    database") and for performance (these lists are always small)."""
    for att in candidate_attachments:
        if not att.datas:
            continue
        try:
            existing_bytes = base64.b64decode(att.datas)
        except Exception:
            continue
        if hashlib.sha256(existing_bytes).hexdigest() == checksum_sha256:
            return att
    return None

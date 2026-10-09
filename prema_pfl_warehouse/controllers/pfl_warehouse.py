# -*- coding: utf-8 -*-
"""The warehouse phone interface: scan, act, photograph.

Two things this deliberately is not.

It is **not** the public pallet page. That page exists (it is in
`prema_dispatch`) and it answers anyone holding the QR: a reference, a truck, a
position, a status. That is the right amount of information for a driver at a
gate and the wrong amount for someone deciding whether to release freight. Every
route here requires a signed-in user in a warehouse group. A token is a
*locator*, never an authorisation — an unguessable string that has been
photographed off a pallet in a yard is not a credential.

It is **not** an IoT integration. Scanning is an input field, so an ordinary
keyboard-wedge scanner works and so does typing; printing is a normal PDF. A
USB scanner and the office printer are enough to run the process, which is what
lets the warehouse start before any hardware decision is made.
"""
import base64
import binascii
import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

#: The roles that may run warehouse operations. Reuses the dispatch groups that
#: already exist rather than defining a second notion of "warehouse staff".
ALLOWED_GROUPS = [
    "prema_dispatch.group_dispatch_warehouse",
    "prema_dispatch.group_dispatch_manager",
    "prema_dispatch.group_dispatcher",
    "base.group_system",
]

#: action name -> (job method, needs a destination)
ACTIONS = {
    "receive": ("action_pfl_receive", False),
    "move": ("action_pfl_move", True),
    "prepare_release": ("action_pfl_prepare_release", False),
    "release": ("action_pfl_release", False),
    "hold": ("action_pfl_hold", False),
    "release_hold": ("action_pfl_release_hold", False),
    "damage": ("action_pfl_record_damage", False),
}


def _authorized():
    """True only for a signed-in user holding a warehouse role."""
    user = request.env.user
    if user._is_public():
        return False
    return any(user.has_group(group) for group in ALLOWED_GROUPS)


def _require_authorized():
    if not _authorized():
        raise AccessError(_(
            "This warehouse operation requires a warehouse role. Sign in with "
            "a warehouse, dispatcher or manager account."))


def _package_summary(package):
    job = package.pfl_job_id
    return {
        "id": package.id,
        "name": package.name,
        "state": package.pfl_state,
        "on_hold": package.pfl_on_hold,
        "hold_reason": package.pfl_hold_reason or "",
        "owner": package.pfl_owner_id.display_name or "",
        "location": package.pfl_location_id.display_name or "",
        "received_at": package.pfl_received_at and str(package.pfl_received_at) or "",
        "released_at": package.pfl_released_at and str(package.pfl_released_at) or "",
        "custody_hours": round(package.pfl_custody_hours, 1),
        "label_mode": package.pfl_label_mode,
        "card": package.pfl_card_id.name or "",
        "job": job and {
            "id": job.id,
            "name": job.name,
            "customer": job.partner_id.display_name,
            "state": job.state,
            "service_mode": job.service_mode,
        } or False,
    }


def _decode_photo(data_b64, filename):
    """Turn a data-URL or bare base64 string into an ir.attachment.

    A failed upload must not lose the operation: the pallet still moved, and
    refusing the whole action because a photo would not decode would push the
    operator to record nothing at all. The photo is reported as failed and the
    action proceeds.
    """
    if not data_b64:
        return None, None
    payload = data_b64.split(",", 1)[1] if "," in data_b64[:64] else data_b64
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None, _("The photo could not be read and was not saved.")
    if not raw:
        return None, _("The photo was empty and was not saved.")
    return {
        "name": filename or "warehouse-photo.jpg",
        "datas": base64.b64encode(raw),
    }, None


class PflWarehouseController(http.Controller):

    # ── the page ───────────────────────────────────────────────────────
    @http.route("/pfl/warehouse", type="http", auth="user", website=False)
    def warehouse_home(self, **kwargs):
        if not _authorized():
            return request.redirect("/web/login?redirect=/pfl/warehouse")
        jobs = request.env["prema.pfl.warehouse.job"].search(
            [("state", "in", ("confirmed", "storing", "prepared"))],
            order="name desc", limit=50)
        return request.render("prema_pfl_warehouse.pfl_warehouse_app_page", {
            "user_name": request.env.user.name,
            "jobs": jobs,
        })

    # ── the API ────────────────────────────────────────────────────────
    @http.route("/pfl/api/resolve", type="json", auth="user", methods=["POST"])
    def resolve(self, code=None, customer_id=None, **kwargs):
        """Resolve a scanned code to a pallet, with operational detail."""
        _require_authorized()
        customer = (
            request.env["res.partner"].browse(int(customer_id))
            if customer_id else None)
        Package = request.env["stock.quant.package"]
        result = Package._pfl_resolve_scan(code, customer)
        if result["status"] == "ok":
            package = result["package"]
            return {
                "status": "ok",
                "source": result["source"],
                "package": _package_summary(package),
            }
        if result["status"] == "ambiguous":
            return {
                "status": "ambiguous",
                "code": result["code"],
                "candidates": [_package_summary(p) for p in result["packages"]],
                "message": _(
                    "That barcode matches more than one pallet. Scan the "
                    "pallet's own QR label, or pick the right customer first — "
                    "the pallets are not merged."),
            }
        return {"status": "not_found", "code": code,
                "message": _("No pallet found for that code.")}

    @http.route("/pfl/api/action", type="json", auth="user", methods=["POST"])
    def act(self, package_ids=None, action=None, dest_barcode=None, notes=None,
            photo=None, photo_name=None, package_id=None, **kwargs):
        """Run one warehouse action against one or more pallets."""
        _require_authorized()
        if action not in ACTIONS:
            raise UserError(_("Unknown warehouse action: %s", action))
        method_name, needs_dest = ACTIONS[action]

        ids = package_ids or ([package_id] if package_id else [])
        packages = request.env["stock.quant.package"].browse(
            [int(i) for i in ids if i])
        packages = packages.exists().filtered(lambda p: p.pfl_job_id)
        if not packages:
            raise UserError(_("No pallet selected."))
        job = packages[:1].pfl_job_id
        if any(p.pfl_job_id != job for p in packages):
            raise UserError(_(
                "The selected pallets belong to different jobs. Run the "
                "action one job at a time."))

        attachment = None
        attachment_vals, photo_warning = _decode_photo(photo, photo_name)
        if attachment_vals:
            attachment_vals.update({
                "res_model": "stock.quant.package",
                "res_id": packages[:1].id,
            })
            attachment = request.env["ir.attachment"].create(attachment_vals)

        method = getattr(job, method_name)
        call_kwargs = {"packages": packages, "notes": notes}
        if method_name == "action_pfl_record_damage":
            call_kwargs = {
                "packages": packages,
                "notes": notes,
                "attachment_ids": attachment and [attachment.id] or None,
            }
        else:
            if attachment:
                call_kwargs["attachment_ids"] = [attachment.id]
            if needs_dest:
                if not dest_barcode:
                    raise UserError(_("Choose a destination for the move."))
                call_kwargs["dest_barcode"] = dest_barcode
            if action in ("hold", "release_hold"):
                call_kwargs["reason"] = notes

        method(**call_kwargs)
        return {
            "status": "ok",
            "photo_saved": bool(attachment),
            "warning": photo_warning,
            "packages": [_package_summary(p) for p in packages],
            "job_state": job.state,
        }

    @http.route("/pfl/api/job", type="json", auth="user", methods=["POST"])
    def job_detail(self, job_id=None, **kwargs):
        _require_authorized()
        job = request.env["prema.pfl.warehouse.job"].browse(int(job_id or 0))
        if not job.exists():
            raise UserError(_("Job not found."))
        return {
            "id": job.id,
            "name": job.name,
            "state": job.state,
            "customer": job.partner_id.display_name,
            "freight_owner": job.freight_owner_id.display_name,
            "service_mode": job.service_mode,
            "uninvoiced_total": job.uninvoiced_total,
            "packages": [_package_summary(p) for p in job.package_ids],
        }

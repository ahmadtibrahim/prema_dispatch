"""Canonical evidence record (spec §35).

One row per captured operational file. The ir.attachment remains the file
store — the stop's POP/POD m2m buckets, the duplicate check, and the
invoice/quotation copies all keep working exactly as before. This record is
the canonical *source of truth* that links an attachment to job / stop /
booking / pallet / invoice, evidence type, driver, capture timestamp and
GPS, so invoices, the driver app, reports and Phase 4's POPP all reference
the same record instead of re-scanning attachment names or descriptions.

Scanner pages are also evidence rows (evidence_type=scan_page) tagged with
a client-generated scan_session; when the driver hits COMPLETE the server
merges them into ONE PDF (spec §17) and the page rows/attachments are
removed, leaving only the final scanned_pop / scanned_pod record.

Deliberately NOT an Odoo attachment subclass: ir.attachment is not
extensible here without touching every existing copy/dedup site, and the
spec's relations (pallet, invoice, driver, GPS) have no home on it.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

EVIDENCE_TYPES = [
    ("pop_general", "POP — General"),
    ("pod_general", "POD — General"),
    ("popp", "POPP — Proof of Pickup Pallet"),
    ("seal_photo", "Seal Photo"),
    ("scanned_pop", "Scanned POP (PDF)"),
    ("scanned_pod", "Scanned POD (PDF)"),
    ("scan_page", "Scan Page (pre-merge)"),
    ("issue_photo", "Issue Photo"),
    ("other", "Other"),
]


class PremaDispatchEvidence(models.Model):
    _name = "prema.dispatch.evidence"
    _description = "Dispatch Evidence Record"
    _order = "captured_at desc, id desc"

    attachment_id = fields.Many2one(
        "ir.attachment", string="Attachment", required=True,
        ondelete="cascade", index=True,
        help="The actual file. Deleting the attachment removes this record "
             "with it; deleting the record keeps the attachment (the stop "
             "m2m link decides visibility).")
    evidence_type = fields.Selection(
        EVIDENCE_TYPES, string="Evidence Type", required=True, index=True)
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", ondelete="cascade", index=True)
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Job", ondelete="cascade", index=True)
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="set null", index=True)
    pallet_id = fields.Many2one(
        "prema.dispatch.item", string="Pallet", ondelete="cascade", index=True,
        help="Physical pallet this proof belongs to (POPP / seal).")
    invoice_id = fields.Many2one(
        "account.move", string="Invoice", ondelete="set null", index=True,
        help="Draft invoice the evidence was synced to (spec §36).")
    driver_id = fields.Many2one(
        "res.users", string="Captured By", ondelete="set null", index=True)
    captured_at = fields.Datetime(string="Captured At", index=True)
    lat = fields.Float(string="Latitude", digits=(16, 7))
    lng = fields.Float(string="Longitude", digits=(16, 7))
    device = fields.Char(string="Device / Source")
    checksum_sha256 = fields.Char(string="SHA-256 Checksum", index=True)

    # §12 evidence relationships: the canonical row now also links to the
    # physical load plan it belongs to and carries the full upload-trace
    # envelope (client idempotency key, original filename, upload time,
    # GPS accuracy, capture timezone). These power the §13 failed-state
    # persistence + retry-by-idempotency-key pipeline.
    load_plan_id = fields.Many2one(
        "prema.dispatch.load.plan", string="Load Plan",
        ondelete="set null", index=True,
        help="Physical load plan the evidence was captured under (derived "
             "from the job's load-plan membership at creation).")
    upload_state = fields.Selection(
        [("uploaded", "Uploaded"), ("failed", "Failed")],
        string="Upload State", default="uploaded", index=True,
        help="'failed' marks a row persisted server-side when the client "
             "upload died mid-flight (spec §57) so retries dedupe via "
             "idempotency_key instead of re-capturing.")
    idempotency_key = fields.Char(
        string="Idempotency Key", index=True,
        help="Client-generated key so a retry of the same capture never "
             "duplicates the row (spec §57).")
    original_filename = fields.Char(
        string="Original Filename",
        help="The file name as chosen on the device, before server-side "
             "renaming/stamping.")
    uploaded_at = fields.Datetime(string="Uploaded At", index=True)
    gps_accuracy_m = fields.Float(
        string="GPS Accuracy (m)",
        help="Reported positioning accuracy at capture time; drives the "
             "'low GPS accuracy' warning instead of a hard lat/lng gate.")
    captured_tz = fields.Char(
        string="Capture Timezone",
        help="The driver's local IANA timezone at capture time, so "
             "captured_at can be re-interpreted in the capture location.")

    # Inline thumbnail for image attachments (backend staff view §2).
    image_preview = fields.Binary(
        string="Preview", compute="_compute_image_preview",
        help="Inline thumbnail of the attachment when it is an image; "
             "empty for PDFs/documents (View/Download still available).")

    @api.depends("attachment_id", "attachment_id.mimetype", "attachment_id.datas")
    def _compute_image_preview(self):
        for ev in self:
            att = ev.attachment_id
            if att and (att.mimetype or "").startswith("image/"):
                ev.image_preview = att.datas
            else:
                ev.image_preview = False

    # Scanner session state (spec §17 multi-page → single PDF)
    scan_session = fields.Char(string="Scan Session", index=True)
    scan_page_index = fields.Integer(string="Scan Page Index")
    merged_into_id = fields.Many2one(
        "prema.dispatch.evidence", string="Merged Into", ondelete="set null",
        help="Final merged PDF this page became part of.")

    # Retake supersession (spec §55): when a photo is retaken, the old
    # record points at the new one so invoice copies can be replaced.
    superseded_by_id = fields.Many2one(
        "prema.dispatch.evidence", string="Superseded By", ondelete="set null")

    _sql_constraints = [
        ("attachment_uniq", "UNIQUE (attachment_id)",
         "An evidence record already exists for this attachment."),
    ]

    @api.model
    def _customer_visible_domain(self, job=None, evidence_type=None,
                                 pallet_id=None):
        """§13: the customer's tracking page shows COMPLETED evidence only —
        never rows still failed/pending (upload_state) and never
        superseded (retaken) proof. Superseded rows stay in the audit
        trail for dispatchers/invoicing; customers only ever see the live
        capture."""
        domain = [("upload_state", "=", "uploaded"),
                  ("superseded_by_id", "=", False)]
        if job:
            domain.append(("job_id", "=", getattr(job, "id", job)))
        if evidence_type:
            domain.append(("evidence_type", "=", evidence_type))
        if pallet_id:
            domain.append(
                ("pallet_id", "=", getattr(pallet_id, "id", pallet_id)))
        return domain

    @api.model
    def _create_evidence(self, attachment, stop, ev_type, meta=None):
        """Create the canonical record for an attachment just uploaded by
        a driver. `meta` may carry captured_at / lat / lng / device /
        scan_session / scan_page_index / pallet_id from the app, plus the
        §12 envelope: idempotency_key / original_filename / uploaded_at /
        gps_accuracy_m / captured_tz.

        Called from driver_add_evidence (already auth'd for the stop), so
        sudo() keeps this independent of the evidence model's own rules."""
        meta = meta or {}
        if not isinstance(meta, dict):
            meta = {}
        captured_at = meta.get("captured_at") or False
        # The app stamps evidence with new Date().toISOString()
        # ("2026-08-20T00:53:35.722Z"); the ORM Datetime field only
        # accepts "YYYY-MM-DD HH:MM:SS". Un-normalized, every upload was
        # rejected with a ValueError (caught, logged, success=False) while
        # the attachment/evidence row still committed — the client never
        # saw success, so per-pallet photos never attached and the guided
        # pickup gate stayed blocked. Found in v7 browser UAT; normalize
        # here so pop/pod/popp/scan all accept both forms.
        if isinstance(captured_at, str):
            captured_at = captured_at.replace("T", " ").split(".")[0].rstrip("Z").strip()
        uploaded_at = meta.get("uploaded_at") or False
        if isinstance(uploaded_at, str):
            uploaded_at = uploaded_at.replace("T", " ").split(".")[0].rstrip("Z").strip()
        job = stop.job_id
        # prema_logistics_booking adds logistics_booking_id to
        # prema.dispatch.job — but its models load AFTER dispatch in the
        # graph (booking depends on dispatch), so at_install tests can run
        # before the field exists. Same guard pattern as dispatch_stop.py.
        booking = job.logistics_booking_id if "logistics_booking_id" in job._fields else False
        # §12: derive the physical load plan from the job's load-plan
        # membership (a job sits on at most one plan line at a time).
        plan_line = self.env["prema.dispatch.load.plan.job"].sudo().search(
            [("job_id", "=", job.id)], limit=1)
        load_plan = plan_line.load_plan_id
        return self.sudo().create({
            "attachment_id": attachment.id,
            "evidence_type": ev_type,
            "stop_id": stop.id,
            "job_id": job.id,
            "booking_id": booking.id if booking else False,
            "pallet_id": meta.get("pallet_id") or False,
            "driver_id": self.env.user.id,
            "captured_at": captured_at,
            "lat": meta.get("lat"),
            "lng": meta.get("lng"),
            "device": meta.get("device") or "",
            "checksum_sha256": meta.get("checksum_sha256") or "",
            "scan_session": meta.get("scan_session") or False,
            "scan_page_index": meta.get("scan_page_index") or 0,
            # §12 envelope
            "load_plan_id": load_plan.id if load_plan else (meta.get("load_plan_id") or False),
            "upload_state": meta.get("upload_state") or "uploaded",
            "idempotency_key": meta.get("idempotency_key") or "",
            "original_filename": meta.get("original_filename") or "",
            "uploaded_at": uploaded_at,
            "gps_accuracy_m": meta.get("gps_accuracy_m"),
            "captured_tz": meta.get("captured_tz") or "",
        })

    def _payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "evidence_type": self.evidence_type,
            "attachment_id": self.attachment_id.id,
            "name": self.attachment_id.name,
            "url": f"/web/content/{self.attachment_id.id}",
            "stop_id": self.stop_id.id,
            "job_id": self.job_id.id,
            "pallet_id": self.pallet_id.id,
            "captured_at": self.captured_at.isoformat() if self.captured_at else "",
            "lat": self.lat,
            "lng": self.lng,
            "device": self.device,
            "scan_session": self.scan_session or "",
            # §12 envelope
            "load_plan_id": self.load_plan_id.id,
            "upload_state": self.upload_state,
            "idempotency_key": self.idempotency_key,
            "original_filename": self.original_filename,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else "",
            "gps_accuracy_m": self.gps_accuracy_m,
            "captured_tz": self.captured_tz,
            "superseded_by_id": self.superseded_by_id.id,
            "invoice_id": self.invoice_id.id,
        }

    def _supersede(self, old_attachment, new_evidence):
        """Spec §55 retake: the retaken photo REPLACES the old one.

        The old record and its attachment are KEPT for audit — the old
        row's superseded_by_id points at the replacement, so the live map
        and dispatch views count it as superseded (never as live proof).
        But the old attachment leaves every driver-visible bucket (stop
        POP/POD m2m, pallet POPP bucket, pallet evidence list) and its
        invoice/quote copy is dropped, so the app and the customer's
        invoice show only the live proof. Returns the rows superseded.

        Idempotent: a retry that names an already-superseded row only
        re-detaches buckets, it never re-points the chain."""
        old_evs = self.sudo().search([
            ("attachment_id", "=", old_attachment.id),
            ("id", "!=", new_evidence.id),
        ])
        for old in old_evs:
            if not old.superseded_by_id:
                old.write({"superseded_by_id": new_evidence.id})
            att = old.attachment_id
            if att:
                stop = old.stop_id
                if stop:
                    if att.id in stop.pop_attachment_ids.ids:
                        stop.write({"pop_attachment_ids": [(3, att.id)]})
                    if att.id in stop.pod_attachment_ids.ids:
                        stop.write({"pod_attachment_ids": [(3, att.id)]})
                    stop.job_id.item_ids.filtered(
                        lambda item: att.id in item.evidence_attachment_ids.ids
                    ).write({"evidence_attachment_ids": [(3, att.id)]})
                item = old.pallet_id
                if item and att.id in item.popp_attachment_ids.ids:
                    item.write({"popp_attachment_ids": [(3, att.id)]})
                # Replace the invoice/quote copy so only the new proof shows.
                tag = f"__evidence_source:{att.id}__"
                self.env["ir.attachment"].search(
                    [("description", "=", tag)]).unlink()
        return old_evs

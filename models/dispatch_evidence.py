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
    def _create_evidence(self, attachment, stop, ev_type, meta=None):
        """Create the canonical record for an attachment just uploaded by
        a driver. `meta` may carry captured_at / lat / lng / device /
        scan_session / scan_page_index / pallet_id from the app.

        Called from driver_add_evidence (already auth'd for the stop), so
        sudo() keeps this independent of the evidence model's own rules."""
        meta = meta or {}
        if not isinstance(meta, dict):
            meta = {}
        job = stop.job_id
        # prema_logistics_booking adds logistics_booking_id to
        # prema.dispatch.job — but its models load AFTER dispatch in the
        # graph (booking depends on dispatch), so at_install tests can run
        # before the field exists. Same guard pattern as dispatch_stop.py.
        booking = job.logistics_booking_id if "logistics_booking_id" in job._fields else False
        return self.sudo().create({
            "attachment_id": attachment.id,
            "evidence_type": ev_type,
            "stop_id": stop.id,
            "job_id": job.id,
            "booking_id": booking.id if booking else False,
            "pallet_id": meta.get("pallet_id") or False,
            "driver_id": self.env.user.id,
            "captured_at": meta.get("captured_at") or False,
            "lat": meta.get("lat"),
            "lng": meta.get("lng"),
            "device": meta.get("device") or "",
            "checksum_sha256": meta.get("checksum_sha256") or "",
            "scan_session": meta.get("scan_session") or False,
            "scan_page_index": meta.get("scan_page_index") or 0,
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
        }

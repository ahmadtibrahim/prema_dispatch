# -*- coding: utf-8 -*-
"""Append-only custody log — one row per thing that happened to a pallet.

Why append-only rather than a set of timestamps on the package: a warehouse
argues about times. "It arrived at 09:40, not 14:00" has to be answerable with
who changed it, when, and why. A field that is simply overwritten answers none
of those questions and cannot be audited afterwards.

So the package's `pfl_received_at` / `pfl_staged_at` / `pfl_released_at` are a
*cache* of this log, recomputed from it. A correction is a new event of the
same type carrying `corrects_id` and a `reason`; the original row is never
edited or deleted, and the cache follows the newest event for each moment.

The one thing this model must never do is restart custody time on an internal
move. Moving a pallet writes a `moved` event, which is not one of the moments
the cache reads, so storage keeps running from the original receipt.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

#: The three moments the storage charge depends on. A `moved` event is
#: deliberately not one of them.
MOMENT_EVENTS = ("received", "staged", "released")

EVENT_TYPES = [
    ("received", "Received into custody"),
    ("moved", "Moved internally"),
    ("staged", "Prepared for release"),
    ("hold", "Placed on hold"),
    ("hold_released", "Hold released"),
    ("released", "Released from custody"),
    ("damage", "Damage / condition recorded"),
]


class PremaPflCustodyEvent(models.Model):
    _name = "prema.pfl.custody.event"
    _description = "PF Logistics Pallet Custody Event"
    _order = "occurred_at desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)

    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet", required=True,
        ondelete="restrict", index=True)
    job_id = fields.Many2one(
        "prema.pfl.warehouse.job", string="Warehouse Job",
        related="package_id.pfl_job_id", store=True, index=True)
    event_type = fields.Selection(EVENT_TYPES, required=True, index=True)
    occurred_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
        help="When it actually happened — which is not necessarily when it was "
             "typed in. A late entry is recorded with its true time, and the "
             "difference is visible rather than hidden.")
    user_id = fields.Many2one(
        "res.users", string="Operator", required=True,
        default=lambda self: self.env.user, index=True)
    from_location_id = fields.Many2one("stock.location", string="From Location")
    to_location_id = fields.Many2one("stock.location", string="To Location")
    picking_id = fields.Many2one(
        "stock.picking", string="Stock Movement", ondelete="set null")
    notes = fields.Text()

    # ── corrections ────────────────────────────────────────────────────
    corrects_id = fields.Many2one(
        "prema.pfl.custody.event", string="Corrects",
        ondelete="restrict", index=True,
        help="Set when this event replaces an earlier one of the same type. "
             "The earlier row is kept, so the recorded history is 'was X, "
             "corrected to Y because Z' rather than just 'Y'.")
    reason = fields.Text(
        string="Reason",
        help="Required on a correction. Not required on a first entry.")
    corrected_by_ids = fields.One2many(
        "prema.pfl.custody.event", "corrects_id", string="Corrections")
    is_corrected = fields.Boolean(
        compute="_compute_is_corrected", string="Since Corrected")

    attachment_ids = fields.Many2many(
        "ir.attachment", "prema_pfl_event_attachment_rel",
        "event_id", "attachment_id", string="Photos & Documents")

    @api.depends("package_id", "event_type", "occurred_at")
    def _compute_display_name(self):
        labels = dict(EVENT_TYPES)
        for event in self:
            event.display_name = "%s — %s" % (
                event.package_id.name or _("Pallet"),
                labels.get(event.event_type, event.event_type or ""))

    @api.depends("corrected_by_ids")
    def _compute_is_corrected(self):
        for event in self:
            event.is_corrected = bool(event.corrected_by_ids)

    @api.constrains("event_type", "corrects_id")
    def _check_correction_shape(self):
        for event in self:
            if event.corrects_id and event.corrects_id.event_type != event.event_type:
                raise UserError(_(
                    "A correction must be of the same kind as the event it "
                    "corrects (%(original)s).",
                    original=event.corrects_id.event_type))

    # ── append-only ────────────────────────────────────────────────────
    def write(self, vals):
        """Refuse to rewrite an event. Record a new one instead.

        `attachment_ids` is the single exception: adding a photograph to an
        event that already happened does not change what happened. Everything
        else — the time, the operator, the type, the location, the reason — is
        frozen the moment the row exists.
        """
        mutable = {"attachment_ids"}
        if set(vals) - mutable:
            raise UserError(_(
                "Custody events are append-only. This event cannot be edited "
                "(%(fields)s). Record a correction instead — it keeps the "
                "original and the reason, which an edit would destroy.",
                fields=", ".join(sorted(set(vals) - mutable))))
        return super().write(vals)

    def unlink(self):
        raise UserError(_(
            "Custody events cannot be deleted — they are the warehouse's "
            "record of what happened to the freight. Record a correction if "
            "an entry is wrong."))

    # ── the cache that reads this log ──────────────────────────────────
    @api.model
    def _pfl_latest_moments(self, package):
        """Return the authoritative time for each moment of `package`.

        The newest row wins, where "newest" is by when it was *entered*, so a
        correction recorded today about last Tuesday correctly overrides the
        original entry while the original stays on the record.
        """
        self.flush_model(["occurred_at", "event_type", "package_id", "create_date"])
        moments = {}
        for event_type in MOMENT_EVENTS:
            event = self.search(
                [("package_id", "=", package.id), ("event_type", "=", event_type)],
                order="create_date desc, id desc", limit=1)
            if event:
                moments[event_type] = event
        return moments

    @api.model
    def _pfl_recompute_package(self, package):
        """Rebuild the pallet's cached moments and custody state from its log."""
        moments = self._pfl_latest_moments(package)
        received = moments.get("received")
        staged = moments.get("staged")
        released = moments.get("released")

        vals = {
            "pfl_received_at": received.occurred_at if received else False,
            "pfl_received_by": received.user_id.id if received else False,
            "pfl_staged_at": staged.occurred_at if staged else False,
            "pfl_staged_by": staged.user_id.id if staged else False,
            "pfl_released_at": released.occurred_at if released else False,
            "pfl_released_by": released.user_id.id if released else False,
        }

        # State is derived, never hand-set, so a correction to the release time
        # cannot leave the pallet claiming a state its log does not support.
        if released:
            state = "released"
        elif staged:
            state = "staged"
        elif received:
            state = "received"
        else:
            state = "expected"
        vals["pfl_state"] = state
        package.sudo().write(vals)
        return vals

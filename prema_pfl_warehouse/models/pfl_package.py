# -*- coding: utf-8 -*-
"""One physical pallet is one `stock.quant.package`.

This is the whole reason the module does not introduce a pallet ledger of its
own. Odoo already identifies a package, already tracks where it is, already
moves it on pickings, and already counts it in `stock.quant`. Adding a parallel
"pallet" record would create a second quantity authority that could disagree
with the first — exactly the failure the audit warned about.

So the package keeps its native meaning and gains only what a warehouse needs
and Odoo does not model:

* an opaque public token (the QR payload),
* the actual freight owner, kept separately from the billing customer,
* the four custody moments a storage charge depends on — received, staged,
  released, and hold — each with the operator who recorded it,
* the customer's own barcode, as an alias rather than as the identity.

Planned dates are deliberately absent. A pallet has no "scheduled receipt
time": it has the time it actually arrived, recorded by the person who took
it in. That asymmetry is what stops a plan from becoming evidence.
"""
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

#: Custody ladder. `expected` is a pallet the job knows about but has not
#: physically taken in yet; `received` is the honest state afterwards, because
#: a cross-docked pallet is received and never stored, and inventing a separate
#: "in storage" state would claim something the record cannot know.
PFL_STATES = [
    ("expected", "Expected"),
    ("received", "Received"),
    ("staged", "Staged (awaiting pickup)"),
    ("released", "Released"),
]


class StockQuantPackage(models.Model):
    _inherit = "stock.quant.package"

    # ── identity ───────────────────────────────────────────────────────
    pfl_job_id = fields.Many2one(
        "prema.pfl.warehouse.job", string="Warehouse Job",
        ondelete="set null", index=True,
        help="The warehouse job this pallet belongs to. Empty for packages "
             "that are not customer freight (the existing MoonVella and "
             "owned-stock packages are untouched by this module).")
    pfl_qr_token = fields.Char(
        string="Pallet QR Token", copy=False, index=True,
        default=lambda self: str(uuid.uuid4()),
        help="Opaque identifier printed as the pallet's QR code. It carries no "
             "customer name, no source and no price — resolving it to a record "
             "requires an authorised Odoo session.")
    pfl_owner_id = fields.Many2one(
        "res.partner", string="Freight Owner", index=True,
        help="Who the goods actually belong to. Deliberately separate from the "
             "job's billing customer: the party that pays is not always the "
             "party that owns the freight.")
    pfl_state = fields.Selection(
        PFL_STATES, string="Custody State", default="expected", index=True,
        )

    # ── custody moments (actual, never planned) ────────────────────────
    pfl_received_at = fields.Datetime(
        string="Received At", readonly=True, copy=False,
        help="When the pallet physically entered our custody. Storage is "
             "counted from this instant — not from the scheduled arrival, and "
             "not from when the entry was typed in.")
    pfl_received_by = fields.Many2one(
        "res.users", string="Received By", readonly=True, copy=False)
    pfl_staged_at = fields.Datetime(
        string="Prepared At", readonly=True, copy=False,
        help="When the pallet was made available for pickup. This is NOT a "
             "release: storage keeps accruing while a pallet sits staged.")
    pfl_staged_by = fields.Many2one(
        "res.users", string="Prepared By", readonly=True, copy=False)
    pfl_released_at = fields.Datetime(
        string="Released At", readonly=True, copy=False,
        help="When the pallet physically left our custody. Storage stops here, "
             "for this pallet only.")
    pfl_released_by = fields.Many2one(
        "res.users", string="Released By", readonly=True, copy=False)

    # ── hold ───────────────────────────────────────────────────────────
    pfl_on_hold = fields.Boolean(string="On Hold", copy=False)
    pfl_hold_reason = fields.Text(string="Hold Reason", copy=False)
    pfl_hold_by = fields.Many2one(
        "res.users", string="Held By", readonly=True, copy=False)
    pfl_hold_at = fields.Datetime(
        string="Held At", readonly=True, copy=False)

    # ── labelling ──────────────────────────────────────────────────────
    pfl_label_mode = fields.Selection(
        [("standard", "Label may be attached"),
         ("no_attached_label", "Customer prohibits an attached label")],
        string="Label Handling", default="standard", required=True,
        help="When a customer prohibits an attached label the pallet is "
             "identified by an off-pallet card and an exclusive position "
             "instead; the movement log is identical either way.")
    pfl_card_id = fields.Many2one(
        "prema.pfl.card", string="Identification Card", copy=False,
        ondelete="set null",
        help="The off-pallet card used when no label may be attached.")
    pfl_alias_ids = fields.One2many(
        "prema.pfl.barcode.alias", "package_id", string="Customer Barcodes")
    pfl_event_ids = fields.One2many(
        "prema.pfl.custody.event", "package_id", string="Custody Events")

    # ── computed conveniences ──────────────────────────────────────────
    pfl_custody_hours = fields.Float(
        string="Custody Hours", compute="_compute_pfl_custody_hours",
        help="Actual hours in custody: from the recorded receipt to the "
             "recorded release, or to now while the pallet is still here.")
    pfl_location_id = fields.Many2one(
        "stock.location", string="Current Location", compute="_compute_pfl_location")

    _sql_constraints = [
        ("pfl_qr_token_unique", "unique(pfl_qr_token)",
         "The pallet QR token must be unique."),
    ]

    @api.depends("pfl_received_at", "pfl_released_at")
    def _compute_pfl_custody_hours(self):
        now = fields.Datetime.now()
        for package in self:
            if not package.pfl_received_at:
                package.pfl_custody_hours = 0.0
                continue
            end = package.pfl_released_at or now
            delta = end - package.pfl_received_at
            package.pfl_custody_hours = delta.total_seconds() / 3600.0

    @api.depends("quant_ids", "quant_ids.location_id")
    def _compute_pfl_location(self):
        for package in self:
            package.pfl_location_id = package.quant_ids[:1].location_id

    # ── guards ─────────────────────────────────────────────────────────
    def _pfl_check_not_released(self, action):
        released = self.filtered(lambda p: p.pfl_state == "released")
        if released:
            raise UserError(_(
                "%(action)s is not possible: pallet(s) %(names)s have already "
                "been released and have left the warehouse.",
                action=action,
                names=", ".join(released.mapped("name"))))

    def _pfl_ensure_available(self, action):
        """A held pallet cannot move on, but it can still be inspected."""
        held = self.filtered("pfl_on_hold")
        if held:
            raise UserError(_(
                "%(action)s is blocked: pallet(s) %(names)s are on hold. "
                "Release the hold first — the reason is recorded on the pallet.",
                action=action,
                names=", ".join(held.mapped("name"))))

    # ── scanning ───────────────────────────────────────────────────────
    def _pfl_qr_data_uri(self):
        """This pallet's QR code as an inline `data:` URI.

        Rendered here rather than through `/report/barcode/...` on purpose.
        That route draws with reportlab's `renderPM` backend, which this
        environment does not ship — so it answers 500, wkhtmltopdf drops the
        image, and the label prints *without a QR while still looking
        finished*. A label nobody can scan is worse than no label. Generating
        the PNG directly needs no server round-trip and no renderer, which is
        also what makes the printed code verifiable offline.
        """
        self.ensure_one()
        import base64
        from io import BytesIO

        import qrcode
        image = qrcode.make(self.pfl_qr_token, box_size=10, border=2)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64,%s" % base64.b64encode(
            buffer.getvalue()).decode()

    def action_pfl_print_label(self):
        """Print the QR label. A normal PDF — no IoT box, no label service."""
        self.ensure_one()
        report = self.env.ref(
            "prema_pfl_warehouse.action_report_pfl_pallet_label")
        return report.report_action(self)

    @api.model
    def _pfl_resolve_scan(self, code, customer=None):
        """Resolve a scanned code to a pallet — or say why it cannot.

        Two kinds of code arrive at the same input: our own opaque QR token, and
        whatever barcode the customer stuck on the pallet. The token is unique
        and answers immediately. A customer barcode may legitimately match more
        than one pallet across customers, so when the answer is ambiguous this
        refuses rather than picking one — guessing here is how two pallets
        become one record.

        Returns a dict: `status` is one of `ok`, `ambiguous`, `not_found`.
        """
        code = (code or "").strip()
        if not code:
            return {"status": "not_found", "code": code}
        package = self.search([("pfl_qr_token", "=", code)], limit=1)
        if package:
            return {"status": "ok", "source": "qr_token", "package": package}
        # A job reference plus a pallet reference is also a legitimate scan.
        package = self.search([("name", "=", code)], limit=1)
        if package and package.pfl_job_id:
            return {"status": "ok", "source": "package_name", "package": package}
        matches = self.env["prema.pfl.barcode.alias"]._pfl_resolve(code, customer)
        if len(matches) == 1:
            return {"status": "ok", "source": "customer_barcode",
                    "package": matches}
        if len(matches) > 1:
            return {"status": "ambiguous", "code": code, "packages": matches}
        return {"status": "not_found", "code": code}

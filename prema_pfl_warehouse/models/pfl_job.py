# -*- coding: utf-8 -*-
"""The warehouse job — the operational header over records that already exist.

The job is deliberately thin. It does not own the pallets (the native
`stock.quant.package` does), it does not own the quantities (native stock moves
do), and it does not own the commercial agreement (the accepted sale order
does). What it owns is the *sequence of custody*: this freight was taken in
here, by this person, at this time; it sat for this long; it left then.

Two consequences of that shape are worth stating, because they are the ones
that get violated in a hurry:

* **A booking is optional.** The job can be opened straight from an accepted
  sale order. Transportation is a separate workflow that may attach later; a
  cross-dock customer who collects with their own truck never has one.

* **Nothing here recomputes the past.** Receiving writes a fact, not a plan.
  Internal moves write a fact that storage deliberately ignores. Only a
  correction — with a reason, by a named operator — can change a recorded
  moment, and even then the original stays in the log.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..service_map import SERVICES_BY_MODE, freight_product

STATE_SELECTION = [
    ("draft", "New"),
    ("confirmed", "Confirmed — rates accepted"),
    ("storing", "In Custody"),
    ("prepared", "Prepared for Release"),
    ("released", "Released"),
    ("cancelled", "Cancelled"),
]

#: The roles that may run a warehouse operation. This mirrors the phone
#: controller's gate on purpose: a model method reachable over RPC must refuse
#: exactly what the HTTP route refuses, or the route's gate is decoration.
#: `sudo` (a module running as root) is always allowed — it is not a person.
OPERATOR_GROUPS = (
    "prema_dispatch.group_dispatch_warehouse",
    "prema_dispatch.group_dispatch_manager",
    "prema_dispatch.group_dispatcher",
    "base.group_system",
)

#: The operation types a warehouse job is allowed to drive. Anything else —
#: the main `WH` receipts, a manufacturing type, a MoonVella shipping type —
#: is outside this job's authority and is refused rather than silently used.
AUTHORIZED_SEQUENCE_CODES = ("PFL-IN", "PFL-INT", "PFL-OUT")

#: The prefix every authorized warehouse location barcode carries, plus the
#: native customer location used as the source of a receipt and the
#: destination of a release.
AUTHORIZED_LOCATION_PREFIX = "PFL-WH"


class PremaPflWarehouseJob(models.Model):
    _name = "prema.pfl.warehouse.job"
    _description = "PF Logistics Warehouse Job"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "name desc"

    # ── identity ───────────────────────────────────────────────────────
    name = fields.Char(
        string="Job Reference", required=True, copy=False, readonly=True,
        default=lambda self: _("New"), index=True)

    # ── parties (kept apart on purpose) ────────────────────────────────
    partner_id = fields.Many2one(
        "res.partner", string="Billing Customer", required=True,
        tracking=True, index=True,
        help="Who is invoiced.")
    freight_owner_id = fields.Many2one(
        "res.partner", string="Freight Owner", required=True,
        tracking=True, index=True,
        help="Who the goods belong to. Preserved on every stock move and quant. "
             "The billing customer is not assumed to own the freight — a "
             "forwarder bill-to with a consignee owner is normal.")

    # ── commercial links ───────────────────────────────────────────────
    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order", ondelete="set null", index=True)
    booking_id = fields.Many2one(
        "logistics.booking", string="Transport Booking", ondelete="set null",
        index=True,
        help="Optional. A warehouse job does not need a transport booking, and "
             "never creates one.")
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Warehouse", required=True,
        default=lambda self: self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1))

    # ── operational header ─────────────────────────────────────────────
    service_mode = fields.Selection(
        [("ltl", "LTL — per pallet handling"), ("ftl", "FTL — one fee per job")],
        required=True, default="ltl", tracking=True,
        help="Decides which handling charge applies. It is set before the work "
             "starts so the customer cannot be billed both ways afterwards.")
    label_restriction = fields.Selection(
        [("standard", "Label may be attached"),
         ("no_attached_label", "Customer prohibits an attached label")],
        required=True, default="standard",
        help="Applied to every pallet on this job when it is created.")
    expected_pallets = fields.Integer(
        string="Expected Pallets",
        help="What the customer expects to send. Informational: the quantity "
             "that is charged is the pallets actually taken in, never this.")
    customer_reference = fields.Char()
    bol_number = fields.Char(string="BOL #")
    arrival_note = fields.Text(string="Expected Arrival / Notes")
    notes = fields.Html()

    # ── service actuals (recorded, not derived) ────────────────────────
    wrapping_pallets = fields.Integer(
        string="Pallets Wrapped", default=0,
        help="Pallets actually shrink-wrapped. Wrapping is billed at this "
             "quantity, not at the number of pallets received.")
    pallets_supplied = fields.Integer(
        string="Pallets Supplied", default=0,
        help="Company pallets actually supplied to the customer.")
    labeling_pallets = fields.Integer(
        string="Pallets Labelled", default=0,
        help="Pallets actually labelled at an agreed rate.")

    # ── state ──────────────────────────────────────────────────────────
    state = fields.Selection(
        STATE_SELECTION, default="draft", required=True, tracking=True, index=True)
    rates_frozen_at = fields.Datetime(readonly=True, copy=False)
    release_prepared_at = fields.Datetime(readonly=True, copy=False)

    # ── relations ──────────────────────────────────────────────────────
    package_ids = fields.One2many(
        "stock.quant.package", "pfl_job_id", string="Pallets")
    rate_ids = fields.One2many("prema.pfl.rate", "job_id", string="Accepted Rates")
    charge_ids = fields.One2many("prema.pfl.charge", "job_id", string="Charges")
    labour_ids = fields.One2many("prema.pfl.labour", "job_id", string="Labour")
    extra_ids = fields.One2many("prema.pfl.extra", "job_id", string="Agreed Extras")
    event_ids = fields.One2many(
        "prema.pfl.custody.event", "job_id", string="Custody Events")
    invoice_ids = fields.One2many(
        "account.move", "prema_pfl_job_id", string="Warehouse Invoices")

    # ── counters ───────────────────────────────────────────────────────
    package_count = fields.Integer(compute="_compute_counters")
    received_count = fields.Integer(compute="_compute_counters")
    released_count = fields.Integer(compute="_compute_counters")
    on_hold_count = fields.Integer(compute="_compute_counters")
    uninvoiced_charge_count = fields.Integer(compute="_compute_counters")
    charge_total = fields.Monetary(
        compute="_compute_counters", currency_field="currency_id")
    uninvoiced_total = fields.Monetary(
        compute="_compute_counters", currency_field="currency_id")
    draft_invoice_id = fields.Many2one(
        "account.move", compute="_compute_counters", string="Open Draft Invoice")

    @api.depends("package_ids", "package_ids.pfl_state", "package_ids.pfl_on_hold",
                 "charge_ids", "charge_ids.invoiced", "charge_ids.amount",
                 "invoice_ids", "invoice_ids.state")
    def _compute_counters(self):
        for job in self:
            packages = job.package_ids
            job.package_count = len(packages)
            job.received_count = len(
                packages.filtered(lambda p: p.pfl_received_at))
            job.released_count = len(
                packages.filtered(lambda p: p.pfl_released_at))
            job.on_hold_count = len(packages.filtered("pfl_on_hold"))
            job.charge_total = sum(job.charge_ids.mapped("amount"))
            job.uninvoiced_total = sum(
                job.charge_ids.filtered(lambda c: not c.invoiced).mapped("amount"))
            job.uninvoiced_charge_count = len(
                job.charge_ids.filtered(lambda c: not c.invoiced))
            job.draft_invoice_id = job.invoice_ids.filtered(
                lambda m: m.state == "draft")[:1]

    # ── creation ───────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "prema.pfl.warehouse.job") or _("New")
            if not vals.get("freight_owner_id") and vals.get("partner_id"):
                vals["freight_owner_id"] = vals["partner_id"]
        return super().create(vals_list)

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        """Offer the billing customer as the owner, but never force it."""
        for job in self:
            if not job.freight_owner_id:
                job.freight_owner_id = job.partner_id

    @api.constrains("sale_order_id", "partner_id")
    def _check_sale_order_partner(self):
        for job in self:
            if job.sale_order_id and job.sale_order_id.partner_id != job.partner_id:
                raise ValidationError(_(
                    "The job's billing customer must be the customer on sales "
                    "order %(order)s. A warehouse job bills the party that "
                    "accepted the quote.",
                    order=job.sale_order_id.name))

    # ── location resolution ────────────────────────────────────────────
    def _pfl_location(self, barcode):
        location = self.env["stock.location"].with_context(
            active_test=False).search([("barcode", "=", barcode)], limit=1)
        if not location:
            raise UserError(_(
                "Warehouse location %(barcode)s is missing. The PF Logistics "
                "warehouse configuration is not complete.", barcode=barcode))
        return location

    def _pfl_customer_location(self):
        return self.env.ref("stock.stock_location_customers")

    # ── authorisation ──────────────────────────────────────────────────
    def _pfl_require_operator(self):
        """The operator must hold a warehouse role.

        Deciding this *before* the stock documents are built is the whole
        point. `_pfl_transfer_one` runs under `sudo` to avoid granting the
        role the Inventory application; that elevation is only defensible if
        the right to perform the movement was already established here. A
        method reachable over RPC must refuse what the HTTP route refuses.
        """
        if self.env.su:
            return
        user = self.env.user
        if user._is_public() or not any(
                user.has_group(group) for group in OPERATOR_GROUPS):
            raise UserError(_(
                "This warehouse operation requires a warehouse role. Sign in "
                "with a warehouse, dispatcher or manager account."))

    def _pfl_authorized_location_ids(self):
        """The locations a warehouse job may move freight between.

        The PFL zones and cross-dock positions, plus the native customer
        location that is the source of a receipt and the destination of a
        release. The main warehouse's own locations are deliberately absent:
        customer freight does not belong in `WH/Stock`.
        """
        Location = self.env["stock.location"].with_context(active_test=False)
        zones = Location.search(
            [("barcode", "like", AUTHORIZED_LOCATION_PREFIX + "%")])
        customer = self.env.ref(
            "stock.stock_location_customers", raise_if_not_found=False)
        return set((zones | customer).ids)

    def _pfl_check_scope(self, packages, dest, sequence_code, src=None):
        """Everything that must be true before a stock document is created.

        Runs ahead of any `sudo`: the restricted environment is used only to
        *execute* a movement the operator was already entitled to make, never
        to widen what they may touch. Without this, one job could move or
        release another job's pallet, because the ACL that governs the job
        model is a model-wide grant — it says nothing about *which* job.
        """
        self.ensure_one()
        self._pfl_require_operator()

        if sequence_code not in AUTHORIZED_SEQUENCE_CODES:
            raise UserError(_(
                "Operation type %(code)s is not a warehouse operation this job "
                "may drive.", code=sequence_code))
        picking_type = self._pfl_picking_type(sequence_code)
        if picking_type.company_id and picking_type.company_id != self.company_id:
            raise UserError(_(
                "Warehouse operation type %(code)s belongs to another company.",
                code=sequence_code))

        for package in packages:
            if package.pfl_job_id != self:
                raise UserError(_(
                    "Pallet %(pallet)s belongs to warehouse job %(other)s, not "
                    "%(job)s. A job can only move its own freight.",
                    pallet=package.name,
                    other=package.pfl_job_id.name or _("(none)"),
                    job=self.name))
            if package.company_id and package.company_id != self.company_id:
                raise UserError(_(
                    "Pallet %(pallet)s belongs to another company (%(company)s).",
                    pallet=package.name, company=package.company_id.name))
            if package.pfl_owner_id and package.pfl_owner_id != self.freight_owner_id:
                raise UserError(_(
                    "Pallet %(pallet)s is owned by %(owner)s, but job %(job)s "
                    "is for freight owner %(job_owner)s.",
                    pallet=package.name,
                    owner=package.pfl_owner_id.name,
                    job=self.name,
                    job_owner=self.freight_owner_id.name or _("(none)")))

        authorized = self._pfl_authorized_location_ids()
        checked = [(dest, _("destination"))]
        if src:
            checked.append((src, _("source")))
        for package in packages:
            if package.pfl_location_id:
                checked.append((package.pfl_location_id, _("current location")))
        for location, role in checked:
            if location.id not in authorized:
                raise UserError(_(
                    "The %(role)s %(location)s is outside the PF Logistics "
                    "warehouse. Customer freight moves only between the "
                    "warehouse zones and the customer.",
                    role=role, location=location.display_name))

    # ── state machine ──────────────────────────────────────────────────
    def action_pfl_confirm(self):
        """Freeze the accepted rates. Nothing may be received before this.

        Confirming is what turns "the catalogue says $12" into "this job is at
        $12". Doing it before the first pallet arrives is the point: a rate
        agreed after the work is a rate negotiated under pressure.
        """
        for job in self:
            if job.state != "draft":
                raise UserError(_("%s is already confirmed.", job.name))
            if not job.rate_ids:
                from .pfl_rate import build_rate_vals
                job.rate_ids = build_rate_vals(self.env, job)
            missing = [code for code in SERVICES_BY_MODE[job.service_mode]
                       if not job._pfl_rate_for(code)
                       and code not in ("extra",)]
            if missing:
                raise UserError(_(
                    "No accepted rate for: %(services)s. The service products "
                    "must exist before a job can be confirmed.",
                    services=", ".join(missing)))
            job.write({
                "state": "confirmed",
                "rates_frozen_at": fields.Datetime.now(),
            })
            job.message_post(body=_(
                "Rates accepted and frozen for %(mode)s handling.",
                mode=dict(job._fields["service_mode"].selection)[job.service_mode]))
        return True

    def action_pfl_cancel(self):
        for job in self:
            if job.package_ids.filtered(lambda p: p.pfl_received_at):
                raise UserError(_(
                    "%s has freight in custody. Release the pallets before "
                    "cancelling the job.", job.name))
            job.state = "cancelled"
        return True

    def _pfl_rate_for(self, service):
        return self.rate_ids.filtered(lambda r: r.service == service)[:1]

    def _pfl_require_confirmed(self, action):
        for job in self:
            if job.state in ("draft", "cancelled"):
                raise UserError(_(
                    "%(action)s needs an accepted job. Confirm %(job)s first so "
                    "the rates are frozen before the work starts.",
                    action=action, job=job.name))

    def _pfl_sync_state(self):
        """Derive the job state from what the pallets actually did."""
        for job in self:
            if job.state in ("draft", "cancelled"):
                continue
            packages = job.package_ids
            received = packages.filtered(lambda p: p.pfl_received_at)
            released = packages.filtered(lambda p: p.pfl_released_at)
            if packages and len(released) == len(packages):
                state = "released"
            elif packages and released:
                state = "released"
            elif job.release_prepared_at and received:
                state = "prepared"
            elif received:
                state = "storing"
            else:
                state = "confirmed"
            if job.state != state:
                job.state = state

    # ── pallets ────────────────────────────────────────────────────────
    def action_pfl_add_pallets(self, count=1, label_mode=None):
        """Create expected pallets up front so labels can be printed ahead."""
        self.ensure_one()
        self._pfl_require_confirmed(_("Adding pallets"))
        Package = self.env["stock.quant.package"]
        created = Package
        for _index in range(int(count)):
            created |= Package.create({
                "pfl_job_id": self.id,
                "pfl_owner_id": self.freight_owner_id.id,
                "pfl_label_mode": label_mode or self.label_restriction,
                "package_type_id": self.env.ref(
                    "prema_pfl_warehouse.pfl_package_type_pallet",
                    raise_if_not_found=False).id or False,
                "company_id": self.company_id.id,
            })
        return created

    def action_pfl_ensure_expected_pallets(self):
        """Make the job hold at least `expected_pallets` expected pallets."""
        self.ensure_one()
        missing = self.expected_pallets - len(self.package_ids)
        if missing > 0:
            self.action_pfl_add_pallets(missing)
        return True

    def _pfl_packages(self, packages=None):
        """The job's own pallets — refusing anything that is not.

        Silently dropping a pallet that belongs to another job would turn
        "release these" into a no-op that looks like success: no error, and no
        pallet released. The refusal names the pallet so the operator knows the
        action did not happen.
        """
        self.ensure_one()
        if packages is None:
            return self.package_ids
        if isinstance(packages, int):
            packages = self.package_ids.browse(packages)
        foreign = packages.filtered(lambda p: p.pfl_job_id != self)
        if foreign:
            raise UserError(_(
                "Pallet(s) %(pallets)s belong to another warehouse job, so "
                "%(job)s cannot act on them.",
                pallets=", ".join(foreign.mapped("name")), job=self.name))
        return packages

    # ── stock integration ──────────────────────────────────────────────
    def _pfl_picking_type(self, sequence_code):
        picking_type = self.env["stock.picking.type"].search(
            [("sequence_code", "=", sequence_code)], limit=1)
        if not picking_type:
            raise UserError(_(
                "The warehouse operation type %(code)s is missing. The PF "
                "Logistics configuration is not complete.", code=sequence_code))
        return picking_type

    def _pfl_transfer(self, packages, dest, sequence_code, event_type,
                      src=None, when=None, notes=None, set_result_package=True,
                      attachment_ids=None):
        """Move `packages` with real stock pickings, then log the fact.

        The picking is the quantity authority — it is what produces quants and
        what makes the pallet's location true. The custody event is the
        *narrative*: who did it and when. Neither replaces the other.

        Pallets are grouped by where they actually are, and one picking is
        created per group. Releasing two pallets that sit in different places
        through a single picking is not a shortcut — Odoo would be told the
        freight is somewhere it is not, and it refuses. The refusal is right,
        so the grouping happens here rather than being worked around.
        """
        self.ensure_one()
        if not packages:
            return self.env["stock.picking"]
        self._pfl_check_scope(packages, dest, sequence_code, src=src)
        pickings = self.env["stock.picking"]
        if src is not None:
            groups = [(src, packages)]
        else:
            by_location = {}
            for package in packages:
                by_location.setdefault(
                    package.pfl_location_id, self.env["stock.quant.package"])
                by_location[package.pfl_location_id] |= package
            groups = list(by_location.items())
        for group_src, group in groups:
            if not group_src:
                raise UserError(_(
                    "Pallet(s) %(names)s have no location, so they cannot be "
                    "moved. Receive them first.",
                    names=", ".join(group.mapped("name"))))
            pickings |= self._pfl_transfer_one(
                group, group_src, dest, sequence_code, event_type,
                when=when, notes=notes, set_result_package=set_result_package,
                attachment_ids=attachment_ids)
        self._pfl_sync_state()
        return pickings

    def _pfl_transfer_one(self, packages, src, dest, sequence_code, event_type,
                          when=None, notes=None, set_result_package=True,
                          attachment_ids=None):
        """One picking for pallets that all start from the same location.

        The stock documents are created and validated under `sudo`. That is not
        a shortcut around a permission: the permission was already decided —
        the caller holds a warehouse role, and the job's own access rules
        govern who may touch it. What `sudo` avoids is turning the warehouse
        role into an inventory clerk. Odoo's `action_confirm` reaches into
        orderpoints, reordering rules and the scheduler as it goes, so granting
        the role enough Inventory access to satisfy it would hand it far more
        than receiving freight — it would hand it the stock application.
        """
        if not packages:
            return self.env["stock.picking"]
        Picking = self.env["stock.picking"].sudo()
        Move = self.env["stock.move"].sudo()
        MoveLine = self.env["stock.move.line"].sudo()
        product = freight_product(self.env)
        if not product:
            raise UserError(_(
                "The customer-freight tracking product (PFL-CUST-PAL) is "
                "missing, so customer freight cannot be moved through stock. "
                "It is adopted, never recreated — restore it first."))
        if not product.is_storable:
            raise UserError(_(
                "%(product)s is not tracked in inventory, so moving it would "
                "produce no quants and the pallet would never become stock. "
                "Set it to be tracked before receiving customer freight.",
                product=product.display_name))
        picking_type = self._pfl_picking_type(sequence_code)
        when = when or fields.Datetime.now()
        picking = Picking.create({
            "picking_type_id": picking_type.id,
            "location_id": src.id,
            "location_dest_id": dest.id,
            "origin": self.name,
            "partner_id": self.freight_owner_id.id,
            "company_id": self.company_id.id,
        })
        move = Move.create({
            "name": "%s: %s" % (self.name, picking_type.name),
            "product_id": product.id,
            "product_uom_qty": float(len(packages)),
            "product_uom": product.uom_id.id,
            "picking_id": picking.id,
            "location_id": src.id,
            "location_dest_id": dest.id,
            "picking_type_id": picking_type.id,
            "company_id": self.company_id.id,
        })
        for package in packages:
            vals = {
                "move_id": move.id,
                "picking_id": picking.id,
                "product_id": product.id,
                "product_uom_id": product.uom_id.id,
                "quantity": 1.0,
                "location_id": src.id,
                "location_dest_id": dest.id,
                "company_id": self.company_id.id,
                # The owner travels with the goods on every movement, so the
                # quant — not just the job — knows whose freight this is.
                "owner_id": (package.pfl_owner_id or self.freight_owner_id).id,
            }
            if set_result_package:
                vals["package_id"] = package.id
                vals["result_package_id"] = package.id
            else:
                vals["result_package_id"] = package.id
            MoveLine.create(vals)

        picking.action_confirm()
        result = picking.with_context(
            skip_backorder=True, skip_sms=True).button_validate()
        if isinstance(result, dict) and result.get("res_model"):
            raise UserError(_(
                "The stock transfer for %(job)s needs attention in Odoo "
                "(%(model)s) — it was not completed.",
                job=self.name, model=result.get("res_model")))
        if picking.state != "done":
            raise UserError(_(
                "Stock transfer %(picking)s did not complete (state: "
                "%(state)s). The pallet movement was not recorded.",
                picking=picking.name, state=picking.state))

        for package in packages:
            package.sudo().write({"pfl_owner_id": (
                package.pfl_owner_id or self.freight_owner_id).id})
            event = self.env["prema.pfl.custody.event"].create({
                "package_id": package.id,
                "event_type": event_type,
                "occurred_at": when,
                "user_id": self.env.user.id,
                "from_location_id": src.id,
                "to_location_id": dest.id,
                "picking_id": picking.id,
                "notes": notes,
            })
            # The photograph belongs to the moment, not to the pallet in
            # general: "this is what it looked like when we took it in" and
            # "this is what it looked like when it left" are different claims.
            if attachment_ids:
                # No role may write a custody event — the log is append-only
                # by construction — so the operator's own photograph rides the
                # authority the movement was already checked for, exactly as
                # the stock documents do. `write()` still refuses every field
                # but `attachment_ids`.
                event.sudo().attachment_ids = [(6, 0, list(attachment_ids))]
            if event_type in ("received", "staged", "released"):
                self.env["prema.pfl.custody.event"]._pfl_recompute_package(package)
        self._pfl_sync_state()
        return picking

    # ── the six actions ────────────────────────────────────────────────
    def action_pfl_receive(self, packages=None, when=None, notes=None,
                           dest_barcode="PFL-WH-RECV", attachment_ids=None):
        """Take pallets into custody, recording the actual time and operator."""
        self.ensure_one()
        self._pfl_require_confirmed(_("Receiving"))
        packages = self._pfl_packages(packages).filtered(
            lambda p: not p.pfl_received_at)
        if not packages:
            return self.env["stock.picking"]
        return self._pfl_transfer(
            packages,
            src=self._pfl_customer_location(),
            dest=self._pfl_location(dest_barcode),
            sequence_code="PFL-IN",
            event_type="received",
            when=when,
            notes=notes,
            set_result_package=False,
            attachment_ids=attachment_ids,
        )

    def action_pfl_move(self, packages=None, dest_barcode=None, dest_location=None,
                        when=None, notes=None):
        """Move pallets between zones. This never restarts the storage clock."""
        self.ensure_one()
        self._pfl_require_confirmed(_("Moving pallets"))
        packages = self._pfl_packages(packages)
        if not packages:
            return self.env["stock.picking"]
        packages._pfl_ensure_available(_("Moving pallets"))
        packages._pfl_check_not_released(_("Moving pallets"))
        dest = dest_location or self._pfl_location(dest_barcode)
        # `src` is left to the transfer: the pallets may be in different zones,
        # and each group is moved from where it actually is.
        return self._pfl_transfer(
            packages, dest=dest, sequence_code="PFL-INT",
            event_type="moved", when=when, notes=notes)

    def action_pfl_hold(self, packages=None, reason=None):
        self.ensure_one()
        self._pfl_require_operator()
        packages = self._pfl_packages(packages).filtered(
            lambda p: not p.pfl_on_hold)
        if not packages:
            return False
        now = fields.Datetime.now()
        for package in packages:
            package.sudo().write({
                "pfl_on_hold": True,
                "pfl_hold_reason": reason,
                "pfl_hold_by": self.env.user.id,
                "pfl_hold_at": now,
            })
            self.env["prema.pfl.custody.event"].create({
                "package_id": package.id,
                "event_type": "hold",
                "occurred_at": now,
                "user_id": self.env.user.id,
                "notes": reason,
            })
        return True

    def action_pfl_release_hold(self, packages=None, reason=None):
        self.ensure_one()
        self._pfl_require_operator()
        packages = self._pfl_packages(packages).filtered("pfl_on_hold")
        if not packages:
            return False
        now = fields.Datetime.now()
        for package in packages:
            package.sudo().write({
                "pfl_on_hold": False,
                "pfl_hold_reason": False,
                "pfl_hold_by": False,
                "pfl_hold_at": False,
            })
            self.env["prema.pfl.custody.event"].create({
                "package_id": package.id,
                "event_type": "hold_released",
                "occurred_at": now,
                "user_id": self.env.user.id,
                "notes": reason,
            })
            self.env["prema.pfl.custody.event"]._pfl_recompute_package(package)
        return True

    def action_pfl_prepare_release(self, packages=None, when=None, notes=None,
                                   attachment_ids=None):
        """Stage pallets as available for pickup.

        Explicitly NOT a release, and explicitly not the end of storage. A
        pallet that has been walked to the dock and is waiting for a truck is
        still occupying our floor, and the customer is still paying for it.
        """
        self.ensure_one()
        self._pfl_require_confirmed(_("Preparing for release"))
        packages = self._pfl_packages(packages).filtered(
            lambda p: p.pfl_received_at and not p.pfl_released_at)
        if not packages:
            return self.env["stock.picking"]
        packages._pfl_ensure_available(_("Preparing for release"))
        picking = self._pfl_transfer(
            packages,
            dest=self._pfl_location("PFL-WH-OUT"),
            sequence_code="PFL-INT",
            event_type="staged",
            when=when,
            notes=notes,
            attachment_ids=attachment_ids,
        )
        if not self.release_prepared_at:
            self.release_prepared_at = when or fields.Datetime.now()
        self._pfl_sync_state()
        return picking

    def action_pfl_release(self, packages=None, when=None, notes=None,
                           attachment_ids=None):
        """Release pallets: the freight physically leaves our custody."""
        self.ensure_one()
        self._pfl_require_confirmed(_("Releasing"))
        packages = self._pfl_packages(packages).filtered(
            lambda p: p.pfl_received_at and not p.pfl_released_at)
        if not packages:
            return self.env["stock.picking"]
        packages._pfl_ensure_available(_("Releasing"))
        picking = self._pfl_transfer(
            packages,
            dest=self._pfl_customer_location(),
            sequence_code="PFL-OUT",
            event_type="released",
            when=when,
            notes=notes,
            attachment_ids=attachment_ids,
        )
        self._pfl_sync_state()
        return picking

    def action_pfl_record_damage(self, packages=None, notes=None, attachment_ids=None):
        """Record condition. Photos attach to the event, not to the job."""
        self.ensure_one()
        self._pfl_require_operator()
        packages = self._pfl_packages(packages)
        now = fields.Datetime.now()
        for package in packages:
            event = self.env["prema.pfl.custody.event"].create({
                "package_id": package.id,
                "event_type": "damage",
                "occurred_at": now,
                "user_id": self.env.user.id,
                "notes": notes,
            })
            if attachment_ids:
                # No role may write a custody event — the log is append-only
                # by construction — so the operator's own photograph rides the
                # authority the movement was already checked for, exactly as
                # the stock documents do. `write()` still refuses every field
                # but `attachment_ids`.
                event.sudo().attachment_ids = [(6, 0, list(attachment_ids))]
        return True

    def action_pfl_correct_moment(self, package, event_type, occurred_at,
                                  reason):
        """Correct a recorded moment without destroying the original."""
        self.ensure_one()
        if event_type not in ("received", "staged", "released"):
            raise UserError(_(
                "Only a receipt, a preparation or a release can be corrected."))
        if not reason:
            raise UserError(_(
                "A correction needs a reason. 'It was wrong' is not one — the "
                "reason is the only thing that makes the correction auditable."))
        package = self._pfl_packages(package)
        CustodyEvent = self.env["prema.pfl.custody.event"]
        previous = CustodyEvent.search(
            [("package_id", "=", package.id), ("event_type", "=", event_type)],
            order="create_date desc, id desc", limit=1)
        event = CustodyEvent.create({
            "package_id": package.id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "user_id": self.env.user.id,
            "corrects_id": previous.id or False,
            "reason": reason,
        })
        CustodyEvent._pfl_recompute_package(package)
        self._pfl_sync_state()
        return event

    def _pfl_open_job(self):
        """The action that opens this job's form — shared by every entry point."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Warehouse Job"),
            "res_model": "prema.pfl.warehouse.job",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    # ── button wrappers ────────────────────────────────────────────────
    def action_button_receive(self):
        for job in self:
            job.action_pfl_receive()
        return True

    def action_button_prepare_release(self):
        for job in self:
            job.action_pfl_prepare_release()
        return True

    def action_button_release(self):
        for job in self:
            job.action_pfl_release()
        return True

# -*- coding: utf-8 -*-
"""Warehouse pallet-position reservations.

A booking for N pallets reserves N specific positions for its appointment
window, buffered at both ends (15 minutes each by default). A reservation is
**planned space, not received inventory** — nothing here touches stock, creates
a quant or moves a pallet.

Two things make this safe to run from a customer-facing form:

* **The window is half-open.** ``[start, end)`` means a new reservation may
  begin at the instant the previous buffered window ends — "10:45–17:15" and
  "17:15–18:00" do not conflict.
* **The check is serialised.** Every candidate position row is locked
  (``SELECT … FOR UPDATE``) before availability is read, so two simultaneous
  submissions cannot both see the same free position. A PostgreSQL exclusion
  constraint on the table is the backstop if anything ever bypasses the ORM.

A position that physically holds a pallet stays occupied until that pallet
actually moves out: planned departure never clears real occupancy, and arriving
freight is never silently allocated into an occupied position.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

#: Position configuration lives on `stock.location` as manual (Studio) fields
#: so the warehouse can maintain it without a code change.
POSITION_FLAG = "x_pfl_is_position"
POSITION_ORDER = "x_pfl_allocation_order"
POSITION_CAPACITY = "x_pfl_capacity"
POSITION_BLOCKED = "x_pfl_blocked"
POSITION_CODE = "x_pfl_position_code"
POSITION_MAX_WEIGHT = "x_pfl_max_weight"

BUFFER_BEFORE_PARAM = "prema_logistics_booking.position_buffer_before_min"
BUFFER_AFTER_PARAM = "prema_logistics_booking.position_buffer_after_min"
DEFAULT_BUFFER_MINUTES = 15


class LogisticsPositionReservation(models.Model):
    _name = "logistics.position.reservation"
    _description = "Warehouse Pallet Position Reservation"
    _inherit = ["mail.thread"]
    _order = "date_start desc, id desc"

    name = fields.Char(default="New", readonly=True, copy=False)
    location_id = fields.Many2one(
        "stock.location", string="Position", required=True, index=True,
        ondelete="restrict",
        domain="[('usage', '=', 'internal')]")
    location_complete_name = fields.Char(related="location_id.complete_name")
    sale_order_id = fields.Many2one(
        "sale.order", string="Service Booking", index=True, ondelete="cascade")
    partner_id = fields.Many2one(
        "res.partner", string="Customer", index=True, ondelete="set null")
    picking_id = fields.Many2one(
        "stock.picking", string="Receipt", index=True, ondelete="set null")
    package_id = fields.Many2one(
        "stock.quant.package", string="Pallet ID", index=True,
        ondelete="set null")
    slot_index = fields.Integer(string="Pallet #")

    #: The buffered window this reservation owns, in UTC.
    date_start = fields.Datetime(required=True, index=True, tracking=True)
    date_end = fields.Datetime(required=True, index=True, tracking=True)
    #: The customer's actual appointment, kept for display.
    appointment_start = fields.Datetime(tracking=True)
    appointment_end = fields.Datetime(tracking=True)
    buffer_before = fields.Integer(default=DEFAULT_BUFFER_MINUTES)
    buffer_after = fields.Integer(default=DEFAULT_BUFFER_MINUTES)

    state = fields.Selection(
        [("reserved", "Reserved"), ("released", "Released"),
         ("cancelled", "Cancelled")],
        default="reserved", required=True, index=True, tracking=True)
    release_reason = fields.Char(readonly=True, copy=False)

    #: Set when the pallet physically left and the position is free again,
    #: independently of the planned window.
    physically_cleared = fields.Boolean(
        help="The pallet was checked out of this position. Planned departure "
             "never sets this — only an actual move does.")

    _sql_constraints = [
        ("logistics_position_reservation_window",
         "CHECK (date_end > date_start)",
         "A reservation must end after it starts."),
    ]

    @api.constrains("date_start", "date_end")
    def _check_window(self):
        for record in self:
            if record.date_start and record.date_end \
                    and record.date_end <= record.date_start:
                raise ValidationError(_("A reservation must end after it starts."))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.name in (False, "New"):
                record.name = "RSV/%s" % (record.id,)
        return records

    # ── window helpers ─────────────────────────────────────────────────
    @api.model
    def pfl_buffer_minutes(self):
        """Buffers are configurable in Settings; 15 minutes each by default."""
        params = self.env["ir.config_parameter"].sudo()
        before = params.get_param(BUFFER_BEFORE_PARAM, DEFAULT_BUFFER_MINUTES)
        after = params.get_param(BUFFER_AFTER_PARAM, DEFAULT_BUFFER_MINUTES)
        try:
            before = max(0, int(float(before)))
        except (TypeError, ValueError):
            before = DEFAULT_BUFFER_MINUTES
        try:
            after = max(0, int(float(after)))
        except (TypeError, ValueError):
            after = DEFAULT_BUFFER_MINUTES
        return before, after

    @api.model
    def pfl_buffered_window(self, arrival, departure):
        """The appointment widened by the configured buffers (UTC datetimes)."""
        from datetime import timedelta
        before, after = self.pfl_buffer_minutes()
        return (arrival - timedelta(minutes=before),
                departure + timedelta(minutes=after))

    @api.model
    def pfl_overlap_domain(self, start, end):
        """Half-open overlap: touching endpoints do NOT conflict."""
        return [("date_start", "<", end), ("date_end", ">", start)]

    # ── candidate positions ────────────────────────────────────────────
    @api.model
    def _pfl_position_fields(self):
        Location = self.env["stock.location"]
        return all(field in Location._fields
                   for field in (POSITION_FLAG, POSITION_ORDER, POSITION_BLOCKED))

    @api.model
    def pfl_candidate_positions(self):
        """Configured pallet positions, in allocation order.

        Only locations explicitly flagged as positions are considered — parent
        zones such as Receiving, Dry Storage or the A/B groups are not, so a
        zone can never be handed out as if it were one pallet space.
        """
        Location = self.env["stock.location"].sudo()
        if not self._pfl_position_fields():
            return Location.browse()
        domain = [(POSITION_FLAG, "=", True), ("usage", "=", "internal"),
                  ("active", "=", True)]
        order = "%s asc, complete_name asc, id asc" % POSITION_ORDER
        return Location.search(domain, order=order)

    def _pfl_occupied_location_ids(self):
        """Positions physically holding something right now.

        Occupancy is real stock on hand in the position — not the plan. A
        position whose pallet is due to leave tomorrow is still occupied today.
        """
        Pos = self.pfl_candidate_positions()
        if not Pos:
            return set()
        self.env.cr.execute("""
            SELECT DISTINCT q.location_id
              FROM stock_quant q
              JOIN stock_location l ON l.id = q.location_id
             WHERE q.location_id = ANY(%s)
               AND q.quantity > 0
               AND l.usage = 'internal'
        """, (list(Pos.ids),))
        return {row[0] for row in self.env.cr.fetchall()}

    def _pfl_lock_positions(self, position_ids):
        """Serialise concurrent allocations.

        Locking every candidate — not just the ones about to be taken — means
        two simultaneous bookings queue behind each other instead of both
        reading the same "free" list.
        """
        ids = sorted(set(position_ids))
        if not ids:
            return
        self.env.cr.execute(
            "SELECT id FROM stock_location WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids,))

    @api.model
    def pfl_availability(self, start, end, exclude_order=None):
        """Which positions can be promised for ``[start, end)``, and why not.

        Returns ``(available, blocked)`` where ``blocked`` maps a position to a
        short human reason, so the warehouse can be told what is in the way.
        """
        Reservation = self.env["logistics.position.reservation"].sudo()
        positions = self.pfl_candidate_positions()
        if not positions:
            return positions, {}

        occupied = self._pfl_occupied_location_ids()
        overlapping = Reservation.search(
            [("location_id", "in", positions.ids), ("state", "=", "reserved")]
            + Reservation.pfl_overlap_domain(start, end))
        if exclude_order:
            overlapping = overlapping.filtered(
                lambda r: r.sale_order_id.id != exclude_order.id)

        available = positions.browse()
        blocked = {}
        for position in positions:
            if position[POSITION_BLOCKED]:
                blocked[position] = _("out of service")
            elif position.id in occupied:
                blocked[position] = _("holds a pallet right now")
            elif position in overlapping.mapped("location_id"):
                blocker = overlapping.filtered(
                    lambda r: r.location_id.id == position.id)[:1]
                when = blocker.appointment_start or blocker.date_start
                blocked[position] = _(
                    "reserved until %s",
                    when.strftime("%Y-%m-%d %H:%M") if when else "—")
            else:
                available |= position
        return available, blocked

    @api.model
    def pfl_capacity_report(self, arrival, departure, count, exclude_order=None):
        """Lock the board and report what can be promised — without writing.

        This is what a customer-facing form calls *before* it creates anything.
        It takes the same row locks the allocation will need, so the positions
        it reports as free are still free when the booking is written a moment
        later in the same transaction.
        """
        count = int(count or 0)
        report = {"need": count, "got": 0, "ok": True, "message": "",
                  "start": False, "end": False, "available_ids": [],
                  "blocked": {}}
        if count <= 0:
            return report
        candidates = self.pfl_candidate_positions()
        start, end = self.pfl_buffered_window(arrival, departure)
        report["start"], report["end"] = start, end
        if not candidates:
            report.update({
                "ok": False, "message": _(
                    "No warehouse pallet positions are configured, so space "
                    "cannot be reserved for this booking.")})
            return report
        self._pfl_lock_positions(candidates.ids)
        available, blocked = self.pfl_availability(
            start, end, exclude_order=exclude_order)
        report["got"] = len(available)
        report["available_ids"] = available.ids
        report["blocked"] = {p.complete_name: r for p, r in blocked.items()}
        if len(available) < count:
            report["ok"] = False
            report["message"] = _(
                "The requested time lacks capacity for %(need)s pallets: only "
                "%(got)s position(s) are free between %(start)s and %(end)s "
                "(America/Toronto).%(detail)s",
                need=count, got=len(available),
                start=start.strftime("%Y-%m-%d %H:%M"),
                end=end.strftime("%Y-%m-%d %H:%M"),
                detail=(" " + "; ".join("%s: %s" % (name, reason)
                                        for name, reason
                                        in list(report["blocked"].items())[:5]))
                if report["blocked"] else "")
        return report

    # ── the allocation itself ──────────────────────────────────────────
    @api.model
    def pfl_reserve_positions(self, order, count, arrival, departure,
                              package_ids=None, picking=None, exclude_order=False):
        """Reserve ``count`` positions for ``order``'s buffered window.

        All-or-nothing: if the window cannot take the whole booking, nothing is
        written and a UserError explains what is missing. The caller is
        responsible for not leaving a half-accepted order behind.
        """
        if count <= 0:
            return self.browse()
        start, end = self.pfl_buffered_window(arrival, departure)
        before, after = self.pfl_buffer_minutes()

        candidates = self.pfl_candidate_positions()
        if not candidates:
            raise UserError(_(
                "No warehouse pallet positions are configured, so space cannot "
                "be reserved. Flag the physical positions in Inventory → "
                "Locations first."))
        self._pfl_lock_positions(candidates.ids)

        # The booking's OWN reservations are real blockers: a top-up must take a
        # DIFFERENT position, never re-claim one it already holds. Only a
        # reschedule, which moves a booking onto a new window, passes
        # exclude_order=True so it can keep the positions it already has.
        available, blocked = self.pfl_availability(
            start, end, exclude_order=order if exclude_order else None)
        if len(available) < count:
            detail = "; ".join(
                "%s: %s" % (pos.display_name, reason)
                for pos, reason in list(blocked.items())[:8])
            raise UserError(_(
                "There is not enough warehouse capacity for %(need)s pallets "
                "between %(start)s and %(end)s (America/Toronto). Only "
                "%(got)s position(s) are free.%(detail)s",
                need=count,
                start=start.strftime("%Y-%m-%d %H:%M"),
                end=end.strftime("%Y-%m-%d %H:%M"),
                got=len(available),
                detail=("\n" + detail) if detail else ""))

        chosen = available[:count]
        packages = self.env["stock.quant.package"].sudo().browse(package_ids or [])
        vals_list = []
        for index, position in enumerate(chosen, start=1):
            package = packages[index - 1] if index <= len(packages) else False
            vals_list.append({
                "location_id": position.id,
                "sale_order_id": order.id,
                "partner_id": order.partner_id.commercial_partner_id.id,
                "picking_id": picking.id if picking else False,
                "package_id": package.id if package else False,
                "slot_index": index,
                "date_start": start,
                "date_end": end,
                "appointment_start": arrival,
                "appointment_end": departure,
                "buffer_before": before,
                "buffer_after": after,
                "state": "reserved",
            })
        reservations = self.create(vals_list)
        _logger.info(
            "PFL position reservation: order %s reserved %s for %s–%s",
            order.name, ", ".join(chosen.mapped("complete_name")),
            start, end)
        return reservations

    def pfl_release(self, reason):
        """Free the space. Only ever called for reservations still in the future."""
        for record in self:
            if record.state == "reserved":
                record.write({"state": "released", "release_reason": reason})
        return True

    # ── the position board ─────────────────────────────────────────────
    @api.model
    def pfl_position_snapshot(self, position):
        """Current occupant and upcoming reservations for one position."""
        self.ensure_one() if False else None
        Quants = self.env["stock.quant"].sudo()
        Package = self.env["stock.quant.package"].sudo()
        occupant = False
        if position.id in self._pfl_occupied_location_ids():
            quants = Quants.search([("location_id", "=", position.id),
                                    ("quantity", ">", 0)])
            package = quants.mapped("package_id")[:1]
            if package:
                occupant = {
                    "package": package.name,
                    "order": package.x_pfl_sale_order_id
                    if "x_pfl_sale_order_id" in Package._fields else False,
                }
            else:
                occupant = {"package": False, "order": False,
                            "product": quants.mapped("product_id.display_name")[:1]}
        upcoming = self.sudo().search(
            [("location_id", "=", position.id), ("state", "=", "reserved"),
             ("date_end", ">", fields.Datetime.now())],
            order="date_start asc")
        return {"occupant": occupant, "upcoming": upcoming}

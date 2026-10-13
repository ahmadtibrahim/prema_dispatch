# -*- coding: utf-8 -*-
"""The dock schedule: what is expected at the warehouse, and what is overdue.

The warehouse employee's day starts here, not in a Sales order. One row per
expected event — a **drop-off** and/or a **pickup** for a warehouse booking —
carrying only what a person at the dock needs: when, which way, whose goods,
the booking and the customer reference, the pallet counts, the requested
services (quantities only), the reserved positions, the operational status and
the authorized carrier.

**Why a SQL view and not, say, a sale.order list.** The schedule has to show a
pickup whose outbound transfer has not been prepared yet, so it is driven by
the *booking*, not by the transfers — and the warehouse operator role is denied
`sale.order` outright (see `data/logistics_warehouse_operator_rules.xml`). A
read-only database view over the booking and its operational records gives the
operator the schedule without handing them the commercial record: the view's
columns are an explicit whitelist, and no price, rate, tax, total or customer
address is among them. This is the same discipline `_pfl_dock_summary` uses —
elevate nothing, select only what the dock may see.

The view is rebuilt on every module update (`init()`), so it can never drift
from the schema. Rows are identified by `booking.id * 2 + direction`, which is
stable across reads, so an action never fires against the wrong event.

**Overdue work does not disappear.** A job whose scheduled time has passed and
which is not finished is flagged, and the landing list shows today *and*
overdue together — a missed drop-off is the one thing the dock must not lose.
"""
from datetime import timezone
from zoneinfo import ZoneInfo

from odoo import _, api, fields, models
from odoo.exceptions import UserError

WAREHOUSE_TZ = "America/Toronto"

#: Warehouse instructions offered on the schedule. Quantities only — the same
#: list `_pfl_dock_summary` uses, so the schedule and the dock agree.
INSTRUCTION_CODES = ("PFL-XD-LTL", "PFL-XD-FTL", "PFL-WRAP", "PFL-PAL-SUP",
                     "PFL-LAB-HR", "PFL-STOR-DAY", "PFL-LABEL")

VIEW_NAME = "logistics_dock_schedule"


def _codes_sql():
    return ", ".join("'%s'" % code for code in INSTRUCTION_CODES)


class LogisticsDockSchedule(models.Model):
    _name = "logistics.dock.schedule"
    _description = "PF Logistics Dock Schedule"
    _auto = False
    _table = VIEW_NAME
    _log_access = False
    _order = "scheduled_datetime, booking_name, direction"
    _rec_name = "name"

    name = fields.Char(readonly=True)
    booking_id = fields.Integer(string="Booking ID", readonly=True)
    booking_name = fields.Char(string="Booking", readonly=True)
    customer_name = fields.Char(string="Customer", readonly=True)
    reference = fields.Char(string="Reference / PO", readonly=True)
    direction = fields.Selection(
        [("dropoff", "Drop-off"), ("pickup", "Pickup")],
        string="Direction", readonly=True)
    appointment_state = fields.Selection(
        [("requested", "Requested"), ("approved", "Approved"),
         ("declined", "Declined")],
        string="Requested / Approved", readonly=True)
    is_approved = fields.Boolean(readonly=True)
    scheduled_datetime = fields.Datetime(
        string="Scheduled", readonly=True,
        help="The approved time when there is one, otherwise the requested one.")
    scheduled_stop = fields.Datetime(readonly=True)
    scheduled_local = fields.Char(string="Scheduled (Toronto)", readonly=True)
    arrival_local = fields.Char(string="Arrival (Toronto)", readonly=True)
    departure_local = fields.Char(string="Pickup (Toronto)", readonly=True)
    expected_pallets = fields.Integer(readonly=True)
    prepared_pallets = fields.Integer(readonly=True)
    received_pallets = fields.Integer(readonly=True)
    positions = fields.Char(string="Reserved Positions", readonly=True)
    services = fields.Char(string="Requested Services", readonly=True)
    carrier = fields.Char(string="Authorized Carrier", readonly=True)
    status = fields.Selection(
        [("scheduled", "Scheduled"),
         ("prepared", "Pallets prepared"),
         ("arrived", "Truck arrived"),
         ("count_confirmed", "Count confirmed"),
         ("received", "Received"),
         ("authorized", "Pickup authorized"),
         ("release_in_progress", "Release being prepared"),
         ("released", "Released"),
         ("collected", "Collected")],
        string="Status", readonly=True)
    is_overdue = fields.Boolean(string="Overdue", readonly=True)
    picking_id = fields.Integer(readonly=True)
    picking_name = fields.Char(string="Transfer", readonly=True)
    picking_state = fields.Char(readonly=True)
    receipt_id = fields.Integer(readonly=True)
    release_id = fields.Integer(readonly=True)
    collection_id = fields.Integer(readonly=True)

    def init(self):
        """(Re)create the view. Cheap enough to rebuild on every update."""
        self.env.cr.execute("DROP VIEW IF EXISTS %s CASCADE" % VIEW_NAME)
        self.env.cr.execute(self._pfl_view_sql())

    @api.model
    def _pfl_view_sql(self):
        codes = _codes_sql()
        return """
CREATE OR REPLACE VIEW %(view)s AS
WITH booking AS (
    SELECT so.id, so.name, so.client_order_ref AS reference, so.state,
           so.x_dock_appointment_state AS appointment_state,
           so.x_requested_arrival, so.x_requested_departure,
           so.x_approved_arrival, so.x_approved_departure,
           COALESCE(so.x_expected_pallets, 0) AS expected_pallets,
           rp.name AS customer_name
    FROM sale_order so
    LEFT JOIN res_partner rp ON rp.id = so.partner_id
    WHERE so.x_warehouse_service = TRUE AND so.state <> 'cancel'
),
receipt AS (
    -- The one the dock is actually working: an open receipt first, else the
    -- most recent closed one.
    SELECT DISTINCT ON (sp.x_service_booking_id)
           sp.x_service_booking_id AS booking_id, sp.id, sp.name, sp.state,
           sp.pfl_confirmed, sp.pfl_arrived_at
    FROM stock_picking sp
    JOIN stock_picking_type pt ON pt.id = sp.picking_type_id
    WHERE pt.sequence_code = 'PFL-IN' AND sp.state <> 'cancel'
      AND sp.x_service_booking_id IS NOT NULL
    ORDER BY sp.x_service_booking_id, (sp.state = 'done') ASC, sp.id DESC
),
release AS (
    SELECT DISTINCT ON (sp.x_service_booking_id)
           sp.x_service_booking_id AS booking_id, sp.id, sp.name, sp.state
    FROM stock_picking sp
    JOIN stock_picking_type pt ON pt.id = sp.picking_type_id
    WHERE pt.sequence_code = 'PFL-OUT' AND sp.state <> 'cancel'
      AND sp.x_service_booking_id IS NOT NULL
    ORDER BY sp.x_service_booking_id, (sp.state = 'done') ASC, sp.id DESC
),
position AS (
    SELECT r.sale_order_id AS booking_id,
           COUNT(*) AS reserved_count,
           string_agg(DISTINCT sl.complete_name, ', ') AS positions
    FROM logistics_position_reservation r
    JOIN stock_location sl ON sl.id = r.location_id
    WHERE r.state = 'reserved'
    GROUP BY r.sale_order_id
),
pallet AS (
    SELECT sp.x_service_booking_id AS booking_id,
           COUNT(DISTINCT sml.result_package_id) AS prepared_count,
           COUNT(DISTINCT CASE WHEN sml.picked AND sml.quantity > 0
                               THEN sml.result_package_id END) AS received_count
    FROM stock_move_line sml
    JOIN stock_picking sp ON sp.id = sml.picking_id
    JOIN stock_picking_type pt ON pt.id = sp.picking_type_id
    WHERE pt.sequence_code = 'PFL-IN' AND sp.state <> 'cancel'
      AND sml.state <> 'cancel' AND sml.result_package_id IS NOT NULL
      AND sp.x_service_booking_id IS NOT NULL
    GROUP BY sp.x_service_booking_id
),
service AS (
    SELECT sol.order_id AS booking_id,
           string_agg(DISTINCT pp.default_code || ' x' ||
                      trim(to_char(sol.product_uom_qty, 'FM999999990.##')),
                      ', ') AS services
    FROM sale_order_line sol
    JOIN product_product pp ON pp.id = sol.product_id
    WHERE pp.default_code IN (%(codes)s) AND sol.product_uom_qty > 0
    GROUP BY sol.order_id
),
carrier AS (
    SELECT DISTINCT ON (c.sale_order_id)
           c.sale_order_id AS booking_id, c.id, c.state,
           COALESCE(c.authorized_contact_name, '') AS carrier
    FROM logistics_pickup_collection c
    WHERE c.state <> 'cancelled'
    ORDER BY c.sale_order_id, c.id DESC
),
event AS (
    SELECT b.id AS booking_id, b.name AS booking_name, b.reference,
           b.customer_name, b.state AS booking_state, b.appointment_state,
           b.expected_pallets,
           'dropoff' AS direction,
           COALESCE(b.x_approved_arrival, b.x_requested_arrival) AS sched,
           (b.x_approved_arrival IS NOT NULL) AS is_approved
    FROM booking b
    WHERE COALESCE(b.x_approved_arrival, b.x_requested_arrival) IS NOT NULL
    UNION ALL
    SELECT b.id, b.name, b.reference, b.customer_name, b.state,
           b.appointment_state, b.expected_pallets,
           'pickup' AS direction,
           COALESCE(b.x_approved_departure, b.x_requested_departure) AS sched,
           (b.x_approved_departure IS NOT NULL) AS is_approved
    FROM booking b
    WHERE COALESCE(b.x_approved_departure, b.x_requested_departure) IS NOT NULL
)
SELECT
    e.booking_id * 2 + CASE WHEN e.direction = 'pickup' THEN 1 ELSE 0 END AS id,
    e.booking_name || CASE WHEN e.direction = 'pickup'
                           THEN ' · Pickup' ELSE ' · Drop-off' END AS name,
    e.booking_id, e.booking_name, e.reference, e.customer_name,
    -- An approved time if the office approved one; otherwise whatever the
    -- booking says; otherwise the plain request. Never invented.
    CASE WHEN e.is_approved THEN 'approved'
         WHEN e.appointment_state IS NOT NULL THEN e.appointment_state
         ELSE 'requested' END AS appointment_state,
    e.is_approved, e.expected_pallets, e.direction,
    e.sched AS scheduled_datetime,
    e.sched + interval '1 hour' AS scheduled_stop,
    to_char(e.sched AT TIME ZONE 'UTC' AT TIME ZONE '%(tz)s',
            'YYYY-MM-DD HH24:MI') AS scheduled_local,
    to_char(b.x_requested_arrival AT TIME ZONE 'UTC' AT TIME ZONE '%(tz)s',
            'YYYY-MM-DD HH24:MI') AS arrival_local,
    to_char(b.x_requested_departure AT TIME ZONE 'UTC' AT TIME ZONE '%(tz)s',
            'YYYY-MM-DD HH24:MI') AS departure_local,
    COALESCE(pl.prepared_count, 0) AS prepared_pallets,
    COALESCE(pl.received_count, 0) AS received_pallets,
    COALESCE(po.positions, '') AS positions,
    COALESCE(sv.services, '') AS services,
    CASE WHEN e.direction = 'pickup' THEN COALESCE(ca.carrier, '') ELSE ''
    END AS carrier,
    CASE WHEN e.direction = 'dropoff' THEN
        CASE WHEN rc.state = 'done' THEN 'received'
             WHEN rc.pfl_confirmed THEN 'count_confirmed'
             WHEN rc.pfl_arrived_at IS NOT NULL THEN 'arrived'
             WHEN COALESCE(pl.prepared_count, 0) > 0 THEN 'prepared'
             ELSE 'scheduled' END
    ELSE
        CASE WHEN ca.state = 'collected' THEN 'collected'
             WHEN rl.state = 'done' THEN 'released'
             WHEN ca.id IS NOT NULL THEN 'authorized'
             WHEN rl.id IS NOT NULL THEN 'release_in_progress'
             ELSE 'scheduled' END
    END AS status,
    CASE WHEN e.direction = 'dropoff' THEN
        (e.sched < now() AND COALESCE(rc.state, '') <> 'done')
    ELSE
        (e.sched < now() AND COALESCE(ca.state, '') <> 'collected')
    END AS is_overdue,
    CASE WHEN e.direction = 'dropoff' THEN rc.id ELSE rl.id END AS picking_id,
    CASE WHEN e.direction = 'dropoff' THEN rc.name ELSE rl.name END AS picking_name,
    CASE WHEN e.direction = 'dropoff' THEN rc.state ELSE rl.state END AS picking_state,
    rc.id AS receipt_id, rl.id AS release_id, ca.id AS collection_id
FROM event e
JOIN booking b ON b.id = e.booking_id
LEFT JOIN receipt rc ON rc.booking_id = e.booking_id
LEFT JOIN release rl ON rl.booking_id = e.booking_id
LEFT JOIN position po ON po.booking_id = e.booking_id
LEFT JOIN pallet pl ON pl.booking_id = e.booking_id
LEFT JOIN service sv ON sv.booking_id = e.booking_id
LEFT JOIN carrier ca ON ca.booking_id = e.booking_id
""" % {"view": VIEW_NAME, "codes": codes, "tz": WAREHOUSE_TZ}

    # ── resolving the underlying records ───────────────────────────────
    def _pfl_booking(self):
        """The booking, read elevated — this view already exposed only its
        operational columns, and the caller is not handed the record."""
        self.ensure_one()
        booking = self.env["sale.order"].sudo().browse(self.booking_id)
        if not booking.exists():
            raise UserError(_("Booking %s no longer exists.", self.booking_name))
        return booking

    def _pfl_picking(self):
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_(
                "There is no %s transfer for %s yet.",
                dict(self._fields["direction"].selection)[self.direction],
                self.booking_name))
        picking = self.env["stock.picking"].browse(self.picking_id)
        if not picking.exists():
            raise UserError(_("Transfer %s no longer exists.", self.picking_name))
        return picking

    def _pfl_receipt(self):
        self.ensure_one()
        if self.direction != "dropoff":
            raise UserError(_("This event is a pickup, not a drop-off."))
        return self._pfl_picking()

    def _pfl_open_barcode(self, picking):
        return {
            "type": "ir.actions.client",
            "tag": "stock_barcode_client_action",
            "name": _("Barcode"),
            "params": {"actionId": False},
            "context": {"active_id": picking.id, "active_model": "stock.picking"},
        }

    # ── incoming: the buttons a drop-off offers ────────────────────────
    def action_pfl_sched_prepare(self):
        """Prepare the pallets and their labels without hunting for the order.

        Reuses the one-click preparation the back office already has; running
        it twice prepares nothing twice.
        """
        self.ensure_one()
        self._pfl_booking().action_pfl_prepare_pallets()
        self.env.invalidate_all()
        refreshed = self.browse(self.id)
        if refreshed.picking_id:
            return self._pfl_open_barcode(
                self.env["stock.picking"].browse(refreshed.picking_id))
        return self.action_pfl_sched_open_picking()

    def action_pfl_sched_print_labels(self):
        self.ensure_one()
        return self._pfl_receipt().action_pfl_print_labels()

    def action_pfl_sched_print_cards(self):
        self.ensure_one()
        return self._pfl_receipt().action_pfl_print_cards()

    def action_pfl_sched_arrived(self):
        self.ensure_one()
        return self._pfl_receipt().action_pfl_mark_arrived()

    def action_pfl_sched_open_barcode(self):
        self.ensure_one()
        return self._pfl_open_barcode(self._pfl_picking())

    # ── outgoing: the buttons a pickup offers ──────────────────────────
    def action_pfl_sched_open_release(self):
        """Open the pickup/loading in Barcode. The release is never created
        from here — an unprepared pickup simply has no transfer to open."""
        self.ensure_one()
        if self.direction != "pickup":
            raise UserError(_("This event is a drop-off, not a pickup."))
        return self._pfl_open_barcode(self._pfl_picking())

    def action_pfl_sched_verify_pickup(self):
        """Record the collecting driver, how authority was verified, and the
        signature — the same dock dialog the release's own button opens."""
        self.ensure_one()
        return self._pfl_picking().action_pfl_open_pickup()

    # ── generic ────────────────────────────────────────────────────────
    def action_pfl_sched_open_picking(self):
        self.ensure_one()
        picking = self._pfl_picking()
        return {
            "type": "ir.actions.act_window",
            "res_model": "stock.picking",
            "res_id": picking.id,
            "view_mode": "form",
            "target": "current",
        }

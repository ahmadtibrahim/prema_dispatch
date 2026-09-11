"""TIER 2 §2 — Book Load wizard timing fields (shared by BOTH wizards).

The two Book Load wizards (prema.dispatch.so.book.wizard for a Sales
Order, prema.dispatch.book.load.wizard for an invoice) must expose and
auto-fill the shipment's timing BEFORE Confirm Booking, and every value
must stay editable by the dispatcher. Both wizards therefore inherit this
abstract model rather than carrying two copies of the same twelve fields.

The two channels the spec keeps apart:

* FACILITY HOURS  — the dock's own open/close for the shipment's day.
  A property of the BUILDING (facility_open_time / facility_close_time).
* WINDOW TYPE + TIMES — the CUSTOMER's commitment: Flexible, Facility
  Hours, Earliest Time, Time Window, Exact Appointment (pickup) or
  Flexible, Facility Hours, Deadline, Time Window, Exact Appointment
  (delivery), with its start/end/deadline.

They coexist: an exact appointment 11:00–12:00 does not erase a dock that
opens at 06:00 and closes at 16:00, and vice versa. Nothing here is
derived by AI — the values arrive from the deterministic mapping service
(BookLoadMappingService) or the dispatcher's own keystrokes.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

PICKUP_WINDOW_TYPES = [
    ("flexible", "Flexible"),
    ("facility_hours", "Facility Hours"),
    ("earliest_time", "Earliest Time"),
    ("time_window", "Time Window"),
    ("exact_appointment", "Exact Appointment"),
]

DELIVERY_WINDOW_TYPES = [
    ("flexible", "Flexible"),
    ("facility_hours", "Facility Hours"),
    ("deadline", "Deadline"),
    ("time_window", "Time Window"),
    ("exact_appointment", "Exact Appointment"),
]

# Which time inputs each window type actually reads — the rest are cleared
# on change so a stale value can never ride along unnoticed.
_PICKUP_INPUTS = {
    "flexible": (),
    "facility_hours": ("facility",),
    "earliest_time": ("start",),
    "time_window": ("start", "end"),
    "exact_appointment": ("start", "end"),
}
_DELIVERY_INPUTS = {
    "flexible": (),
    "facility_hours": ("facility",),
    "deadline": ("deadline",),
    "time_window": ("start", "end"),
    "exact_appointment": ("start", "end"),
}


class BookLoadTimingMixin(models.AbstractModel):
    _name = "prema.dispatch.book.load.timing.mixin"
    _description = "Book Load Wizard — Shipment Timing (Tier 2 §2)"

    # ── Pickup ─────────────────────────────────────────────────────────
    pickup_facility_open_time = fields.Float(
        string="Pickup facility open",
        help="The pickup dock's own opening time on the shipment's day "
             "(e.g. 06:00). Independent of the pickup window below.")
    pickup_facility_close_time = fields.Float(
        string="Pickup facility close",
        help="The pickup dock's own closing time (e.g. 16:00).")
    pickup_window_type = fields.Selection(
        PICKUP_WINDOW_TYPES, string="Pickup window type", default="flexible",
        required=True)
    pickup_window_start = fields.Float(
        string="Pickup window start")
    pickup_window_end = fields.Float(
        string="Pickup window end")
    pickup_appointment_required = fields.Boolean(
        string="Pickup appointment required")

    # ── Delivery ───────────────────────────────────────────────────────
    delivery_facility_open_time = fields.Float(
        string="Delivery facility open",
        help="The delivery dock's own opening time (e.g. 08:00).")
    delivery_facility_close_time = fields.Float(
        string="Delivery facility close")
    delivery_window_type = fields.Selection(
        DELIVERY_WINDOW_TYPES, string="Delivery window type",
        default="flexible", required=True)
    delivery_window_start = fields.Float(
        string="Delivery window start")
    delivery_window_end = fields.Float(
        string="Delivery window end")
    delivery_deadline = fields.Datetime(
        string="Delivery deadline",
        help="Hard 'must be completed by' moment for the delivery.")
    delivery_appointment_required = fields.Boolean(
        string="Delivery appointment required")

    @api.onchange("pickup_window_type", "delivery_window_type")
    def _onchange_window_type_clear_stale(self):
        """Changing a window type clears the inputs that type no longer
        reads — a leftover 11:00 from a previous Exact Appointment must
        never be silently re-applied as a Window start."""
        for wizard in self:
            self._apply_window_shape(wizard, "pickup", _PICKUP_INPUTS)
            self._apply_window_shape(wizard, "delivery", _DELIVERY_INPUTS)

    def _apply_window_shape(self, wizard, side, inputs_by_type):
        keep = inputs_by_type.get(getattr(wizard, "%s_window_type" % side), ())
        if "facility" not in keep:
            wizard["%s_facility_open_time" % side] = False
            wizard["%s_facility_close_time" % side] = False
        if "start" not in keep:
            wizard["%s_window_start" % side] = False
        if "end" not in keep:
            wizard["%s_window_end" % side] = False
        # Only the delivery side carries a deadline (§2), so never write a
        # "{side}_deadline" that this wizard does not have.
        deadline_field = "%s_deadline" % side
        if "deadline" not in keep and deadline_field in wizard._fields:
            wizard[deadline_field] = False
        # An appointment-bearing window always needs the flag; the
        # dispatcher can still clear it deliberately on a facility-hours
        # or flexible stop.
        if getattr(wizard, "%s_window_type" % side) == "exact_appointment":
            wizard["%s_appointment_required" % side] = True

    def _timing_stop_values(self, side):
        """This side's timing as booking-stop keys (24h floats +
        hard_deadline), whatever the source channel was.

        A window type whose inputs are missing is REFUSED rather than
        silently stored as midnight — a booking must never claim a
        00:00 window the customer never asked for."""
        window_type = getattr(self, "%s_window_type" % side) or "flexible"
        start = getattr(self, "%s_window_start" % side, 0.0) or 0.0
        end = getattr(self, "%s_window_end" % side, 0.0) or 0.0
        label = _("Pickup") if side == "pickup" else _("Delivery")
        values = {
            "timing_type": window_type,
            "appointment_required": bool(
                getattr(self, "%s_appointment_required" % side, False)),
            "facility_open_time": getattr(
                self, "%s_facility_open_time" % side, 0.0) or False,
            "facility_close_time": getattr(
                self, "%s_facility_close_time" % side, 0.0) or False,
        }
        if window_type in ("earliest_time", "time_window",
                           "exact_appointment") and not start:
            raise UserError(_(
                "%s window type is set but no start time was entered — "
                "enter the time, or set the type to Flexible.") % label)
        if window_type == "time_window" and not end:
            raise UserError(_(
                "Delivery/Pickup Time Window needs both a start and an end "
                "time — enter the end time, or use Earliest Time."))
        if window_type == "facility_hours" and not values["facility_open_time"]:
            raise UserError(_(
                "%s window type is 'Facility Hours' but no facility opening "
                "time is set — enter the dock's hours, or set the type to "
                "Flexible.") % label)
        if window_type == "earliest_time":
            values["window_start"] = start
        elif window_type == "time_window":
            values["window_start"] = start
            values["window_end"] = end
        elif window_type == "exact_appointment":
            # The quoted appointment is a RANGE ("11:00 AM to 12:00 PM"):
            # appointment_time is its start, window_end its quoted end.
            values["appointment_time"] = start
            values["window_start"] = start
            if end:
                values["window_end"] = end
        elif window_type == "deadline":
            deadline = getattr(self, "%s_deadline" % side, False)
            if not deadline:
                raise UserError(_(
                    "%s window type is 'Deadline' but no deadline was set — "
                    "enter the deadline, or set the type to Flexible.") % label)
            values["hard_deadline"] = deadline
        return values

"""Night-before route finalization service.

Runs at the CONFIGURABLE Route Finalization Time (Dispatch Settings →
"logistics.route_finalization_hour") and freezes each scheduled departure's
itinerary for the next day:

  1. Collect every confirmed booking on the departure.
  2. Build planner stops from the booking stops (facility hours snapshot,
     timing-type windows, appointments, service time — the SAME data the
     quote/calendar ETAs use).
  3. Optimize the stop order with ItineraryPlanner.recommend_route
     (precedence, capacity, facility-hours feasibility, waiting).
  4. Walk the recommended order from the corridor's real departure datetime
     and compute per-stop arrival / service-start / departure times — never
     before a facility opens; closed days roll to the next open slot.
  5. Freeze the itinerary on the departure (confirmed_itinerary Json),
     stamp the booking-level PLANNED pickup/delivery windows
     (pickup_eta_confirmed / confirmed_*_window — INTERNAL planning data
     only, never a customer promise), and write the planned service times
     onto the matching dispatch stops (scheduled_time) for the driver.
  6. At the separate CONFIGURABLE Customer ETA Notification Time the
     notifier emails every finalized-not-yet-notified booking its
     ESTIMATED windows + tracking link — never "confirmed/final"
     language. The public tracking page is the service-day ETA authority:
     it updates as the driver progresses through the route.

Nothing here is hardcoded: both hours are ir.config_parameter values.
"""

import logging

from datetime import datetime, timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

# Booking states eligible for night-before finalization (not yet executed,
# not cancelled).
FINALIZABLE_STATES = ("confirmed", "planned")


def _op_now():
    """Operational wall clock (America/Toronto) — same convention as the
    routing service's _op_today."""
    from pytz import timezone as tz
    return datetime.now(tz("America/Toronto"))


class RouteFinalizationService:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    # ── Configurable hours (never hardcoded) ─────────────────────────

    def finalization_hour(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "logistics.route_finalization_hour", "17.0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 17.0

    def notification_hour(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "logistics.customer_eta_notification_hour", "18.0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 18.0

    def hour_matches(self, configured_hour, now=None):
        """Whole-hour cron match (the cron runs hourly). A configured hour
        of 17.5 is treated as 17 (documented granularity)."""
        now = now or _op_now()
        return int(configured_hour) == now.hour

    # ── Cron entry points ────────────────────────────────────────────

    def cron_finalize(self, date=None):
        """Finalize departures scheduled for the next day (or `date`).
        Idempotent: a finalized departure is skipped unless a booking was
        added after finalization (finalized_at < newest booking write)."""
        from pytz import timezone as tz
        op_tz = tz("America/Toronto")
        now = _op_now()
        target = date or (now + timedelta(days=1)).date()
        Departure = self.env["logistics.corridor.departure"]
        departures = Departure.search([
            ("departure_date", "=", target),
            ("active", "=", True),
            ("status", "=", "scheduled"),
        ])
        results = []
        for departure in departures:
            try:
                outcome = self.finalize_departure(departure)
            except Exception:
                _logger.exception(
                    "Finalization failed for departure %s", departure.id)
                outcome = {"departure_id": departure.id, "error": True}
            results.append(outcome)
        finalized = [r for r in results if r.get("finalized")]
        _logger.info(
            "Route finalization: %d departures for %s, %d finalized",
            len(departures), target, len(finalized))
        return {"target_date": str(target), "total": len(departures),
                "finalized": len(finalized), "results": results}

    def cron_notify(self):
        """Email ESTIMATED pickup/delivery windows + tracking link to
        every finalized booking that has not been notified yet (at the
        configured Customer ETA Notification Time)."""
        Booking = self.env["logistics.booking"]
        bookings = Booking.search([
            ("pickup_eta_confirmed", "=", True),
            ("eta_notification_sent", "=", False),
            ("state", "in", list(FINALIZABLE_STATES) + ["in_execution", "delivered"]),
        ])
        sent = 0
        for booking in bookings:
            try:
                if self._send_confirmed_window_email(booking):
                    sent += 1
            except Exception:
                _logger.exception(
                    "ETA notification failed for booking %s", booking.booking_number)
        _logger.info("Customer ETA notifications sent: %d/%d",
                     sent, len(bookings))
        return {"pending": len(bookings), "sent": sent}

    # ── Finalization ─────────────────────────────────────────────────

    def finalize_departure(self, departure):
        """Freeze one departure's confirmed itinerary."""
        from ..services.itinerary_planner import (
            ItineraryPlanner, snapshot_facility_hours,
        )

        Booking = self.env["logistics.booking"]
        bookings = Booking.search([
            ("departure_id", "=", departure.id),
            ("state", "in", list(FINALIZABLE_STATES)),
        ])
        if not bookings:
            return {"departure_id": departure.id, "finalized": False,
                    "reason": "no_finalizable_bookings"}

        # Re-finalize only when something changed since the last freeze.
        newest_write = bookings.sudo().max("write_date") if bookings else False
        if (departure.finalized_at and newest_write
                and departure.finalized_at >= newest_write):
            return {"departure_id": departure.id, "finalized": False,
                    "reason": "already_finalized"}

        # 1. Planner stops from the booking stops (facility-hours snapshot,
        #    timing windows, appointments, service time).
        planner_stops = []
        movements = []
        stop_meta = {}  # stop_key → booking / booking.stop metadata
        for booking in bookings:
            stops = booking.stop_ids.sorted("sequence")
            pickup_stops = [s for s in stops if s.stop_type == "pickup"]
            delivery_stops = [s for s in stops if s.stop_type == "delivery"]
            for side, key_prefix in (("pickup", "p"), ("delivery", "d")):
                side_stops = pickup_stops if side == "pickup" else delivery_stops
                for index, bstop in enumerate(side_stops):
                    stop_key = f"b{booking.id}-{key_prefix}{index + 1}"
                    hours = bstop.operating_hours_snapshot or (
                        snapshot_facility_hours(
                            self.env, bstop.saved_location_id, side)
                        if bstop.saved_location_id else None)
                    planner_stops.append({
                        "stop_key": stop_key,
                        "stop_type": side,
                        "latitude": bstop.latitude
                            or (bstop.saved_location_id.pin_lat
                                if bstop.saved_location_id else 0.0) or 0.0,
                        "longitude": bstop.longitude
                            or (bstop.saved_location_id.pin_lng
                                if bstop.saved_location_id else 0.0) or 0.0,
                        "city": bstop.city or bstop.location_name or "",
                        "timezone": bstop.timezone or "America/Toronto",
                        "operating_hours_snapshot": hours,
                        "timing_type": bstop.timing_type or "flexible",
                        "window_start": bstop.window_start,
                        "window_end": bstop.window_end,
                        "appointment_time": bstop.appointment_time,
                        "service_time_minutes": bstop.service_time_minutes or 15,
                    })
                    stop_meta[stop_key] = {"booking": booking, "stop": bstop}
            if pickup_stops and delivery_stops:
                movements.append({
                    "key": f"m{booking.id}",
                    "pickup_stop_key": f"b{booking.id}-p1",
                    "delivery_stop_keys": [
                        f"b{booking.id}-d{i + 1}" for i in range(len(delivery_stops))],
                    "shared": False,
                    "weight_lbs": booking.weight_lbs or 0.0,
                })

        if not planner_stops:
            return {"departure_id": departure.id, "finalized": False,
                    "reason": "no_planner_stops"}

        # 2. Optimized stop order from the corridor's real start time.
        from pytz import timezone as tz
        op_tz = tz("America/Toronto")
        dep_dt = datetime.combine(
            departure.departure_date, datetime.min.time(),
            tzinfo=op_tz) + timedelta(hours=departure.departure_time or 0.0)
        vehicle_max = departure.max_capacity or (
            departure.corridor_id.planned_pallets if departure.corridor_id else 0)
        planner = ItineraryPlanner(self.env)
        ordered = planner.recommend_route(
            planner_stops, movements, dep_dt, vehicle_max=vehicle_max)

        # 3. Walk the order: travel + facility-hours arrival plan per stop.
        itinerary = []
        cursor = dep_dt
        for stop in ordered:
            travel = planner._travel_minutes(
                {"latitude": getattr(stop, "latitude", 0.0) or 0.0,
                 "longitude": getattr(stop, "longitude", 0.0) or 0.0},
                {"latitude": stop.get("latitude") if isinstance(stop, dict) else 0.0,
                 "longitude": stop.get("longitude") if isinstance(stop, dict) else 0.0},
            )
            cursor = cursor + timedelta(minutes=travel)
            feasible, waiting, service_start, departure_dt = planner.arrival_plan(
                stop, cursor)
            key = stop.get("stop_key") if isinstance(stop, dict) else stop.stop_key
            meta = stop_meta.get(key, {})
            bstop = meta.get("stop")
            itinerary.append({
                "stop_key": key,
                "booking_id": meta.get("booking").id if meta.get("booking") else None,
                "booking_number": (meta.get("booking").booking_number
                                   if meta.get("booking") else ""),
                "booking_stop_id": bstop.id if bstop else None,
                "stop_type": stop.get("stop_type") if isinstance(stop, dict) else stop.stop_type,
                "name": (stop.get("city") or "") if isinstance(stop, dict) else (stop.city or ""),
                "arrival": self._iso(stop and service_start),
                "service_start": self._iso(service_start),
                "departure": self._iso(departure_dt),
                "waiting_minutes": round(waiting),
                "feasible": bool(feasible),
            })
            cursor = departure_dt

        # 4. Freeze on the departure.
        departure.write({
            "confirmed_itinerary": {
                "finalized_at": fields.Datetime.now().isoformat(),
                "departure_datetime": dep_dt.isoformat(),
                "departure_id": departure.id,
                "stops": itinerary,
            },
            "finalized_at": fields.Datetime.now(),
        })

        # 5. Booking-level PLANNED windows (internal) + dispatch stop
        #    scheduled times. Every booking picks its OWN
        #    pickup/last-delivery plans.
        by_booking = {}
        for entry in itinerary:
            by_booking.setdefault(entry["booking_id"], []).append(entry)
        for booking in bookings:
            plans = by_booking.get(booking.id, [])
            pickup_plans = [p for p in plans if p["stop_type"] == "pickup"]
            delivery_plans = [p for p in plans if p["stop_type"] == "delivery"]
            if not pickup_plans or not delivery_plans:
                continue
            pu_plan = pickup_plans[0]
            de_plan = delivery_plans[-1]
            pu_win = self._window_label(pu_plan["service_start"])
            de_win = self._window_label(de_plan["service_start"])
            booking.write({
                "pickup_eta_confirmed": True,
                "confirmed_pickup_window": pu_win,
                "confirmed_delivery_window": de_win,
            })
            # Dispatch: write the confirmed service time onto the matching
            # dispatch stop (via the booking-stop bridge).
            for plan in plans:
                if not plan.get("booking_stop_id"):
                    continue
                dispatch_stop = self.env["prema.dispatch.stop"].search([
                    ("logistics_booking_stop_id", "=", plan["booking_stop_id"]),
                ], limit=1)
                if dispatch_stop:
                    dispatch_stop.write({
                        "scheduled_time": self._parse_iso(plan["service_start"]),
                    })

        _logger.info(
            "Finalized departure %s (%s): %d bookings, %d stops",
            departure.id, departure.departure_date, len(bookings), len(itinerary))
        return {"departure_id": departure.id, "finalized": True,
                "bookings": len(bookings), "stops": len(itinerary)}

    # ── Customer notification ────────────────────────────────────────

    def _send_confirmed_window_email(self, booking):
        """Email the customer their ESTIMATED pickup/delivery windows with
        the tracking link. The finalization result is internal planning —
        the customer is never promised a "confirmed" or "final" time. The
        tracking page is the service-day ETA authority (it updates as the
        driver progresses through the route). Returns True when the mail
        was created."""
        partner = booking.partner_id
        email = (booking.partner_id.email or "").strip()
        if not email:
            return False
        base_url = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "")
        tracking_url = (
            f"{base_url}/track?booking_number={booking.booking_number}"
            f"&tracking_token={booking.tracking_token}"
        )
        body = (
            "<h3>Your shipment is scheduled</h3>"
            f"<p><strong>Booking:</strong> {booking.booking_number}</p>"
            f"<p><strong>Estimated Pickup:</strong> "
            f"{booking.confirmed_pickup_window or 'See tracking'}</p>"
            f"<p><strong>Estimated Delivery:</strong> "
            f"{booking.confirmed_delivery_window or 'See tracking'}</p>"
            f"<p>Estimated times may change as the route is finalized. "
            f"Your updated ETA will be available on the day of service "
            f"through your tracking link, and actual arrival times update "
            f"as the driver progresses through the route. Track your "
            f"shipment: "
            f"<a href='{tracking_url}'>{tracking_url}</a></p>"
            "<p>Prema Logistics</p>"
        )
        company = self.env.company
        mail = self.env["mail.mail"].create({
            "subject": f"Your Prema shipment {booking.booking_number} — "
                       f"pickup/delivery estimates",
            "body_html": body,
            "email_from": company.email or "no-reply@premafirm.com",
            "email_to": email,
            "model": "logistics.booking",
            "res_id": booking.id,
            "auto_delete": False,
        })
        mail.send()
        booking.write({"eta_notification_sent": True})
        _logger.info("ETA notification sent for %s → %s",
                     booking.booking_number, email)
        return True

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _iso(dt):
        return dt.isoformat() if dt else None

    @staticmethod
    def _parse_iso(iso):
        if not iso:
            return False
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return False
        # Store UTC-naive (Odoo Datetime convention).
        if dt.tzinfo:
            from pytz import timezone as tz
            dt = dt.astimezone(tz("UTC"))
        return dt.replace(tzinfo=None)

    @staticmethod
    def _window_label(iso):
        """'2026-08-25T08:00:00' → 'Tue Aug 25 · 8:00 AM – 8:30 AM'."""
        dt = RouteFinalizationService._parse_iso(iso)
        if not dt:
            return ""
        return "%s · %s – %s" % (
            dt.strftime("%a %b %d"),
            dt.strftime("%-I:%M %p"),
            (dt + timedelta(minutes=30)).strftime("%-I:%M %p"),
        )

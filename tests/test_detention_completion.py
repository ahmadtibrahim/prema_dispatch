"""MP1 D-B3 (§18) detention completion — targeted verification.

Covers the stop-kind dimension and the new timing/evidence/extras on the
dispatch-module side:

  D1  Pickup vs delivery rule sides: pickup-kind stops freeze the pickup
      parameter set; a pickup side left at zero follows the same rule's
      delivery columns (legacy single-set rules behave unchanged).
  D2  Minimum charge binds once any unit bills; the maximum caps; both
      freeze on the item at suggestion.
  D3  Dwell spans (dock start → release) when both timing records exist,
      else departure − arrival; post-completion timing backfills refresh
      a still-draft item (shorter span → honest charge, never stale).
  D4  action_record_timing: forward recording order is legal, inverted
      timestamps raise, completed-stop refresh happens per event.
  D5  evidence_required gates Approve/Modify until live stop evidence is
      linked (auto-linked at capture); Waive stays available.
  D6  Exception catalog is neutral: it never rewrites the suggested
      charge and never auto-approves.

Nothing commits — every test rolls back.
"""

import datetime
import json

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestDetentionCompletion(TransactionCase):
    """§18 customer detention completion (dispatch-module side)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "prema_dispatch.detention_defaults", json.dumps({
                "free_minutes": 30,
                "increment_minutes": 30,
                "rate_per_increment": 0.0,
            }))
        cls.partner = cls.env["res.partner"].create(
            {"name": "D-B3 Test Customer"})
        cls.facility = cls.env["prema.dispatch.location"].create({
            "name": "D-B3 Test Facility",
            "address": "125 Test St, Ontario",
            "pin_lat": 43.6,
            "pin_lng": -79.4,
            # The stop validator refuses cross_dock_pickup stops unless
            # the saved location is Cross-Dock-enabled.
            "allow_cross_dock": True,
        })
        cls.job = cls.env["prema.dispatch.job"].create(
            {"partner_id": cls.partner.id})
        # Mid-loading test phase: prema_logistics_booking loads AFTER
        # prema_dispatch, so logistics.booking is not yet in the model
        # pool and these comodels were set to _unknown. The final
        # registry.setup_models pass fixes them at boot in production —
        # repair them here so the mid-load tests can run.
        for model, field in (
                ("prema.dispatch.detention.item", "booking_id"),
                ("prema.dispatch.job", "logistics_booking_id")):
            f = cls.env[model]._fields.get(field)
            if f and f.comodel_name == "_unknown":
                f.comodel_name = "logistics.booking"

    # ── Fixture helpers ────────────────────────────────────────────

    def _rule(self, free=30, increment=30, rate=25.0, min_charge=0.0,
              max_charge=0.0, evidence=False):
        return self.env["prema.dispatch.detention.rule"].create({
            "partner_id": self.partner.id,
            "facility_id": self.facility.id,
            "free_minutes": free,
            "increment_minutes": increment,
            "rate_per_increment": rate,
            "minimum_charge": min_charge,
            "maximum_charge": max_charge,
            "evidence_required": evidence,
        })

    def _stop(self, dwell_minutes, stop_type="dropoff", status="completed",
              arrival="2026-09-07 09:00:00"):
        """A completed stop whose arrival→departure span is exactly
        dwell_minutes (departure = arrival + dwell)."""
        arrival_dt = datetime.datetime.strptime(arrival, "%Y-%m-%d %H:%M:%S")
        departure_dt = arrival_dt + datetime.timedelta(
            minutes=dwell_minutes or 0)
        return self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": stop_type,
            "status": status,
            "saved_location_id": self.facility.id,
            "actual_arrival_time": arrival,
            "actual_departure_time": departure_dt.strftime(
                "%Y-%m-%d %H:%M:%S"),
        })

    def _suggest(self, stop):
        return self.env["prema.dispatch.detention.item"]._suggest_for_stop(
            stop)

    def _evidence(self, stop, ev_type="pod_general", upload_state="uploaded"):
        """Capture-style evidence record for the stop (sudo, like the
        driver upload path)."""
        import base64
        att = self.env["ir.attachment"].sudo().create({
            "name": "d-b3-proof.jpg",
            "type": "binary",
            "datas": base64.b64encode(b"jpg-bytes").decode("ascii"),
            "res_model": "prema.dispatch.stop",
            "res_id": stop.id,
        })
        return self.env["prema.dispatch.evidence"].sudo()._create_evidence(
            att, stop, ev_type, {"upload_state": upload_state})

    # ── D1: pickup vs delivery rule sides ──────────────────────────

    def test_d1_kind_split_and_legacy_fallback(self):
        """Pickup stops freeze the pickup side; a zero pickup side keeps
        using the delivery columns of the same rule."""
        Rule = self.env["prema.dispatch.detention.rule"]
        rule = self._rule(free=30, increment=30, rate=25.0)
        # create() keeps the pickup side in step with the delivery side —
        # the split is opt-in per rule.
        self.assertEqual(rule.pickup_free_minutes, 30)
        self.assertEqual(rule.pickup_rate_per_increment, 25.0)

        # Legacy fallback: an ALL-ZERO pickup side follows the delivery
        # columns (this is what a pre-§18 rule looked like before the
        # migration backfilled it; the fallback keeps it working anyway).
        rule.write({"pickup_free_minutes": 0, "pickup_increment_minutes": 0,
                    "pickup_rate_per_increment": 0.0})
        match_delivery = Rule._match(self.partner.id, self.facility.id)
        match_pickup = Rule._match(self.partner.id, self.facility.id,
                                   stop_kind="pickup")
        self.assertEqual(match_pickup["free_minutes"], 30)
        self.assertEqual(match_pickup["rate_per_increment"], 25.0)
        self.assertEqual(match_delivery["free_minutes"], 30)

        # Split configured: pickup stops get the pickup set, delivery
        # stops keep the base set — one rule row, two parameter sets.
        rule.write({"pickup_free_minutes": 15, "pickup_increment_minutes": 15,
                    "pickup_rate_per_increment": 40.0})
        pickup_match = Rule._match(self.partner.id, self.facility.id,
                                   stop_kind="pickup")
        self.assertEqual(pickup_match["free_minutes"], 15)
        self.assertEqual(pickup_match["increment_minutes"], 15)
        self.assertEqual(pickup_match["rate_per_increment"], 40.0)
        delivery_match = Rule._match(self.partner.id, self.facility.id,
                                     stop_kind="delivery")
        self.assertEqual(delivery_match["free_minutes"], 30)
        self.assertEqual(delivery_match["rate_per_increment"], 25.0)

        # Whole-flow check through suggestion: a pickup-kind stop (both
        # pickup stop types) freezes the pickup side; the dropoff stop on
        # the SAME job/facility freezes the delivery side.
        item_pickup = self._suggest(
            self._stop(50, stop_type="pickup", arrival="2026-09-07 07:00:00"))
        # 50 min dwell − 15 free = 35 → 3 units × $40 = $120.
        self.assertEqual(item_pickup.stop_kind, "pickup")
        self.assertEqual(item_pickup.free_minutes, 15)
        self.assertEqual(item_pickup.rate_per_increment, 40.0)
        self.assertEqual(item_pickup.suggested_amount, 120.0)
        item_xdock = self._suggest(self._stop(
            50, stop_type="cross_dock_pickup",
            arrival="2026-09-07 06:00:00"))
        self.assertEqual(item_xdock.stop_kind, "pickup")
        item_drop = self._suggest(self._stop(50))
        self.assertEqual(item_drop.stop_kind, "delivery")
        self.assertEqual(item_drop.free_minutes, 30)
        # 50 − 30 free = 20 billable → 1 unit × $25.
        self.assertEqual(item_drop.suggested_amount, 25.0)

        # _match's kind defaults to delivery for legacy callers.
        legacy = Rule._match(self.partner.id, self.facility.id)
        self.assertEqual(legacy["stop_kind"], "delivery")
        self.assertEqual(legacy["rate_per_increment"], 25.0)

    # ── D2: minimum / maximum charge ───────────────────────────────

    def test_d2_minimum_and_cap_clamp_and_freeze(self):
        """min binds once any unit bills; the cap always binds; both are
        frozen on the item at suggestion."""
        rule = self._rule(free=30, increment=30, rate=25.0,
                          min_charge=60.0, max_charge=200.0)
        # 95-min dwell → 3 units × $25 = $75 — above min, under cap.
        item = self._suggest(self._stop(95))
        self.assertEqual(item.units, 3)
        self.assertEqual(item.minimum_charge, 60.0)
        self.assertEqual(item.maximum_charge, 200.0)
        self.assertEqual(item.suggested_amount, 75.0)
        # 50-min dwell → 1 unit × $25 = $25 — clamped UP to the minimum.
        item_min = self._suggest(self._stop(50))
        self.assertEqual(item_min.units, 1)
        self.assertEqual(item_min.suggested_amount, 60.0)
        # 300-min dwell → 9 units × $25 = $225 — clamped DOWN to the cap.
        item_cap = self._suggest(self._stop(300))
        self.assertEqual(item_cap.suggested_amount, 200.0)

        # Rule extras frozen: a later rule change never mutates an
        # existing item (drafts refresh only via re-suggestion).
        rule.write({"minimum_charge": 0.0, "maximum_charge": 0.0})
        self.env.invalidate_all()
        self.assertEqual(item_min.minimum_charge, 60.0,
                         "frozen at suggestion, rule edit does not reach it")
        self.assertEqual(item_cap.maximum_charge, 200.0)

        # Re-suggestion of a DRAFT re-freezes from the current rule.
        again = self._suggest(item_min.stop_id)
        self.assertEqual(again.id, item_min.id)
        self.assertEqual(again.minimum_charge, 0.0)
        self.assertEqual(again.suggested_amount, 25.0)

    # ── D3: dwell span (dock start → release) ──────────────────────

    def test_d3_dwell_spans_and_timing_refresh(self):
        """Dock span wins when both timing records exist; otherwise the
        actual arrival→departure span; timing backfills refresh drafts."""
        self._rule()
        # 120-min wall-clock stop; dock start 10:00 + release 10:45 →
        # 45-min facility-held span → 1 unit × $25.
        stop = self._stop(120)
        stop.write({"dock_start_at": "2026-09-07 10:00:00",
                    "released_at": "2026-09-07 10:45:00"})
        item = self._suggest(stop)
        self.assertEqual(item.actual_dwell_minutes, 45)
        self.assertEqual(item.suggested_amount, 25.0)

        # Only dock start recorded → arrival→departure span (120).
        stop2 = self._stop(120)
        stop2.write({"dock_start_at": "2026-09-07 10:00:00"})
        item2 = self._suggest(stop2)
        self.assertEqual(item2.actual_dwell_minutes, 120)

        # Only release recorded → arrival→departure span.
        stop3 = self._stop(120)
        stop3.write({"released_at": "2026-09-07 10:45:00"})
        item3 = self._suggest(stop3)
        self.assertEqual(item3.actual_dwell_minutes, 120)

        # An inverted span (release before dock start) falls back to the
        # arrival→departure actuals instead of producing nonsense.
        stop4 = self._stop(120)
        stop4.write({"dock_start_at": "2026-09-07 10:45:00",
                     "released_at": "2026-09-07 10:00:00"})
        item4 = self._suggest(stop4)
        self.assertEqual(item4.actual_dwell_minutes, 120)

        # Post-completion backfill: the item was suggested from the
        # wall-clock span; recording release + dock start afterwards
        # refreshes the DRAFT to the dock span (45 min).
        stop5 = self._stop(120)
        item5 = self._suggest(stop5)
        self.assertEqual(item5.actual_dwell_minutes, 120)
        self.assertEqual(item5.suggested_amount, 75.0)
        stop5.write({"dock_start_at": "2026-09-07 10:00:00",
                     "released_at": "2026-09-07 10:45:00"})
        self._suggest(stop5)
        self.assertEqual(item5.actual_dwell_minutes, 45)
        self.assertEqual(item5.suggested_amount, 25.0)
        self.assertEqual(item5.state, "draft")

        # ... and a backfill that lands INSIDE the free window zeroes the
        # stale suggestion instead of keeping it (never stale charges).
        stop6 = self._stop(120)
        item6 = self._suggest(stop6)
        self.assertEqual(item6.suggested_amount, 75.0)
        stop6.write({"dock_start_at": "2026-09-07 10:00:00",
                     "released_at": "2026-09-07 10:15:00"})  # 15 min ≤ free
        self._suggest(stop6)
        self.assertEqual(item6.actual_dwell_minutes, 15)
        self.assertEqual(item6.units, 0)
        self.assertEqual(item6.suggested_amount, 0.0)

        # Reviewed items stay immutable through re-suggestion.
        item5.action_approve()
        self._suggest(stop5)
        self.assertEqual(item5.state, "approved")
        self.assertEqual(item5.approved_amount, 25.0)
        self.assertEqual(item5.actual_dwell_minutes, 45)

    # ── D4: stop timing recorder ───────────────────────────────────

    def test_d4_action_record_timing_order_and_refresh(self):
        """action_record_timing stamps events in order, refuses inverted
        ones, and refreshes a completed stop's draft item."""
        rule = self._rule()
        stop = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": "dropoff",
            "status": "arrived",
            "saved_location_id": self.facility.id,
            "actual_arrival_time": "2026-09-07 09:00:00",
        })
        Stop = self.env["prema.dispatch.stop"]

        # Forward recording order is legal (regression: the order guard
        # used to reject normal forward records).
        res = stop.action_record_timing(
            "check_in", "2026-09-07 09:10:00")
        self.assertTrue(res["success"])
        stop.action_record_timing("dock_start", "2026-09-07 10:00:00")
        stop.action_record_timing("release", "2026-09-07 10:45:00")
        fmt = "%Y-%m-%d %H:%M:%S"
        self.assertEqual(stop.check_in_at.strftime(fmt), "2026-09-07 09:10:00")
        self.assertEqual(stop.dock_start_at.strftime(fmt),
                         "2026-09-07 10:00:00")
        self.assertEqual(stop.released_at.strftime(fmt),
                         "2026-09-07 10:45:00")

        # Inverted records raise.
        with self.assertRaises(UserError):
            stop.action_record_timing("dock_start", "2026-09-07 08:00:00")
        with self.assertRaises(UserError):
            stop.action_record_timing("release", "2026-09-07 09:30:00")
        with self.assertRaises(UserError):
            stop.action_record_timing("check_in", "2026-09-07 06:00:00")

        # Completed stop: draft items refresh on each recorded event.
        done = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": "dropoff",
            "status": "completed",
            "saved_location_id": self.facility.id,
            "actual_arrival_time": "2026-09-07 09:00:00",
            "actual_departure_time": "2026-09-07 11:00:00",
        })
        item = self._suggest(done)
        self.assertEqual(item.actual_dwell_minutes, 120)
        done.action_record_timing("dock_start", "2026-09-07 10:00:00")
        # release lands on a completed stop before its departure: span
        # switches to the dock span (10:00 → 10:30 = 30 min, free window).
        done.action_record_timing("release", "2026-09-07 10:30:00")
        self.env.invalidate_all()
        self.assertEqual(item.actual_dwell_minutes, 30)
        self.assertEqual(item.suggested_amount, 0.0)

    # ── D5: evidence-required gate ─────────────────────────────────

    def test_d5_evidence_required_gates_approval(self):
        """Approve/Modify need linked live evidence when the rule flags
        it; capture auto-links; Waive never needs evidence."""
        rule = self._rule(evidence=True)
        item = self._suggest(self._stop(95))
        self.assertTrue(item.evidence_required)
        with self.assertRaises(UserError):
            item.action_approve()
        with self.assertRaises(UserError):
            item.action_modify()

        # Waive stays available without evidence.
        item.action_waive()
        self.assertEqual(item.state, "waived")

        item2 = self._suggest(self._stop(95))
        # Evidence for the stop auto-links to the item at capture.
        ev = self._evidence(item2.stop_id, "issue_photo")
        self.assertIn(ev.id, item2.evidence_ids.ids)
        # Failed-state (or superseded) proof is never linked.
        failed = self._evidence(item2.stop_id, "issue_photo",
                                upload_state="failed")
        self.assertNotIn(failed.id, item2.evidence_ids.ids)
        item2.action_approve()
        self.assertEqual(item2.state, "approved")
        self.assertEqual(item2.approved_amount, 75.0)

        # Modify is gated the same way.
        item3 = self._suggest(self._stop(95))
        item3.write({"approved_amount": 60.0})
        with self.assertRaises(UserError):
            item3.action_modify()
        self._evidence(item3.stop_id)
        item3.action_modify()
        self.assertEqual(item3.state, "modified")

        # Evidence captured BEFORE the item exists links at suggestion.
        stop4 = self._stop(95)
        pre = self._evidence(stop4)
        item4 = self._suggest(stop4)
        self.assertIn(pre.id, item4.evidence_ids.ids)

    # ── D6: exception catalog neutrality ───────────────────────────

    def test_d6_exception_catalog_is_neutral(self):
        """exception_type is catalog-only: the suggestion never changes
        and nothing auto-approves."""
        self._rule()
        item = self._suggest(self._stop(95))
        self.assertEqual(item.suggested_amount, 75.0)
        for code in ("traffic", "weather", "facility_delay",
                     "customer_delay", "equipment", "driver", "other"):
            item.write({"exception_type": code})
            self.assertEqual(item.suggested_amount, 75.0,
                             "catalog code %s rewrote the charge" % code)
            self.assertEqual(item.state, "draft",
                             "catalog code %s auto-approved" % code)
        item.action_approve()
        self.assertEqual(item.state, "approved")
        self.assertEqual(item.approved_amount, 75.0)
        self.assertEqual(item.exception_type, "other")

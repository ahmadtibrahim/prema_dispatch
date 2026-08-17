"""Route Adviser — time-aware milk-run review and manual route validation.

Covers: current-vs-recommended metrics, apply, hard-window protection,
capacity blocking, closed-facility blocking with authorized override,
locked/completed stop protection, and delivery-before-pickup blocking.
"""
import datetime

from odoo.tests import TransactionCase

from odoo.addons.prema_dispatch.services.route_adviser_service import (
    RouteAdviserService,
)


def _dt(hour, minute=0):
    # Naive UTC (Odoo convention; container TZ is UTC): 12:00 = 08:00
    # Toronto (EDT) — deterministic timezone conversions.
    return datetime.datetime(2026, 8, 19, 12, 0) + datetime.timedelta(
        hours=hour - 12, minutes=minute
    )


class TestRouteAdviser(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].search([], limit=1)
        cls.stage_draft = cls.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1)

    def _make_job(self):
        # scheduled_pickup is a base prema.dispatch.job field — booking
        # extension fields (operation_date) are NOT available during this
        # module's test phase.
        return self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "stage_id": self.stage_draft.id,
            "scheduled_pickup": _dt(12, 0),
        })

    def _add_stop(self, job, stop_type, name, lat, lng, seq, hours=None,
                  timing="flexible", window_start=None, window_end=None,
                  exact_hour=None, pallets_in=0, pallets_out=0):
        values = {
            "job_id": job.id,
            "sequence": seq,
            "stop_type": stop_type,
            "address": name,
            "latitude": lat,
            "longitude": lng,
            "pallets_in": pallets_in,
            "pallets_out": pallets_out,
            "time_window_type": timing,
            "tz_name": "America/Toronto",
        }
        if hours is not None:
            values["operating_hours_snapshot"] = hours
        if window_start is not None:
            values["earliest_time"] = _dt(window_start)
        if window_end is not None:
            values["latest_time"] = _dt(window_end)
        if exact_hour is not None:
            values["exact_time"] = _dt(exact_hour)
        return self.env["prema.dispatch.stop"].create(values)

    def _add_item(self, job, name, pickup_stop, delivery_stop, shared=False,
                  extra_deliveries=None):
        item = self.env["prema.dispatch.item"].create({
            "job_id": job.id,
            "name": name,
            "weight_lbs": 500.0,
            "pickup_stop_id": pickup_stop.id,
            "delivery_stop_id": delivery_stop.id,
            "shared_skid": shared,
        })
        allocations = [delivery_stop] + list(extra_deliveries or [])
        if len(allocations) > 1:
            item.stop_allocation_ids = [(0, 0, {
                "stop_id": stop.id,
                "unload_sequence": (index + 1) * 10,
            }) for index, stop in enumerate(allocations)]
        return item

    def _canonical_job(self, hours_ud=None, hours_tf=None, hours_blv=None,
                       hours_ott=None, compact=False):
        """UD pickup (4 → Ottawa), TF pickup (3 → Belleville).

        compact=True places the four stops ~20 km apart so travel times
        stay inside a deterministic same-day window for time tests."""
        open_24 = {str(d): [0.0, 24.0] for d in range(7)}
        coords = {
            "ud": (43.70, -79.70) if compact else (44.23, -76.49),
            "tf": (43.80, -79.60) if compact else (43.73, -79.76),
            "blv": (43.90, -79.50) if compact else (44.16, -77.38),
            "ott": (44.00, -79.40) if compact else (45.42, -75.70),
        }
        job = self._make_job()
        ud = self._add_stop(job, "pickup", "United Dairy", *coords["ud"], 10,
                            hours=hours_ud if hours_ud is not None else open_24,
                            pallets_in=4)
        tf = self._add_stop(job, "pickup", "TerraFreska", *coords["tf"], 20,
                            hours=hours_tf if hours_tf is not None else open_24,
                            pallets_in=3)
        blv = self._add_stop(job, "dropoff", "Belleville Depot", *coords["blv"], 30,
                             hours=hours_blv if hours_blv is not None else open_24,
                             pallets_out=3)
        ott = self._add_stop(job, "dropoff", "Ottawa DC", *coords["ott"], 40,
                             hours=hours_ott if hours_ott is not None else open_24,
                             pallets_out=4)
        for i in range(4):
            self._add_item(job, "U-%02d" % (i + 1), ud, ott)
        for i in range(3):
            self._add_item(job, "TF-%02d" % (i + 1), tf, blv)
        return job, ud, tf, blv, ott

    # ── Adviser: current vs recommended ─────────────────────────────

    def test_01_adviser_recommends_precedence_order(self):
        job, ud, tf, blv, ott = self._canonical_job()
        # Current order is wrong: deliveries before their pickups.
        blv.write({"sequence": 10})
        ott.write({"sequence": 20})
        ud.write({"sequence": 30})
        tf.write({"sequence": 40})
        report = RouteAdviserService(self.env).adviser_report(job)
        self.assertTrue(report["feasible"])
        keys = report["recommended_keys"]
        self.assertEqual(len(keys), 4)
        # Precedence: every pickup precedes its deliveries; first stop is
        # a pickup. (The exact greedy order between equally-legal stops
        # is deterministic but not contractual.)
        self.assertEqual(keys[0], "ds%d" % ud.id)
        self.assertLess(keys.index("ds%d" % ud.id), keys.index("ds%d" % ott.id))
        self.assertLess(keys.index("ds%d" % tf.id), keys.index("ds%d" % blv.id))
        self.assertFalse(report["current"]["feasible"])
        self.assertLessEqual(report["recommended"]["peak"], 7)
        self.assertIn("distance_km", report["recommended"])
        self.assertGreaterEqual(report["recommended"]["distance_km"], 0)
        # Recommended steps carry per-stop timing.
        self.assertEqual(len(report["recommended"]["steps"]), 4)
        self.assertEqual(
            report["recommended"]["steps"][0]["stop_type"], "pickup")

    def test_02_apply_recommended_writes_sequence(self):
        job, ud, tf, blv, ott = self._canonical_job()
        blv.write({"sequence": 10})
        ott.write({"sequence": 20})
        ud.write({"sequence": 30})
        tf.write({"sequence": 40})
        report = RouteAdviserService(self.env).adviser_report(job)
        result = RouteAdviserService(self.env).apply_recommended_route(job)
        self.assertTrue(result["success"])
        self.assertEqual(result["applied"], 4)
        applied_ids = job.stop_ids.sorted("sequence").ids
        self.assertEqual(
            [("ds%d" % sid) for sid in applied_ids],
            report["recommended_keys"],
        )

    # ── Manual validation: hard-invalid blocks ──────────────────────

    def test_03_validation_blocks_delivery_before_pickup(self):
        job, ud, tf, blv, ott = self._canonical_job()
        order = [blv.id, ott.id, ud.id, tf.id]
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertFalse(result["valid"])
        self.assertTrue(any("onboard" in e.lower() for e in result["errors"]))

    def test_04_validation_blocks_capacity_exceeded(self):
        job, ud, tf, blv, ott = self._canonical_job()
        model = self.env["fleet.vehicle.model"].search([], limit=1)
        if not model:
            model = self.env["fleet.vehicle.model"].create({
                "name": "Test Van Model",
            })
        vehicle = self.env["fleet.vehicle"].create({
            "name": "Small Van",
            "model_id": model.id,
            "x_max_pallets": 6,
        })
        job.write({"vehicle_id": vehicle.id})
        order = [ud.id, tf.id, blv.id, ott.id]  # peak 7 > 6
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertFalse(result["valid"])
        self.assertTrue(any("capacity" in e.lower() or "peak" in e.lower()
                            for e in result["errors"]))

    def test_05_validation_blocks_closed_facility(self):
        closed = {str(d): None for d in range(7)}
        job, ud, tf, blv, ott = self._canonical_job(hours_blv=closed)
        order = [ud.id, tf.id, blv.id, ott.id]
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Closed facility" in e for e in result["errors"]))
        # Adviser cannot produce a feasible route either.
        report = RouteAdviserService(self.env).adviser_report(job)
        self.assertFalse(report["feasible"])

    def test_06_authorized_hours_override_unblocks(self):
        closed = {str(d): None for d in range(7)}
        job, ud, tf, blv, ott = self._canonical_job(hours_blv=closed)
        self.env["prema.dispatch.hours.override"].create({
            "job_id": job.id,
            "stop_id": blv.id,
            "reason": "Customer agreed to after-hours receiving.",
            "user_id": self.env.user.id,
        })
        order = [ud.id, tf.id, blv.id, ott.id]
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(any("override" in w.lower() for w in result["warnings"]))

    def test_07_validation_blocks_moved_completed_stop(self):
        job, ud, tf, blv, ott = self._canonical_job()
        ott.write({"status": "completed"})
        order = [ud.id, tf.id, ott.id, blv.id]  # ott moved
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertFalse(result["valid"])
        self.assertTrue(any("locked" in e.lower() or "completed" in e.lower()
                            for e in result["errors"]))

    def test_08_validation_blocks_impossible_appointment(self):
        job, ud, tf, blv, ott = self._canonical_job(compact=True)
        # Ottawa has an exact appointment at 08:00 Toronto (12:00 UTC).
        # The route starts at 08:00 Toronto and reaches Ottawa ~09:50 —
        # after the appointment, same day → impossible.
        ott.write({
            "time_window_type": "exact",
            "exact_time": _dt(12, 0),
        })
        order = [ud.id, tf.id, blv.id, ott.id]
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertFalse(result["valid"])
        self.assertTrue(any("appointment" in e.lower() for e in result["errors"]))

    # ── Valid manual route: warnings only ───────────────────────────

    def test_09_valid_manual_route_warns_with_metrics(self):
        job, ud, tf, blv, ott = self._canonical_job()
        # Valid but silly order: UD → BLV (nothing to unload there yet is
        # legal? No — BLV has 0 onboard deliveries from UD, but the TF
        # pallets aren't picked up yet, so BLV-before-TF is blocked.)
        # Use a legal-but-inefficient order instead: UD → TF → OTT → BLV.
        order = [ud.id, tf.id, ott.id, blv.id]
        result = RouteAdviserService(self.env).validate_manual_route(job, order)
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(any("km" in w for w in result["warnings"]))
        self.assertIn("metrics", result)
        self.assertEqual(result["metrics"]["peak"], 7)

    # ── Wizard action ───────────────────────────────────────────────

    def test_10_wizard_action_builds_lines(self):
        job, ud, tf, blv, ott = self._canonical_job()
        action = job.action_route_adviser()
        self.assertEqual(action["res_model"], "prema.dispatch.route.adviser")
        adviser = self.env["prema.dispatch.route.adviser"].browse(action["res_id"])
        self.assertEqual(adviser.job_id, job)
        self.assertEqual(len(adviser.line_ids), 4)
        self.assertTrue(adviser.feasible)
        self.assertEqual(adviser.current_peak, 7)
        first_line = adviser.line_ids.sorted("sequence")[0]
        self.assertIn("United Dairy", first_line.stop_name)
        self.assertEqual(first_line.onboard_after, 4)

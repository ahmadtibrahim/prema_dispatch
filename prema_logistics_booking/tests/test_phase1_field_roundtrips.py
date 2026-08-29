"""Phase 1 targeted tests — hours bulk editor wizard, split-window
snapshot merge, per-stop timing thread-through (work order P1-6/7/8).

Every test rolls back — nothing commits. No corridor/departure data
required (unlike the UAT matrix tests).
"""

from datetime import date, datetime

from odoo.tests.common import TransactionCase

from ..services.itinerary_planner import _snapshot_from_rows


class TestHoursSnapshotSplitWindow(TransactionCase):
    """P1-6: _snapshot_from_rows merges split windows instead of using
    only the first row."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.facility = cls.env["prema.dispatch.location"].create({
            "name": "P1 Snapshot Test Facility",
            "address": "1 Test Street",
        })

    def _make(self, day, status, open_t, close_t, sequence=10, scope="general"):
        return self.env["prema.dispatch.location.hours"].create({
            "facility_id": self.facility.id,
            "day_of_week": day,
            "service_scope": scope,
            "status": status,
            "open_time": open_t,
            "close_time": close_t,
            "sequence": sequence,
        })

    def test_split_window_merges_to_one_span(self):
        self._make("0", "custom", 7.0, 11.0, sequence=10)
        self._make("0", "custom", 13.0, 17.0, sequence=20)
        rows = self.env["prema.dispatch.location.hours"].search(
            [("facility_id", "=", self.facility.id), ("active", "=", True)])
        self.assertEqual(_snapshot_from_rows(rows, "0", ["pickup"]), [7.0, 17.0])

    def test_closed_row_anywhere_wins(self):
        self._make("1", "custom", 7.0, 11.0, sequence=10)
        self._make("1", "closed", 0.0, 0.0, sequence=20)
        rows = self.env["prema.dispatch.location.hours"].search(
            [("facility_id", "=", self.facility.id), ("active", "=", True)])
        self.assertIsNone(_snapshot_from_rows(rows, "1", ["pickup"]))

    def test_open_24h_row_makes_day_24h(self):
        self._make("2", "open_24h", 0.0, 24.0, sequence=10)
        self._make("2", "custom", 13.0, 17.0, sequence=20)
        rows = self.env["prema.dispatch.location.hours"].search(
            [("facility_id", "=", self.facility.id), ("active", "=", True)])
        self.assertEqual(_snapshot_from_rows(rows, "2", ["pickup"]), [0.0, 24.0])

    def test_missing_day_is_closed(self):
        rows = self.env["prema.dispatch.location.hours"].search(
            [("facility_id", "=", self.facility.id), ("active", "=", True)])
        self.assertIsNone(_snapshot_from_rows(rows, "3", ["pickup"]))


class TestHoursBulkWizard(TransactionCase):
    """P1-6: prema.dispatch.location.hours.wizard applies one template
    across many facilities with audit stamps; old rows deactivated."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Facility = cls.env["prema.dispatch.location"]
        cls.f1 = Facility.create({"name": "P1 Wiz A", "address": "2 Test Street"})
        cls.f2 = Facility.create({"name": "P1 Wiz B", "address": "3 Test Street"})

    def test_apply_hours_creates_stamped_rows_and_deactivates_old(self):
        Hours = self.env["prema.dispatch.location.hours"]
        # Pre-existing Sunday row on f1 must survive (day not selected).
        Hours.create({
            "facility_id": self.f1.id, "day_of_week": "6",
            "service_scope": "general", "status": "closed",
            "open_time": 0.0, "close_time": 0.0, "sequence": 10,
            "source": "backend",
        })
        # Stale Monday row to be replaced.
        Hours.create({
            "facility_id": self.f1.id, "day_of_week": "0",
            "service_scope": "general", "status": "custom",
            "open_time": 6.0, "close_time": 18.0, "sequence": 10,
            "source": "backend",
        })

        wiz = self.env["prema.dispatch.location.hours.wizard"].create({
            "facility_ids": [(6, 0, [self.f1.id, self.f2.id])],
            "service_scope": "general",
            "apply_monday": True, "apply_tuesday": True, "apply_wednesday": True,
            "apply_thursday": True, "apply_friday": True,
            "apply_saturday": False, "apply_sunday": False,
            "day_status": "open_24h",
        })
        result = wiz.apply_hours()

        # Result re-opens the Master Facilities list filtered to the pair.
        self.assertEqual(result["res_model"], "prema.dispatch.location")
        self.assertEqual(set(result["domain"][0][2]), {self.f1.id, self.f2.id})

        active = Hours.search([("facility_id", "in", [self.f1.id, self.f2.id]),
                               ("active", "=", True)])
        by_fac = {}
        for row in active:
            by_fac.setdefault(row.facility_id.id, []).append(row.day_of_week)
        # 5 weekdays × 2 facilities, all stamped. f1 keeps its pre-existing
        # Sunday row (day not selected), f2 gets exactly the 5 weekdays.
        self.assertEqual(sorted(by_fac[self.f1.id]), ["0", "1", "2", "3", "4", "6"])
        self.assertEqual(sorted(by_fac[self.f2.id]), ["0", "1", "2", "3", "4"])
        # All wizard-created weekdays stamped; the surviving Sunday row
        # (asserted below) keeps its own backend source.
        wizard_days = active.filtered(lambda r: r.day_of_week != "6")
        self.assertTrue(all(r.source == "wizard" for r in wizard_days))
        self.assertTrue(all(r.changed_by.id == self.env.user.id for r in wizard_days))
        self.assertTrue(all(r.changed_at for r in wizard_days))
        self.assertTrue(all(r.status == "open_24h" for r in wizard_days))

        # Old Monday row deactivated, Sunday row untouched.
        old_monday = Hours.search(
            [("facility_id", "=", self.f1.id), ("day_of_week", "=", "0"),
             ("active", "=", False)])
        self.assertTrue(old_monday)
        sunday = Hours.search(
            [("facility_id", "=", self.f1.id), ("day_of_week", "=", "6"),
             ("active", "=", True)])
        self.assertEqual(len(sunday), 1)
        self.assertEqual(sunday.source, "backend")

    def test_custom_hours_require_times(self):
        wiz = self.env["prema.dispatch.location.hours.wizard"].create({
            "facility_ids": [(6, 0, [self.f1.id])],
            "apply_monday": True,
            "day_status": "custom",
            "open_time": 0.0, "close_time": 0.0,
        })
        with self.assertRaises(ValueError):
            wiz.apply_hours()


class TestDispatchTimingVals(TransactionCase):
    """P1-8: booking stop timing maps to dispatch stop timing fields."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env["res.partner"].create({"name": "P1 Timing Test"})
        cls.booking = cls.env["logistics.booking"].create({
            "partner_id": partner.id,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 1,
            "weight_lbs": 500.0,
        })

    def _stop(self, **kwargs):
        vals = {"booking_id": self.booking.id, "sequence": 10,
                "stop_type": "delivery"}
        vals.update(kwargs)
        return self.env["logistics.booking.stop"].create(vals)

    def test_time_window_maps_earliest_latest(self):
        stop = self._stop(
            timing_type="time_window", window_start=8.5, window_end=17.0)
        day = date(2026, 8, 28)
        vals = stop._dispatch_timing_vals(day)
        self.assertEqual(vals["time_window_type"], "window")
        self.assertEqual(vals["earliest_time"], datetime(2026, 8, 28, 8, 30))
        self.assertEqual(vals["latest_time"], datetime(2026, 8, 28, 17, 0))

    def test_exact_appointment_maps_exact_time(self):
        stop = self._stop(timing_type="exact_appointment", appointment_time=10.0)
        vals = stop._dispatch_timing_vals(date(2026, 8, 28))
        self.assertEqual(vals["time_window_type"], "exact")
        self.assertEqual(vals["exact_time"], datetime(2026, 8, 28, 10, 0))

    def test_deadline_maps_deadline_time(self):
        dl = datetime(2026, 8, 28, 12, 0)
        stop = self._stop(timing_type="deadline", hard_deadline=dl)
        vals = stop._dispatch_timing_vals(date(2026, 8, 28))
        self.assertEqual(vals["time_window_type"], "deadline")
        self.assertEqual(vals["deadline_time"], dl)
        self.assertTrue(vals["hard_deadline"])

    def test_flexible_default(self):
        stop = self._stop()
        vals = stop._dispatch_timing_vals(date(2026, 8, 28))
        self.assertEqual(vals["time_window_type"], "flexible")
        self.assertNotIn("earliest_time", vals)
        self.assertNotIn("exact_time", vals)

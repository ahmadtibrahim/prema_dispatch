"""Phases 3-10 targeted verification — matrix A-J (ONE run, all rolled back).

Runs against a Prod-db copy: the REAL c15 Monday SWON corridor and its
scheduled departures are the live production configuration, so tests
assert against them (toggles inside the transaction roll back). Every
test rolls back — nothing commits, nothing touches Prod-db.

Matrix:
  A  P3  Sunday prior-day pickup binds the SINGLE Monday c15 departure
  B  P4  Monday feeder + Tuesday onward via the hub (multi-leg chain)
  C  P5  Hub Arrival/Return Time — manual preserved, suggested recalculated
  D  P6  Recommended Operational Start — backward calc, buffer, position
  E  P7  Facility hours scope chain (pickup→shipping→general, receiving)
  F  P8  Operational classification hierarchy (manual > history > type)
  G  P9  Historical dwell reuse (median + running unload, one hierarchy)
  H  P10 Detention appends to the booking's existing DRAFT invoice
  J  P9/10 support — actual-distance pricing + facility ETA service time
"""

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
    ItineraryPlanner,
    snapshot_facility_hours,
)
from odoo.addons.prema_logistics_booking.services.shipment_routing_service import (
    ShipmentRoutingService,
)

from pytz import UTC as utc_tz


class TestPhasesThreeTen(TransactionCase):
    """Phases 3-10 batch verification (booking-module side)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Param = env["ir.config_parameter"].sudo()
        # Migrations do not run in test DBs — seed the two single-authority
        # parameters explicitly (the same values the migrations seed).
        Param.set_param("prema_dispatch.service_time_defaults", json.dumps({
            "retail": 15,
            "warehouse": 30,
            "distribution_center": 60,
            "grocery_dc": 60,
        }))
        Param.set_param("prema_dispatch.detention_defaults", json.dumps({
            "free_minutes": 30,
            "increment_minutes": 30,
            "rate_per_increment": 0.0,
        }))
        cls.svc = ShipmentRoutingService(env)
        cls.partner = env["res.partner"].create(
            {"name": "Phases 3-10 Test Customer"})
        cls.canada = env["res.country"].search([("code", "=", "CA")], limit=1)
        cls.ontario = env["res.country.state"].search(
            [("code", "=", "ON"), ("country_id", "=", cls.canada.id)],
            limit=1) if cls.canada else env["res.country.state"]
        if cls.canada and not cls.canada.logistics_network_enabled:
            cls.canada.logistics_network_enabled = True
        if cls.ontario and not cls.ontario.logistics_network_enabled:
            cls.ontario.logistics_network_enabled = True
        cls.hub = env["logistics.hub"].search(
            [("name", "ilike", "%Mississauga%")], limit=1)
        cls.vehicle = cls._make_vehicle()
        # Mid-loading test phase: prema_logistics_booking loads AFTER
        # prema_dispatch, so logistics.booking is not yet in the model
        # pool and these comodels were set to _unknown. The final
        # registry.setup_models pass fixes them at boot in production —
        # repair them here so the mid-load tests can run.
        for model, field in (
                ("prema.dispatch.detention.item", "booking_id"),
                ("prema.dispatch.job", "logistics_booking_id")):
            f = env[model]._fields.get(field)
            if f and f.comodel_name == "_unknown":
                f.comodel_name = "logistics.booking"

    # ── Fixture helpers ────────────────────────────────────────────

    @classmethod
    def _make_vehicle(cls):
        env = cls.env
        brand = env["fleet.vehicle.model.brand"].create(
            {"name": "Phases 3-10 Test Brand"})
        model = env["fleet.vehicle.model"].create(
            {"name": "Phases 3-10 Test Model", "brand_id": brand.id})
        return env["fleet.vehicle"].create({
            "model_id": model.id,
            "license_plate": "TEST-3-10",
            "odometer_unit": "kilometers",
            "power_unit": "power",
            "driver_id": cls.partner.id,
        })

    @classmethod
    def _region(cls, code, lat=None, lng=None):
        """Official-LTL region by code, created on demand with an approved
        polygon around (lat, lng) so coordinate resolution works."""
        env = cls.env
        region = env["logistics.region"].search([("code", "=", code)], limit=1)
        if region:
            return region
        assert cls.canada and cls.ontario, "Canada/Ontario required for regions"
        vals = {
            "code": code,
            "name": code,
            "country_id": cls.canada.id,
            "state_id": cls.ontario.id,
            "is_official_ltl_region": True,
            "boundary_status": "approved",
        }
        if lat is not None:
            poly = [
                [lng - 0.5, lat - 0.5], [lng + 0.5, lat - 0.5],
                [lng + 0.5, lat + 0.5], [lng - 0.5, lat + 0.5],
                [lng - 0.5, lat - 0.5],
            ]
            vals["polygon_geojson"] = json.dumps(
                {"type": "Polygon", "coordinates": [poly]})
        return env["logistics.region"].create(vals)

    @classmethod
    def _make_departure(cls, corridor, dep_date):
        return cls.env["logistics.corridor.departure"].create({
            "corridor_id": corridor.id,
            "departure_date": dep_date,
            "vehicle_id": cls.vehicle.id,
        })

    @staticmethod
    def _stops(pickup, delivery):
        return [
            {"stop_type": "pickup", "latitude": pickup[0],
             "longitude": pickup[1], "city": "Pickup", "stop_key": "p"},
            {"stop_type": "delivery", "latitude": delivery[0],
             "longitude": delivery[1], "city": "Delivery", "stop_key": "d"},
        ]

    @classmethod
    def _location(cls, name, lat=43.6, lng=-79.4, **extra):
        # Address must be unique per created location — the normalized
        # address key has a unique constraint.
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        vals = {"name": name,
                "address": "123 Test St #%d, Ontario" % cls._loc_n,
                "pin_lat": lat, "pin_lng": lng}
        vals.update(extra)
        return cls.env["prema.dispatch.location"].create(vals)

    @classmethod
    def _hours_all_days(cls, facility, scope, open_t, close_t):
        return cls.env["prema.dispatch.location.hours"].create([
            {"facility_id": facility.id, "day_of_week": day,
             "service_scope": scope, "status": "custom",
             "open_time": open_t, "close_time": close_t}
            for day in ("0", "1", "2", "3", "4", "5", "6")
        ])

    # ── A: P3 prior-day pickup on the REAL c15 ──────────────────────

    def test_a_prior_day_pickup_c15(self):
        """Sunday physical pickup → Monday linehaul on the real c15: the
        Sunday calendar entry binds the Monday departure (single capacity
        pool — never a second Sunday departure)."""
        c15 = self.env["logistics.corridor"].search([
            ("name", "ilike", "%SW Ontario / Windsor%"),
            ("operate_monday", "=", True),
            ("same_day_return", "=", True),
            ("active", "in", (True, False)),
        ], limit=1)
        self.assertTrue(c15, "c15 Monday SWON corridor must exist in Prod-db")
        monday = date(2026, 9, 7)
        sunday = date(2026, 9, 6)

        # The REAL scheduled Monday departure is the authority — exactly
        # one active departure exists per date on the live network.
        dep = self.env["logistics.corridor.departure"].search([
            ("corridor_id", "=", c15.id),
            ("departure_date", "=", monday),
            ("active", "=", True),
        ], limit=1)
        self.assertTrue(dep, "c15 Monday departure must exist")

        # Enable prior-day pickup on c15 (the prod migration does the same,
        # restricted to this one corridor — never c12).
        c15.write({
            "allow_prior_day_pickup": True,
            "prior_day_pickup_max_days": 1,
            "prior_day_pickup_hub_id": self.hub.id,
        })
        stops = self._stops((43.6532, -79.3832), (42.3149, -83.0364))
        result = self.svc.calendar_availability(stops)
        self.assertFalse(result["manual_quote"], result.get("reason"))
        entries = {e["date"]: e for e in result["dates"]}

        # Same-day Monday service binds the single Monday departure.
        mon = entries.get(monday.isoformat())
        self.assertTrue(mon, "Monday same-day entry must exist")
        self.assertFalse(mon["prior_day_pickup"])
        self.assertEqual(mon["departure_id"], dep.id)
        self.assertEqual(mon["departure_date"], monday.isoformat())

        # Sunday prior-day pickup binds the SAME Monday departure.
        sun = entries.get(sunday.isoformat())
        self.assertTrue(sun, "Sunday prior-day entry must exist")
        self.assertTrue(sun["prior_day_pickup"])
        self.assertEqual(sun["departure_id"], dep.id)
        self.assertEqual(sun["departure_date"], monday.isoformat())
        self.assertEqual(sun["corridor_departure_date"], monday.isoformat())

        # No departure is ever dated Sunday (single Monday pool).
        for e in result["dates"]:
            self.assertNotEqual(
                e["departure_date"], sunday.isoformat(),
                "no departure may be dated Sunday")

        # Prior-day OFF → the Sunday date disappears, Monday stays.
        c15.write({"allow_prior_day_pickup": False})
        result2 = self.svc.calendar_availability(stops)
        dates2 = {e["date"] for e in result2["dates"]}
        self.assertNotIn(sunday.isoformat(), dates2)
        self.assertIn(monday.isoformat(), dates2)

    # ── B: P4 Monday→Tuesday multi-leg ──────────────────────────────

    def test_b_monday_tuesday_multileg(self):
        """Feeder Monday + onward Tuesday via the hub — ONE planning chain
        (never two calculators), custody hold ≤ 24h."""
        # Remote northern coordinates — far outside every real corridor
        # polygon, so the resolver matches ONLY the test squares (a
        # southern pick would collide with real region polygons and be
        # ambiguous).
        reg_a = self._region("ON-TEST-A", lat=48.5, lng=-81.5)
        reg_d = self._region("ON-TEST-D", lat=48.9, lng=-80.9)
        gta = self._region("ON-GTA")
        Corridor = self.env["logistics.corridor"].with_context(
            skip_departure_reconcile=True)  # fixture: never reconcile horizons
        Stop = self.env["logistics.corridor.stop"]

        feeder = Corridor.create({
            "name": "TEST feeder A→Hub", "direction": "eastbound",
            "equipment_type": "dry", "operate_monday": True,
            "start_time": 4.0, "same_day_return": False,
        })
        Stop.create([
            {"corridor_id": feeder.id, "sequence": 10, "region_id": reg_a.id,
             "pickup_allowed": True, "delivery_allowed": False,
             "distance_from_origin_km": 0.0},
            {"corridor_id": feeder.id, "sequence": 20, "region_id": gta.id,
             "pickup_allowed": False, "delivery_allowed": True,
             "distance_from_origin_km": 120.0},
        ])
        onward = Corridor.create({
            "name": "TEST onward Hub→D", "direction": "eastbound",
            "equipment_type": "dry", "operate_tuesday": True,
            "start_time": 4.0, "same_day_return": False,
        })
        Stop.create([
            {"corridor_id": onward.id, "sequence": 10, "region_id": gta.id,
             "pickup_allowed": True, "delivery_allowed": False,
             "distance_from_origin_km": 0.0},
            {"corridor_id": onward.id, "sequence": 20, "region_id": reg_d.id,
             "pickup_allowed": False, "delivery_allowed": True,
             "distance_from_origin_km": 120.0},
        ])
        dep_feeder = self._make_departure(feeder, date(2026, 9, 7))
        dep_onward = self._make_departure(onward, date(2026, 9, 8))

        route = self.svc.plan_route(
            pickup_lat=48.5, pickup_lng=-81.5,
            delivery_lat=48.9, delivery_lng=-80.9,
            pallets=1, weight_lbs=500,
            requested_pickup_date="2026-09-07",
            equipment="dry", shipment_type="ltl",
        )
        self.assertTrue(route.available, route.reason)
        self.assertEqual(len(route.legs), 2)
        leg1, leg2 = route.legs
        self.assertEqual(leg1.leg_type, "feeder_to_hub")
        self.assertEqual(leg1.corridor_id, feeder.id)
        self.assertEqual(leg1.departure_id, dep_feeder.id)
        self.assertEqual(self.svc._parse_iso_dt(leg1.pickup_datetime).date(),
                         date(2026, 9, 7))
        self.assertEqual(leg2.leg_type, "final_mile")
        self.assertEqual(leg2.corridor_id, onward.id)
        self.assertEqual(leg2.departure_id, dep_onward.id)
        dep2_dt = self.svc._parse_iso_dt(leg2.corridor_departure_datetime)
        self.assertEqual(dep2_dt.date(), date(2026, 9, 8),
                         "onward leg must ride the Tuesday departure")
        # Custody hold: feeder arrival at hub → onward departure ≤ 24h.
        leg1_arr = self.svc._parse_iso_dt(leg1.delivery_datetime)
        hold_hours = (dep2_dt - leg1_arr).total_seconds() / 3600.0
        self.assertLessEqual(hold_hours, 24.0)

    # ── C: P5 Hub Arrival / Return Time ─────────────────────────────

    def test_c_hub_arrival_manual_preserved(self):
        """Manual stops and a manual hub arrival survive Calculate Route
        Times; a suggested hub arrival is recalculated; editing a planned
        time flips the source to Manual Override."""
        corr = self.env["logistics.corridor"].with_context(
            skip_departure_reconcile=True).create({
                "name": "TEST timing corridor", "direction": "eastbound",
                "equipment_type": "dry", "operate_monday": True,
                "start_time": 4.0, "same_day_return": False,
                "destination_hub_id": self.hub.id,
            })
        Stop = self.env["logistics.corridor.stop"]
        stop_a = Stop.create({"corridor_id": corr.id, "sequence": 10})
        stop_manual = Stop.create({"corridor_id": corr.id, "sequence": 20})
        # Manual edits flip the source flag automatically.
        stop_manual.write({"planned_arrival_time": 8.0,
                           "planned_departure_time": 8.25})
        self.assertEqual(stop_manual.timing_source, "manual")
        corr.write({"destination_hub_arrival_time": 15.0})
        self.assertEqual(corr.hub_arrival_time_source, "manual")

        targets = [("stop", stop_a), ("stop", stop_manual)]
        legs = [{"drive_minutes": 60}, {"drive_minutes": 60}]
        corr._apply_suggested_timing([stop_a], targets, legs)

        self.assertEqual(stop_a.planned_arrival_time, 5.0)
        self.assertEqual(stop_a.timing_source, "suggested")
        self.assertEqual(stop_manual.planned_arrival_time, 8.0,
                         "manual stop must be preserved")
        self.assertEqual(corr.destination_hub_arrival_time, 15.0,
                         "manual hub arrival must be preserved")

        # Suggested hub arrival recalculates (same-day return: outbound
        # drive twice + a dwell at every stop and the hub).
        corr.write({"same_day_return": True,
                    "hub_arrival_time_source": "suggested"})
        corr._apply_suggested_timing([stop_a], targets, legs)
        self.assertEqual(corr.destination_hub_arrival_time, 8.75)
        self.assertEqual(corr.hub_arrival_time_source, "suggested")

        # Plain edit flips the stop back to Manual Override.
        stop_a.write({"planned_arrival_time": 5.5})
        self.assertEqual(stop_a.timing_source, "manual")

        # The UI indicator selection carries both source values.
        values = [v for _, v in
                  corr._fields["hub_arrival_time_source"].selection]
        self.assertIn("Suggested", values)
        self.assertIn("Manual Override", values)

    # ── D: P6 Recommended Operational Start ─────────────────────────

    def test_d_recommended_operational_departure(self):
        """Backward calculation from the earliest hard constraint: window
        start − travel − buffer; start_position shifts the origin; no
        constraints → the corridor/anchor default stands."""
        planner = ItineraryPlanner(self.env)
        anchor = datetime(2026, 9, 7, 9, 0, tzinfo=utc_tz)  # 05:00 local
        hours = {str(d): [8.0, 17.0] for d in range(7)}
        stops = [
            {"stop_key": "s1", "stop_type": "pickup", "city": "A",
             "latitude": 43.6532, "longitude": -79.3832,
             "timezone": "America/Toronto",
             "operating_hours_snapshot": hours, "timing_type": "flexible",
             "location_name": "Pickup A"},
            {"stop_key": "s2", "stop_type": "delivery", "city": "B",
             "latitude": 43.85, "longitude": -79.5,
             "timezone": "America/Toronto",
             "operating_hours_snapshot": hours, "timing_type": "time_window",
             "window_start": 8.0, "window_end": 17.0,
             "location_name": "Delivery B"},
        ]
        movements = [{"key": "m1", "pickup_stop_key": "s1",
                      "delivery_stop_keys": ["s2"], "shared": False,
                      "weight_lbs": 500}]
        travel = lambda a, b: 195.0  # deterministic s1→s2 = 195 min

        # 08:00 local − 195 min − 15 min buffer = 04:30 local = 08:30 UTC.
        result = planner.recommended_departure(
            stops, movements, anchor, travel_fn=travel)
        self.assertTrue(result["feasible"], result.get("reason"))
        self.assertEqual(result["reason"], "hard_constraint")
        self.assertEqual(result["recommended_start"],
                         "2026-09-07T08:30:00+00:00")
        self.assertEqual(result["binding_stop"], "Delivery B")

        # Buffer 0 → 04:45 local = 08:45 UTC.
        result0 = planner.recommended_departure(
            stops, movements, anchor, travel_fn=travel, buffer_minutes=0)
        self.assertEqual(result0["recommended_start"],
                         "2026-09-07T08:45:00+00:00")

        # Start position: +195 min before the first stop → 195 min earlier.
        result_pos = planner.recommended_departure(
            stops, movements, anchor, start_position=(43.4, -79.2),
            travel_fn=travel)
        self.assertTrue(result_pos["start_position_used"])
        base_dt = datetime.fromisoformat(result["recommended_start"])
        pos_dt = datetime.fromisoformat(result_pos["recommended_start"])
        self.assertEqual(base_dt - pos_dt, timedelta(minutes=195))

        # Facility-hours bound: no hard windows — the start backs off to
        # the first stop's opening minus travel minus the buffer (11:45
        # UTC for a 14:00 UTC anchor).
        flexible = [dict(s, timing_type="flexible", window_start=None,
                         window_end=None) for s in stops]
        late_anchor = datetime(2026, 9, 7, 14, 0, tzinfo=utc_tz)
        result_flex = planner.recommended_departure(
            flexible, movements, late_anchor, travel_fn=travel)
        self.assertEqual(result_flex["reason"], "facility_hours")
        self.assertEqual(result_flex["recommended_start"],
                         "2026-09-07T11:45:00+00:00")
        self.assertEqual(result_flex["binding_stop"], "Pickup A")

        # A stop with NO operating hours is closed — the route is
        # infeasible and the caller keeps the corridor default (the
        # adviser never invents hours).
        bare = [dict(s, operating_hours_snapshot={}, timing_type="flexible",
                     window_start=None, window_end=None) for s in stops]
        result_bare = planner.recommended_departure(
            bare, movements, anchor, travel_fn=travel)
        self.assertFalse(result_bare["feasible"])
        self.assertEqual(result_bare["reason"], "facility_operating_hours")
        self.assertEqual(result_bare["recommended_start"],
                         anchor.isoformat())

        # Naive UTC input (an Odoo Datetime) is normalized, never crashes.
        naive = datetime(2026, 9, 7, 9, 0)
        result_naive = planner.recommended_departure(
            stops, movements, naive, travel_fn=travel)
        self.assertTrue(result_naive["feasible"])
        self.assertEqual(result_naive["recommended_start"],
                         "2026-09-07T08:30:00+00:00")

    # ── E: P7 facility operating hours reuse ────────────────────────

    def test_e_facility_hours_scope_chain(self):
        """Pickup stops consult pickup→shipping→general; delivery stops
        consult receiving→general — ONE snapshot authority."""
        fac = self._location("TEST Hours Facility")
        self._hours_all_days(fac, "general", 8.0, 17.0)
        self._hours_all_days(fac, "pickup", 6.0, 18.0)
        self._hours_all_days(fac, "shipping", 7.0, 19.0)
        self._hours_all_days(fac, "receiving", 9.0, 16.0)

        snap_pickup = snapshot_facility_hours(self.env, fac, "pickup")
        self.assertEqual(snap_pickup["0"], [6.0, 18.0])
        snap_delivery = snapshot_facility_hours(self.env, fac, "delivery")
        self.assertEqual(snap_delivery["0"], [9.0, 16.0])

        # Shipping-only facility: pickup falls back pickup→shipping.
        fac2 = self._location("TEST Hours Facility 2", 43.61, -79.41)
        self._hours_all_days(fac2, "shipping", 7.0, 19.0)
        snap2 = snapshot_facility_hours(self.env, fac2, "pickup")
        self.assertEqual(snap2["0"], [7.0, 19.0])

        # General-only facility: both fall back to general.
        fac3 = self._location("TEST Hours Facility 3", 43.62, -79.42)
        self._hours_all_days(fac3, "general", 8.0, 17.0)
        self.assertEqual(
            snapshot_facility_hours(self.env, fac3, "pickup")["0"],
            [8.0, 17.0])
        self.assertEqual(
            snapshot_facility_hours(self.env, fac3, "delivery")["0"],
            [8.0, 17.0])

    # ── F: P8 operational classification ────────────────────────────

    def test_f_operational_classification_hierarchy(self):
        """manual > history (≥5 samples) > type default (ONE parameter
        authority) > 15-min baseline."""
        Loc = self.env["prema.dispatch.location"]
        base = {"location_type": "warehouse", "pin_lat": 43.6,
                "pin_lng": -79.4}
        self.assertEqual(
            self._location("TEST Retail", **base,
                           operational_classification="retail")
            .planning_service_time_minutes(), 15)
        self.assertEqual(
            self._location("TEST WH", **base,
                           operational_classification="warehouse")
            .planning_service_time_minutes(), 30)
        self.assertEqual(
            self._location("TEST DC", **base,
                           operational_classification="distribution_center")
            .planning_service_time_minutes(), 60)
        self.assertEqual(
            self._location("TEST GDC", **base,
                           operational_classification="grocery_dc")
            .planning_service_time_minutes(), 60)
        self.assertEqual(
            self._location("TEST Other", **base,
                           operational_classification="other")
            .planning_service_time_minutes(), 15)

        # History beats the type default only at ≥5 samples.
        hist = self._location("TEST Hist", **base,
                              operational_classification="retail")
        self.assertEqual(hist.planning_service_time_minutes(), 15)
        hist.write({"use_count": 4, "recommended_service_time_minutes": 40})
        self.assertEqual(hist.planning_service_time_minutes(), 15,
                         "fewer than 5 samples → type default")
        hist.write({"use_count": 6})
        self.assertEqual(hist.planning_service_time_minutes(), 40,
                         "history wins from 5 samples")
        # Manual override always wins; the effective computed field
        # reflects the same single hierarchy.
        hist.write({"manual_service_time_minutes": 55})
        self.assertEqual(hist.planning_service_time_minutes(), 55)
        self.assertEqual(hist.effective_service_time_minutes, 55)

    # ── G: P9 historical dwell reuse ────────────────────────────────

    def test_g_historical_dwell_reuse(self):
        """dwell = actual departure − actual arrival; the median survives
        outliers; the ONE recommendation hierarchy consumes the history."""
        job = self.env["prema.dispatch.job"].create(
            {"partner_id": self.partner.id})
        loc = self._location("TEST Dwell Facility",
                             operational_classification="retail")

        def _stop(arrival, departure, service=45):
            return self.env["prema.dispatch.stop"].create({
                "job_id": job.id, "stop_type": "dropoff",
                "saved_location_id": loc.id,
                "service_time_minutes": service,
                "actual_arrival_time": arrival,
                "actual_departure_time": departure,
            })

        stop1 = _stop("2026-09-07 09:00:00", "2026-09-07 09:45:00")
        loc.record_visit_stats(stop1)
        self.assertEqual(loc.median_dwell_minutes, 45.0)
        self.assertEqual(loc.recommended_service_time_minutes, 45)
        self.assertEqual(loc.use_count, 0,
                         "record_visit_stats does not bump use_count")
        self.assertEqual(loc.planning_service_time_minutes(), 15,
                         "fewer than 5 samples → retail type default")

        # A 300-min outlier and a 60-min sample: median survives.
        stop2 = _stop("2026-09-08 09:00:00", "2026-09-08 14:00:00")
        loc.record_visit_stats(stop2)
        stop3 = _stop("2026-09-09 09:00:00", "2026-09-09 10:00:00")
        loc.record_visit_stats(stop3)
        self.assertEqual(loc.median_dwell_minutes, 60.0,
                         "median must survive the 300-min outlier")
        self.assertEqual(loc.avg_last10_dwell_minutes,
                         (45.0 + 300.0 + 60.0) / 3.0)

        # With enough samples the history drives the recommendation.
        loc.write({"use_count": 6})
        self.assertEqual(loc.planning_service_time_minutes(), 45)
        # The recommended figure is staff-only visibility — the customer
        # ETA path consumes the same method (P9/P10 support, TEST J3).

    # ── H: P10 detention invoice lifecycle ──────────────────────────

    def test_h_detention_invoice_lifecycle(self):
        """An approved detention charge appends to the booking's existing
        DRAFT invoice — never a second invoice."""
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "pallets": 1,
            "weight_lbs": 500,
            "calculated_price": 250.0,
        })
        job = self.env["prema.dispatch.job"].create({
            "partner_id": self.partner.id,
            "logistics_booking_id": booking.id,
        })
        loc = self._location("TEST Detention Facility")
        stop = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "stop_type": "dropoff",
            "saved_location_id": loc.id,
            "actual_arrival_time": "2026-09-07 09:00:00",
            "actual_departure_time": "2026-09-07 10:30:00",  # 90 min dwell
        })
        # (customer, facility) rule: 30 free / 30 increment / $25.
        self.env["prema.dispatch.detention.rule"].create({
            "partner_id": self.partner.id, "facility_id": loc.id,
            "free_minutes": 30, "increment_minutes": 30,
            "rate_per_increment": 25.0,
        })

        Item = self.env["prema.dispatch.detention.item"]
        item = Item._suggest_for_stop(stop)
        self.assertTrue(item)
        self.assertEqual(item.actual_dwell_minutes, 90)
        self.assertEqual(item.billable_minutes, 60)
        self.assertEqual(item.units, 2)
        self.assertEqual(item.suggested_amount, 50.0)
        self.assertEqual(item.state, "draft")

        # Idempotent: re-suggest refreshes, never duplicates.
        again = Item._suggest_for_stop(stop)
        self.assertEqual(again.id, item.id)
        self.assertEqual(Item.search_count([("stop_id", "=", stop.id)]), 1)

        item.action_approve()
        self.assertEqual(item.state, "approved")
        self.assertEqual(item.approved_amount, 50.0)
        self.assertTrue(item.review_user_id)

        invoice = booking._create_draft_invoice()
        self.assertTrue(invoice, "freight product mapping must exist")
        before = self.env["account.move"].search_count([
            ("logistics_booking_id", "=", booking.id),
            ("move_type", "=", "out_invoice")])
        item.action_add_to_invoice()
        after = self.env["account.move"].search_count([
            ("logistics_booking_id", "=", booking.id),
            ("move_type", "=", "out_invoice")])
        self.assertEqual(after, before, "never a second invoice")
        self.assertEqual(len(invoice.invoice_line_ids), 2)
        det_line = invoice.invoice_line_ids.filtered(
            lambda l: "Detention" in (l.name or ""))
        self.assertEqual(len(det_line), 1)
        self.assertEqual(det_line.price_unit, 50.0)
        self.assertTrue(item.invoiced)
        self.assertEqual(item.invoice_line_id.id, det_line.id)

    # ── J: actual-distance pricing + facility ETA ───────────────────

    def test_j_actual_distance_and_facility_eta(self):
        """Single-leg LTL bills the actual point-to-point road distance
        (Google-first, snapshot-audited), falls back to the segment
        distance unmocked; the facility's service duration feeds the ETA
        and never antes before opening."""
        # J1: mocked Google distance drives the leg + the snapshot.
        c15 = self.env["logistics.corridor"].search([
            ("name", "ilike", "%SW Ontario / Windsor%"),
            ("operate_monday", "=", True),
        ], limit=1)
        self.assertTrue(c15)
        with patch(
            "odoo.addons.prema_dispatch.services.route_service."
            "DispatchRouteService.get_sequential_travel",
            return_value=[{"distance_km": 190.0, "drive_minutes": 150}],
        ):
            route = self.svc.plan_route(
                pickup_lat=43.6532, pickup_lng=-79.3832,
                delivery_lat=42.3149, delivery_lng=-83.0364,
                pallets=1, weight_lbs=500,
                requested_pickup_date="2026-09-07",
                equipment="dry", shipment_type="ltl",
            )
        self.assertTrue(route.available, route.reason)
        self.assertEqual(route.legs[0].estimated_distance_km, 190.0)
        self.assertEqual(route.routing_snapshot.get("actual_distance_km"), 190.0)
        self.assertGreater(route.legs[0].leg_price, 0.0)

        # J2: no Google result → the corridor segment distance stands.
        with patch(
            "odoo.addons.prema_dispatch.services.route_service."
            "DispatchRouteService.get_sequential_travel",
            return_value=[],
        ):
            route2 = self.svc.plan_route(
                pickup_lat=43.6532, pickup_lng=-79.3832,
                delivery_lat=42.3149, delivery_lng=-83.0364,
                pallets=1, weight_lbs=500,
                requested_pickup_date="2026-09-07",
                equipment="dry", shipment_type="ltl",
            )
        self.assertTrue(route2.available, route2.reason)
        self.assertGreater(route2.legs[0].estimated_distance_km, 0.0)

        # J3: facility service duration (manual 40) shapes the ETA; an
        # early arrival waits at the door, never before opening.
        fac = self._location("TEST ETA Facility",
                             operational_classification="retail")
        self._hours_all_days(fac, "general", 8.0, 17.0)
        fac.write({"manual_service_time_minutes": 40})
        arrival = datetime(2026, 9, 7, 8, 0, tzinfo=utc_tz)  # 04:00 local
        eta, source, rolled = self.svc._facility_eta(
            {"saved_location_id": fac.id, "latitude": 43.6,
             "longitude": -79.4, "timezone": "America/Toronto"},
            arrival, "pickup")
        self.assertEqual(source, "facility_hours")
        # Arrival 08:00 UTC = 04:00 local (EDT); opening 08:00 local =
        # 12:00 UTC. The estimate must wait at the door until opening.
        self.assertEqual(eta, datetime(2026, 9, 7, 12, 0, tzinfo=utc_tz),
                         "service starts at opening, never before")
        plan = ItineraryPlanner(self.env).arrival_plan({
            "latitude": 43.6, "longitude": -79.4,
            "timezone": "America/Toronto",
            "operating_hours_snapshot":
                snapshot_facility_hours(self.env, fac, "pickup"),
            "timing_type": "flexible",
            "service_time_minutes": fac.planning_service_time_minutes(),
        }, eta)
        self.assertTrue(plan[0])
        self.assertEqual(plan[3] - eta, timedelta(minutes=40),
                         "manual service time 40 must drive the plan")

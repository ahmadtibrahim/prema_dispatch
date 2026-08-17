"""Milk-run portal flow — generalized route builder payload end to end:

quote (prepare_quote) → session stops with stable stop keys + operating
hours snapshot → movement_v1 confirm → booking stops with frozen hours →
canonical pallet rows → dispatch route bridge.

Plus the hours-snapshot immutability guarantee: editing the master saved
location AFTER confirmation never changes the historical booking's
planning.
"""
import datetime
import json

from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)
from odoo.addons.prema_logistics_booking.services.itinerary_planner import (
    snapshot_saved_location_hours,
)


class TestMilkRunPortal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        Region = cls.env["logistics.region"]

        cls.country_ca = cls.env.ref("base.ca")
        cls.country_ca.logistics_network_enabled = True
        cls.state_on = cls.env["res.country.state"].search(
            [("country_id", "=", cls.country_ca.id), ("code", "=", "ON")], limit=1,
        )
        cls.state_on.logistics_network_enabled = True

        def _square(lng, lat, dx=0.1, dy=0.05):
            return json.dumps({"type": "Polygon", "coordinates": [[
                [lng - dx, lat - dy], [lng + dx, lat - dy],
                [lng + dx, lat + dy], [lng - dx, lat + dy],
                [lng - dx, lat - dy],
            ]]})

        cls.gta = Region.create({
            "code": "MRP-GTA", "name": "GTA", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-87.5, 49.0),
        })
        cls.ott = Region.create({
            "code": "MRP-OTT", "name": "Ottawa", "is_official_ltl_region": True,
            "boundary_status": "approved", "country_id": cls.country_ca.id,
            "state_id": cls.state_on.id, "polygon_geojson": _square(-86.5, 49.8),
        })
        cls.env["logistics.hub"].create({
            "name": "MRP Hub", "public_name": "MRP Hub", "code": "MRP-HUB",
            "canonical_region_id": cls.gta.id, "is_default": True,
            "latitude": 49.0, "longitude": -87.5,
        })
        cls.corridor = cls.env["logistics.corridor"].create({
            "name": "MRP-Eastbound",
            "direction": "eastbound",
            "rate_per_km": 3.5,
            "planned_pallets": 8,
            "included_weight_per_pallet": 500.0,
            "minimum_booking_charge": 150.0,
            "operate_wednesday": True,
            "enable_volume_discounts": True,
            "ltl_additional_stop_charge": 75.0,
            "ltl_additional_pickup_charge": 25.0,
        })
        cls.env["logistics.corridor.stop"].create([
            {"corridor_id": cls.corridor.id, "sequence": 10, "region_id": cls.gta.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 0.0},
            {"corridor_id": cls.corridor.id, "sequence": 20, "region_id": cls.ott.id,
             "pickup_allowed": True, "delivery_allowed": True, "distance_from_origin_km": 507.6},
        ])
        cls.env["logistics.pallet.volume.tier"].create([
            {"corridor_id": cls.corridor.id, "min_pallets": 7, "max_pallets": 9,
             "discount_pct": 15.0, "pricing_type": "ltl"},
        ])

        cls.partner = cls.env["res.partner"].create({"name": "MRP Customer"})
        # 4 saved locations (United Dairy + TerraFreska pickups,
        # Belleville + Ottawa deliveries) with per-scope operating hours.
        SavedLocation = cls.env["logistics.saved.location"]
        Hours = cls.env["logistics.saved.location.hours"]
        cls.loc_ud = SavedLocation.create({
            "name": "United Dairy", "business_name": "United Dairy",
            "commercial_partner_id": cls.partner.id, "location_type": "pickup",
            "city": "Kingston", "postal_code": "X0A",
            "latitude": 49.0, "longitude": -87.5, "timezone": "America/Toronto",
        })
        cls.loc_tf = SavedLocation.create({
            "name": "TerraFreska", "business_name": "TerraFreska",
            "commercial_partner_id": cls.partner.id, "location_type": "pickup",
            "city": "Brampton", "postal_code": "X0B",
            "latitude": 49.03, "longitude": -87.45, "timezone": "America/Toronto",
        })
        cls.loc_blv = SavedLocation.create({
            "name": "Belleville Depot", "business_name": "Belleville Depot",
            "commercial_partner_id": cls.partner.id, "location_type": "delivery",
            "city": "Belleville", "postal_code": "X0C",
            "latitude": 49.78, "longitude": -86.5, "timezone": "America/Toronto",
        })
        cls.loc_ott = SavedLocation.create({
            "name": "Ottawa DC", "business_name": "Ottawa DC",
            "commercial_partner_id": cls.partner.id, "location_type": "delivery",
            "city": "Ottawa", "postal_code": "X0D",
            "latitude": 49.8, "longitude": -86.45, "timezone": "America/Toronto",
        })
        # UD: pickup hours Mon-Fri 06:00-16:00, Sat 07:00-12:00, Sun closed.
        for day in ("0", "1", "2", "3", "4"):
            Hours.create({
                "saved_location_id": cls.loc_ud.id, "day_of_week": day,
                "service_scope": "pickup", "status": "custom",
                "open_time": 6.0, "close_time": 16.0,
            })
        Hours.create({
            "saved_location_id": cls.loc_ud.id, "day_of_week": "5",
            "service_scope": "pickup", "status": "custom",
            "open_time": 7.0, "close_time": 12.0,
        })
        Hours.create({
            "saved_location_id": cls.loc_ud.id, "day_of_week": "6",
            "service_scope": "pickup", "status": "closed",
        })
        # TF: open 24h every day.
        for day in range(7):
            Hours.create({
                "saved_location_id": cls.loc_tf.id, "day_of_week": str(day),
                "service_scope": "pickup", "status": "open_24h",
            })
        # Deliveries: general receiving hours.
        for loc in (cls.loc_blv, cls.loc_ott):
            for day in range(7):
                Hours.create({
                    "saved_location_id": loc.id, "day_of_week": str(day),
                    "service_scope": "delivery", "status": "custom",
                    "open_time": 8.0, "close_time": 17.0,
                })

        # CRITICAL: logistics.booking.stop.saved_location_id points to
        # prema.dispatch.location (the master facility), NOT to
        # logistics.saved.location. Link each customer location to its
        # master dispatch facility like the real portal confirm does.
        DispatchLocation = cls.env["prema.dispatch.location"]
        for loc in (cls.loc_ud, cls.loc_tf, cls.loc_blv, cls.loc_ott):
            master = DispatchLocation.create({
                "name": "Facility — %s" % loc.name,
                "address": "%s, %s" % (loc.city, loc.postal_code),
                "pin_lat": loc.latitude,
                "pin_lng": loc.longitude,
            })
            loc.dispatch_location_id = master.id

        cls.wednesday = cls._next("wednesday")

    @classmethod
    def _next(cls, weekday):
        day = datetime.date.today()
        while day.strftime("%A").lower() != weekday:
            day += datetime.timedelta(days=1)
        return day

    def _route_stops(self):
        return [
            {"stop_key": "stp-ud", "stop_type": "pickup",
             "saved_location_id": self.loc_ud.id,
             "liftgate_required": True, "appointment_required": False,
             "timing_type": "flexible", "service_time_minutes": 20,
             "instructions": "Dock 3"},
            {"stop_key": "stp-tf", "stop_type": "pickup",
             "saved_location_id": self.loc_tf.id,
             "liftgate_required": False, "appointment_required": True,
             "timing_type": "time_window", "window_start": 9.0,
             "window_end": 14.0, "service_time_minutes": 30,
             "instructions": ""},
            {"stop_key": "stp-blv", "stop_type": "delivery",
             "saved_location_id": self.loc_blv.id,
             "liftgate_required": False, "appointment_required": False,
             "timing_type": "flexible", "service_time_minutes": 15,
             "instructions": "Receiving door B"},
            {"stop_key": "stp-ott", "stop_type": "delivery",
             "saved_location_id": self.loc_ott.id,
             "liftgate_required": True, "appointment_required": False,
             "timing_type": "flexible", "service_time_minutes": 15,
             "instructions": ""},
        ]

    def _movements(self):
        movements = []
        for i in range(4):
            movements.append({
                "key": "u%d" % (i + 1), "label": "U-%02d" % (i + 1),
                "weight_lbs": 500.0, "shared": False,
                "pickup_stop_key": "stp-ud", "delivery_stop_keys": ["stp-ott"],
            })
        for i in range(3):
            movements.append({
                "key": "t%d" % (i + 1), "label": "TF-%02d" % (i + 1),
                "weight_lbs": 400.0, "shared": False,
                "pickup_stop_key": "stp-tf", "delivery_stop_keys": ["stp-blv"],
            })
        return movements

    # ── Hours snapshot helper ─────────────────────────────────────────

    def test_01_hours_snapshot_scopes_and_closed_days(self):
        snapshot = snapshot_saved_location_hours(self.env, self.loc_ud, "pickup")
        self.assertEqual(snapshot["0"], [6.0, 16.0])
        self.assertEqual(snapshot["5"], [7.0, 12.0])
        self.assertIsNone(snapshot["6"])  # closed Sunday
        tf_snapshot = snapshot_saved_location_hours(self.env, self.loc_tf, "pickup")
        self.assertEqual(tf_snapshot["0"], [0.0, 24.0])
        blv_snapshot = snapshot_saved_location_hours(self.env, self.loc_blv, "delivery")
        self.assertEqual(blv_snapshot["0"], [8.0, 17.0])

    # ── Generalized quote → session ───────────────────────────────────

    def _generalized_quote(self):
        svc = BookingOrchestrationService(self.env)
        route_stops = self._route_stops()
        pickups = [s for s in route_stops if s["stop_type"] == "pickup"]
        deliveries = [s for s in route_stops if s["stop_type"] == "delivery"]
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [dict(p, **{
                "latitude": self.env["logistics.saved.location"].browse(
                    p["saved_location_id"]).latitude,
                "longitude": self.env["logistics.saved.location"].browse(
                    p["saved_location_id"]).longitude,
                "postal_code": self.env["logistics.saved.location"].browse(
                    p["saved_location_id"]).postal_code,
            }) for p in pickups],
            "delivery_stops": [dict(d, **{
                "latitude": self.env["logistics.saved.location"].browse(
                    d["saved_location_id"]).latitude,
                "longitude": self.env["logistics.saved.location"].browse(
                    d["saved_location_id"]).longitude,
                "postal_code": self.env["logistics.saved.location"].browse(
                    d["saved_location_id"]).postal_code,
            }) for d in deliveries],
            "route_stops": route_stops,
            "route_model_version": "movement_v1",
            "pallet_movements": self._movements(),
            "pallets": 7, "physical_pallets": 7,
            "weight_lbs": 3200.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": self.wednesday,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1)
        return result, session

    def test_02_generalized_quote_session_stops_and_movements(self):
        result, session = self._generalized_quote()
        self.assertTrue(session)
        # Ordered session stops with stable stop keys — pickups AND
        # deliveries, in route order.
        stops = session.stop_ids.sorted("sequence")
        self.assertEqual(
            [s.stop_key for s in stops],
            ["stp-ud", "stp-tf", "stp-blv", "stp-ott"],
        )
        self.assertEqual([s.stop_type for s in stops],
                         ["pickup", "pickup", "delivery", "delivery"])
        # Hours snapshotted per stop; closed Sunday is None.
        ud = stops[0]
        self.assertEqual(ud.operating_hours_snapshot["6"], None)
        self.assertEqual(ud.operating_hours_snapshot["0"], [6.0, 16.0])
        # Stop-level requirements carried on the stop, not the booking.
        self.assertTrue(ud.liftgate_required)
        self.assertFalse(ud.appointment_required)
        tf = stops[1]
        self.assertTrue(tf.appointment_required)
        self.assertEqual(tf.window_start, 9.0)
        self.assertEqual(tf.window_end, 14.0)
        # Canonical movements stored in the pricing snapshot.
        movements = self.env["logistics.booking"]._extract_pallet_movements_from_snapshot(
            session.price_snapshot)
        self.assertEqual(len(movements), 7)
        # Additional pickup charge: 1 extra pickup × $25.
        pricing = (session.route_snapshot or {}).get("pricing") or {}
        self.assertEqual(pricing["additional_pickup_count"], 1)
        self.assertEqual(pricing["additional_pickup_total"], 25.0)
        self.assertAlmostEqual(session.calculated_price,
                               pricing["final_transportation"], places=2)
        # No additional STOP charge — the two delivery cities differ.
        self.assertEqual(pricing.get("additional_stop_total", 0.0), 0.0)

    def test_03_legacy_quote_has_no_movements(self):
        """A quote without the generalized payload never carries movements
        and never flips the discriminator."""
        svc = BookingOrchestrationService(self.env)
        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [{"latitude": 49.0, "longitude": -87.5, "postal_code": "X0A"}],
            "delivery_stops": [{"latitude": 49.8, "longitude": -86.5, "postal_code": "X0C"}],
            "pallets": 2, "physical_pallets": 2,
            "weight_lbs": 1000.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "requested_pickup_date": self.wednesday,
        }, source_channel="portal")
        result = svc.prepare_quote(norm)
        session = self.env["logistics.pricing.session"].search(
            [("token", "=", result["quote_token"])], limit=1)
        movements = self.env["logistics.booking"]._extract_pallet_movements_from_snapshot(
            session.price_snapshot)
        self.assertEqual(movements, [])

    # ── Generalized confirm → movement_v1 booking → dispatch bridge ───

    def test_04_confirm_generalized_freezes_hours_and_bridges_dispatch(self):
        _, session = self._generalized_quote()
        route_stops = self._route_stops()
        pickups = [s for s in route_stops if s["stop_type"] == "pickup"]
        deliveries = [s for s in route_stops if s["stop_type"] == "delivery"]
        svc = BookingOrchestrationService(self.env)

        def _stop_dict(s):
            loc = self.env["logistics.saved.location"].browse(s["saved_location_id"])
            return dict(s, **{
                "company_name": loc.business_name or loc.name,
                "street": loc.street or "",
                "city": loc.city,
                "postal_code": loc.postal_code,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                # booking.stop.saved_location_id = master dispatch facility
                "saved_location_id": loc.dispatch_location_id.id
                if loc.dispatch_location_id else False,
                "logistics_saved_location_id": loc.id,
                "operating_hours_snapshot": snapshot_saved_location_hours(
                    self.env, loc, s["stop_type"]),
            })

        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [_stop_dict(s) for s in pickups],
            "delivery_stops": [_stop_dict(s) for s in deliveries],
            "route_model_version": "movement_v1",
            "pallet_movements": self._movements(),
            "pallets": 7, "physical_pallets": 7,
            "weight_lbs": 3200.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "pricing_method": "manual",
            "agreed_rate": 500.0,
            "idempotency_key": "test:milkrun:portal:confirm",
        }, source_channel="portal")
        booking = svc.confirm_from_internal(norm, skip_invoice=True)
        self.assertEqual(booking.route_model_version, "movement_v1")
        stops = booking.stop_ids.sorted("sequence")
        self.assertEqual([s.stop_key for s in stops],
                         ["stp-ud", "stp-tf", "stp-blv", "stp-ott"])
        # Frozen hours + stop-level requirements on the persistent stops.
        ud = stops[0]
        self.assertEqual(ud.operating_hours_snapshot["0"], [6.0, 16.0])
        self.assertIsNone(ud.operating_hours_snapshot["6"])
        self.assertTrue(ud.liftgate_required)
        tf = stops[1]
        self.assertTrue(tf.appointment_required)
        self.assertEqual(tf.window_start, 9.0)
        self.assertEqual(tf.service_time_minutes, 30)
        self.assertEqual(ud.service_time_minutes, 20)
        # 7 canonical pallets.
        self.assertEqual(len(booking.pallet_ids), 7)
        # Dispatch: ONE milk-run route job, 4 ordered stops, 7 items.
        jobs = booking.dispatch_job_ids
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        dispatch_stops = job.stop_ids.sorted("sequence")
        self.assertEqual([s.pallets_in for s in dispatch_stops], [4, 3, 0, 0])
        self.assertEqual([s.pallets_out for s in dispatch_stops], [0, 0, 3, 4])
        self.assertEqual(len(job.item_ids), 7)

    # ── Hours immutability after confirmation ─────────────────────────

    def test_05_master_hours_edits_do_not_change_confirmed_booking(self):
        _, session = self._generalized_quote()
        route_stops = self._route_stops()
        pickups = [s for s in route_stops if s["stop_type"] == "pickup"]
        deliveries = [s for s in route_stops if s["stop_type"] == "delivery"]
        svc = BookingOrchestrationService(self.env)

        def _stop_dict(s):
            loc = self.env["logistics.saved.location"].browse(s["saved_location_id"])
            return dict(s, **{
                "company_name": loc.business_name or loc.name,
                "city": loc.city,
                "postal_code": loc.postal_code,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "saved_location_id": loc.dispatch_location_id.id
                if loc.dispatch_location_id else False,
                "logistics_saved_location_id": loc.id,
                "operating_hours_snapshot": snapshot_saved_location_hours(
                    self.env, loc, s["stop_type"]),
            })

        norm = svc.normalize_request({
            "partner_id": self.partner.id,
            "pickup_stops": [_stop_dict(s) for s in pickups],
            "delivery_stops": [_stop_dict(s) for s in deliveries],
            "route_model_version": "movement_v1",
            "pallet_movements": self._movements(),
            "pallets": 7, "physical_pallets": 7,
            "weight_lbs": 3200.0,
            "load_type": "ltl",
            "equipment_type": "dry",
            "pricing_method": "manual",
            "agreed_rate": 500.0,
            "idempotency_key": "test:milkrun:portal:immutable",
        }, source_channel="portal")
        booking = svc.confirm_from_internal(norm, skip_invoice=True)
        ud_stop = booking.stop_ids.filtered(lambda s: s.stop_key == "stp-ud")
        self.assertEqual(ud_stop.operating_hours_snapshot["0"], [6.0, 16.0])
        # Master location hours change AFTER confirmation...
        ud_hours = self.env["logistics.saved.location.hours"].search([
            ("saved_location_id", "=", self.loc_ud.id),
            ("day_of_week", "=", "0"),
        ])
        ud_hours.write({"open_time": 11.0, "close_time": 13.0})
        # ...but the confirmed booking keeps its frozen snapshot.
        self.assertEqual(ud_stop.operating_hours_snapshot["0"], [6.0, 16.0])
        self.assertEqual(
            snapshot_saved_location_hours(self.env, self.loc_ud, "pickup")["0"],
            [11.0, 13.0],
        )

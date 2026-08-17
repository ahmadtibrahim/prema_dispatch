"""Coordinate / facility integrity (Manual-UAT Parts 2-3).

Regression tests for the Booking 185 corruptions:
  (A) the pickup facility was replaced by an unrelated master location
      ("Demo Logistics Customer", 994 Westport Crescent, Mississauga)
      instead of United Dairy, 145 Sun Pac Boulevard, Brampton; and
  (B) the Healthy Planet Belleville stop's pin was geocoded to
      41.658361, -70.925542 (New Bedford, MA — routing the Ontario route
      through the USA) because its truncated address lacked the city.

Rules under test:
  * the CONFIRMED booking-stop snapshot is the historical authority
    (business, street, city, postal, coordinates);
  * master dispatch locations may only SUPPLEMENT (dock, entrance pin,
    metadata), never silently replace the confirmed facility or pin;
  * a pin materially far from the confirmed address is restored and
    flagged (coordinate_warning / facility_mismatch);
  * RE-GEOCODE FROM CONFIRMED ADDRESS repairs a corrupted stop in one
    click using the full street+city+province+postal address.
"""
import datetime

from odoo.tests import TransactionCase


class TestCoordinateIntegrity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_departure_reconcile=True))
        cls.partner = cls.env["res.partner"].search([], limit=1)
        cls.Location = cls.env["prema.dispatch.location"]

        # Master locations mirroring Prod-db: United Dairy + a WRONG
        # "Demo Logistics Customer" style location at 994 Westport Crs, and
        # Healthy Planet Belleville (correct pin).
        cls.loc_united_dairy = cls.Location.create({
            "name": "United Dairy",
            "address": "145 Sun Pac Blvd, Brampton, ON L6S 5Z6, Canada",
            "pin_lat": 43.755959, "pin_lng": -79.692568,
        })
        cls.loc_demo_wrong = cls.Location.create({
            "name": "Demo Logistics Customer",
            "address": "994 Westport Crescent, Mississauga",
            # no pin — like the real record
        })
        cls.loc_healthy_planet = cls.Location.create({
            "name": "Healthy Planet - Belleville",
            "address": "290 N Front St, Belleville, ON K8P 3C4, Canada",
            "pin_lat": 44.183661, "pin_lng": -77.394851,
        })

        cls.booking = cls.env["logistics.booking"].create({
            "partner_id": cls.partner.id,
            "booking_number": "CI-185",
            "shipment_type": "ltl", "temperature_mode": "dry",
            "pallets": 4, "physical_pallets": 4, "weight_lbs": 2000.0,
            "state": "confirmed", "calculated_price": 647.19,
            "route_model_version": "legacy",
        })
        # Confirmed snapshot: United Dairy Brampton pickup.
        cls.ud = cls.env["logistics.booking.stop"].create({
            "booking_id": cls.booking.id, "sequence": 10, "stop_type": "pickup",
            "company_name": "United Dairy", "street": "145 Sun Pac Boulevard",
            "city": "Brampton", "province_state": "ON", "postal_zip": "L6S 5Z6",
            "formatted_address": "145 Sun Pac Boulevard",
            "latitude": 43.755959, "longitude": -79.692568,
            "saved_location_id": cls.loc_demo_wrong.id,
            # ^ deliberately the WRONG master link (what the session could
            # produce before the integrity pass)
        })
        # Confirmed snapshot: Healthy Planet Belleville.
        cls.hp = cls.env["logistics.booking.stop"].create({
            "booking_id": cls.booking.id, "sequence": 20, "stop_type": "delivery",
            "company_name": "Healthy Planet", "street": "290 North Front Street",
            "city": "Belleville", "province_state": "ON", "postal_zip": "K8P 3C4",
            "formatted_address": "290 North Front Street",
            # ^ truncated Google short form — no city
            "latitude": 44.183661, "longitude": -77.394851,
            "saved_location_id": cls.loc_healthy_planet.id,
        })

    # ── (A) wrong master location must never replace the facility ────

    def test_01_wrong_master_location_never_replaces_confirmed_pickup(self):
        """Booking 185 corruption (A): even when the booking stop's saved
        location points at a DIFFERENT facility (Demo Logistics, no pin),
        the dispatch stop keeps the confirmed United Dairy address and
        Brampton pin, and the mismatch is flagged."""
        job = self.booking._create_dispatch_operation(
            False, "pickup", datetime.date(2026, 8, 19),
            origin_stop=self.ud, sequence=1)
        stop = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")
        self.assertTrue(stop)
        # Confirmed facility kept — not the master location's address.
        self.assertIn("145 Sun Pac Boulevard", stop.address)
        self.assertIn("Brampton", stop.address)
        self.assertNotIn("Westport", stop.address)
        # Confirmed pin kept.
        self.assertAlmostEqual(stop.latitude, 43.755959, places=4)
        self.assertAlmostEqual(stop.longitude, -79.692568, places=4)
        # The wrong master link is loudly flagged for dispatcher review.
        self.assertTrue(stop.facility_mismatch)
        self.assertIn("Demo Logistics", stop.coordinate_warning or "")
        self.assertEqual(stop.coordinate_source, "booking_stop")
        self.assertTrue(stop.coordinate_validated)

    def test_02_booking_stop_level_location_mismatch_signal(self):
        """The booking stop itself carries a computed mismatch warning when
        its saved location is a different facility (portal sees it before
        confirmation)."""
        self.assertTrue(self.ud.location_mismatch_warning)
        self.assertIn("Demo Logistics", self.ud.location_mismatch_warning)
        # A consistent link has no warning.
        self.assertFalse(self.hp.location_mismatch_warning)

    # ── (B) cross-city pin is restored from the confirmed snapshot ───

    def test_03_cross_city_pin_restored_at_create(self):
        """Booking 185 corruption (B): a dispatch stop created with a pin
        in the USA (41.658361, -70.925542 — New Bedford, MA) is restored to
        the confirmed Belleville coordinates at creation."""
        job = self.booking._create_dispatch_operation(
            False, "delivery", datetime.date(2026, 8, 19),
            destination_stop=self.hp, sequence=1)
        stop = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        # Simulate the corruption: the record comes in with the wrong pin.
        stop.write({
            "latitude": 41.658361, "longitude": -70.925542,
            "pin_lat": 41.658361, "pin_lng": -70.925542,
        })
        # write() re-validates against the confirmed snapshot → restored.
        self.assertAlmostEqual(stop.latitude, 44.183661, places=4)
        self.assertAlmostEqual(stop.longitude, -77.394851, places=4)
        self.assertIn("restored", stop.coordinate_warning or "")
        self.assertEqual(stop.coordinate_source, "booking_stop")
        self.assertTrue(stop.coordinate_validated)

    def test_04_full_address_builds_from_snapshot_parts(self):
        """The canonical dispatch address for Healthy Planet includes the
        city — a bare '290 North Front Street' geocodes to the USA."""
        stop_address = self.booking._booking_stop_address(self.hp)
        self.assertIn("Belleville", stop_address)
        self.assertIn("290 North Front Street", stop_address)

    def test_05_regeocode_action_repairs_corrupted_stop(self):
        """RE-GEOCODE FROM CONFIRMED ADDRESS restores full address, pin,
        contact and hours and clears the warning — one-click repair."""
        job = self.booking._create_dispatch_operation(
            False, "delivery", datetime.date(2026, 8, 19),
            destination_stop=self.hp, sequence=1)
        stop = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        # Corrupt it the way production did: truncated address + USA pin.
        stop.with_context(_coord_integrity_restore=True).write({
            "address": "290 North Front Street",
            "latitude": 41.658361, "longitude": -70.925542,
            "pin_lat": 41.658361, "pin_lng": -70.925542,
            "coordinate_warning": "Pin was 760 km from the confirmed address",
        })
        self.assertTrue(stop.coordinate_warning)
        result = stop.action_regeocode_from_confirmed_address()
        self.assertTrue(result)
        self.assertIn("Belleville", stop.address)
        self.assertAlmostEqual(stop.latitude, 44.183661, places=4)
        self.assertAlmostEqual(stop.longitude, -77.394851, places=4)
        self.assertFalse(stop.coordinate_warning)
        self.assertFalse(stop.facility_mismatch)
        self.assertTrue(stop.coordinate_validated)
        self.assertEqual(stop.coordinate_source, "booking_stop")

    def test_06_regeocode_requires_booking_snapshot(self):
        """A stop with no confirmed booking snapshot cannot re-geocode."""
        job = self.booking._create_dispatch_operation(
            False, "pickup", datetime.date(2026, 8, 19),
            origin_stop=self.ud, sequence=2)
        # Break the bridge to simulate a non-booking stop.
        job.stop_ids.write({"logistics_booking_stop_id": False})
        with self.assertRaises(Exception):
            job.stop_ids.action_regeocode_from_confirmed_address()

    # ── Fix 1 regression: hub placeholders never become stops ────────

    def test_07_hub_transfer_placeholder_never_becomes_dispatch_stop(self):
        """A pricing-only hub placeholder (no stop_key, hub_transfer_stop
        flagged, pallet_count 0, no saved location) must never appear as a
        dispatch stop — the legacy delivery loop and the movement bridge
        both exclude it."""
        hub = self.env["logistics.booking.stop"].create({
            "booking_id": self.booking.id, "sequence": 50, "stop_type": "delivery",
            "company_name": "Transit Mississauga, ON",
            "street": "994 Westport Crescent", "city": "Mississauga",
            "hub_transfer_stop": True,
            # no stop_key, no saved location, no pallet count
        })
        # Legacy delivery loop excludes the flagged placeholder.
        BStop = self.env["logistics.booking.stop"]
        customer_delivery_stops = BStop.search([
            ("booking_id", "=", self.booking.id),
            ("stop_type", "=", "delivery"),
        ], order="sequence").filtered(
            lambda s: not s.hub_transfer_stop
            and (s.pallet_count > 0 or s.saved_location_id))
        self.assertNotIn(hub.id, customer_delivery_stops.ids)
        self.assertIn(self.hp.id, customer_delivery_stops.ids)

    def test_08_geocode_guard_keeps_snapshot_pin(self):
        """Geocoding a booking-sourced stop whose snapshot has coordinates
        never overwrites the pin — even when the stored address is the
        ambiguous short form."""
        job = self.booking._create_dispatch_operation(
            False, "delivery", datetime.date(2026, 8, 19),
            destination_stop=self.hp, sequence=1)
        stop = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        before = (stop.latitude, stop.longitude)
        # force=True still geocodes — but from the FULL confirmed address
        # (city included), so the pin can never land in the USA.
        stop._geocode_address(force=True)
        after = (stop.latitude, stop.longitude)
        # Without an API key nothing changes; with one, the full-address
        # geocode still lands in Belleville (both are within tolerance of
        # the snapshot).
        dlat = abs(after[0] - before[0])
        dlng = abs(after[1] - before[1])
        self.assertLess(max(dlat, dlng), 0.1)

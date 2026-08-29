# -*- coding: utf-8 -*-
"""18-section work order §18: Booking detail physical-stop grouping.

Physical stops are grouped by canonical facility (prema.dispatch.location
link preferred; same-snapshot-address stops without a master link still
group together) with PICKUPS/DELIVERIES aggregation:

- the portal booking-detail route renders ONE card per building — two
  pickups at one warehouse, or pickup+delivery at one site, collapse
  into a single card instead of repeated identical addresses
- the backend booking form shows a per-facility summary line
  (facility_grouping_summary) on the Stops & Pallets page
- hub-transfer placeholders are excluded, groups appear in route order,
  and each stop keeps its date / time-window / pallet detail

Run: --test-tags /prema_logistics_booking/tests/test_phase18_stop_grouping
"""
import datetime

from odoo.tests import TransactionCase


class TestPhase18StopGrouping(TransactionCase):
    """§18 booking detail physical-stop grouping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "P18 Customer"})
        cls.loc_a = cls.env["prema.dispatch.location"].create({
            "name": "P18 Warehouse A",
            "address": "1101 Grouping Rd", "city": "Brampton",
            "postal_code": "L6T 0A1",
            "pin_lat": 43.70, "pin_lng": -79.75,
        })
        cls.loc_b = cls.env["prema.dispatch.location"].create({
            "name": "P18 Depot B",
            "address": "2202 Grouping Rd", "city": "Mississauga",
            "postal_code": "L5A 1A1",
            "pin_lat": 43.60, "pin_lng": -79.65,
        })

    def _booking(self, route_version="legacy"):
        booking = self.env["logistics.booking"].create({
            "partner_id": self.partner.id,
            "route_model_version": route_version,
            "shipment_type": "ltl", "service_mode": "dedicated",
            "load_type": "ltl", "temperature_mode": "dry",
            "equipment_requirement": "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 10),
            "estimated_delivery_date": datetime.date(2026, 9, 10),
            "price_snapshot": [{"line": "P18 test"}],
        })
        return booking

    def _stop(self, booking, seq, stop_type, loc=None, **kw):
        vals = {
            "booking_id": booking.id, "sequence": seq,
            "stop_type": stop_type,
            "city": loc.city if loc else "Snap City",
            "pallet_count": kw.pop("pallet_count", 1),
            "requested_time_from": kw.pop("time_from", None),
            "requested_time_to": kw.pop("time_to", None),
        }
        if loc:
            vals["saved_location_id"] = loc.id
        vals.update(kw)
        return self.env["logistics.booking.stop"].create(vals)

    def _groups(self, booking):
        return booking._stop_groupings()

    def test_a_two_pickups_same_facility_one_group(self):
        """Two pickups at ONE master facility collapse into a single
        group carrying both stop details."""
        booking = self._booking()
        self._stop(booking, 10, "pickup", self.loc_a)
        self._stop(booking, 20, "pickup", self.loc_a)
        groups = self._groups(booking)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["facility_name"], "P18 Warehouse A")
        self.assertEqual(len(groups[0]["pickups"]), 2)
        self.assertEqual(len(groups[0]["deliveries"]), 0)
        self.assertEqual(
            [s["sequence"] for s in groups[0]["pickups"]], [10, 20])

    def test_b_snapshot_address_groups_without_master_link(self):
        """Stops sharing a snapshot address but NO master link still
        group together (same company/street/city/postal)."""
        booking = self._booking()
        common = {
            "company_name": "Snap Co", "street": "99 Common Rd",
            "city": "Kitchener", "province_state": "ON",
            "postal_zip": "N2A 1A1",
        }
        self._stop(booking, 10, "pickup", **common)
        self._stop(booking, 20, "delivery", **common)
        groups = self._groups(booking)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["facility_name"], "Snap Co")
        self.assertEqual(len(groups[0]["pickups"]), 1)
        self.assertEqual(len(groups[0]["deliveries"]), 1)

    def test_c_pickup_and_delivery_same_facility(self):
        """Pickup + delivery at the same building → ONE facility card
        with both the Pickups and Deliveries sections populated."""
        booking = self._booking()
        self._stop(booking, 10, "pickup", self.loc_a)
        self._stop(booking, 20, "delivery", self.loc_a)
        groups = self._groups(booking)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["pickups"]), 1)
        self.assertEqual(len(groups[0]["deliveries"]), 1)

    def test_d_distinct_facilities_keep_route_order(self):
        """A→B→A route: two facility groups, first-seen route order
        preserved, and the revisited facility aggregates both visits."""
        booking = self._booking()
        self._stop(booking, 10, "pickup", self.loc_a)
        self._stop(booking, 20, "pickup", self.loc_b)
        self._stop(booking, 30, "pickup", self.loc_a)
        groups = self._groups(booking)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["facility_name"], "P18 Warehouse A")
        self.assertEqual(groups[1]["facility_name"], "P18 Depot B")
        self.assertEqual(len(groups[0]["pickups"]), 2,
                         "revisited facility aggregates both visits")
        self.assertEqual(
            [s["sequence"] for s in groups[0]["pickups"]], [10, 30])

    def test_e_hub_transfer_placeholders_excluded(self):
        """Hub-transfer placeholder stops never appear in the grouping —
        exactly like the portal timeline they replace."""
        booking = self._booking()
        self._stop(booking, 10, "pickup", self.loc_a)
        self._stop(booking, 15, "delivery", self.loc_b,
                   hub_transfer_stop=True)
        self._stop(booking, 20, "delivery", self.loc_b)
        groups = self._groups(booking)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[1]["deliveries"]), 1,
                         "the hub-transfer placeholder is excluded")

    def test_f_backend_summary_field(self):
        """facility_grouping_summary: one line per facility with
        PICKUPS/DELIVERIES aggregation, for movement_v1 and legacy;
        'No stops' when the booking has none."""
        booking = self._booking("movement_v1")
        self._stop(booking, 10, "pickup", self.loc_a,
                   pallet_count=2, time_from=8.5, time_to=12.0,
                   province_state="ON")
        self._stop(booking, 20, "pickup", self.loc_a, province_state="ON")
        self._stop(booking, 30, "delivery", self.loc_b, province_state="ON")
        summary = booking.facility_grouping_summary
        self.assertIn("P18 Warehouse A — 2 pickups · 0 deliveries", summary)
        self.assertIn("(Brampton, ON)", summary)
        self.assertIn("P18 Depot B — 0 pickups · 1 delivery", summary)
        # Legacy bookings get the same grouping summary.
        legacy = self._booking("legacy")
        self._stop(legacy, 10, "pickup", self.loc_a, province_state="ON")
        self.assertEqual(legacy.facility_grouping_summary,
                         "P18 Warehouse A — 1 pickup · 0 deliveries "
                         "(Brampton, ON)")
        # No stops → honest fallback.
        empty = self._booking("movement_v1")
        self.assertEqual(empty.facility_grouping_summary, "No stops")

    def test_g_stop_detail_formatting(self):
        """Per-stop detail formatting: float-hour window → "08:30–12:00",
        pallet count with pluralization, singular "1 pallet"."""
        booking = self._booking()
        self._stop(booking, 10, "pickup", self.loc_a,
                   pallet_count=2, time_from=8.5, time_to=12.0)
        self._stop(booking, 20, "delivery", self.loc_a,
                   pallet_count=1, time_from=14.0, time_to=15.5)
        groups = self._groups(booking)
        self.assertEqual(groups[0]["pickups"][0]["window_display"],
                         "08:30–12:00")
        self.assertEqual(groups[0]["pickups"][0]["pallet_display"],
                         "2 pallets")
        self.assertEqual(groups[0]["deliveries"][0]["window_display"],
                         "14:00–15:30")
        self.assertEqual(groups[0]["deliveries"][0]["pallet_display"],
                         "1 pallet")

    def test_h_portal_and_backend_views_rendered(self):
        """The portal booking-detail template renders the grouped cards
        (stop_groups t-set + facility badge aggregation) and the flat
        per-stop timeline comment is gone; the backend form shows the
        facility grouping field."""
        portal = self.env.ref(
            "prema_logistics_booking.portal_booking_detail")
        arch = portal.arch
        self.assertIn('t-set="stop_groups"', arch)
        self.assertIn("t-value=\"booking._stop_groupings()\"", arch)
        self.assertIn("group['facility_name']", arch)
        self.assertIn("'%d Pickup%s'", arch)
        self.assertIn("'%d Deliver%s'", arch)
        self.assertNotIn("Multi-stop route timeline", arch,
                         "the flat per-stop timeline is replaced")
        self.assertNotIn("stop.movement_weight_lbs", arch,
                         "no per-stop weight rows in the old flat layout")
        form = self.env.ref(
            "prema_logistics_booking.view_logistics_booking_form")
        self.assertIn("facility_grouping_summary", form.arch)
        self.assertIn("Facility Grouping (§18)", form.arch)

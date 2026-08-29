# -*- coding: utf-8 -*-
"""Phase 3 targeted tests — 18-section work order Section B defects:

- #29 (HIGH) Singleton pickup assumption in the pallet prefix: a milk-run
  item picked at the SECOND pickup stop must be named from THAT stop's
  facility, not the job-level partner / first-pickup location.
- #20 (HIGH) Step 2 unassigned-delivery data defect: an item whose job has
  NO load_plan_job link still gets renderable delivery_choices from its own
  job's dropoff stops; the legacy bridge links per-pallet items to their
  logistics.booking.pallet row.
- #21 (HIGH) POPP upload: per-pallet proof respects the 4-photo cap, the
  pallet-scope check, and writes the canonical evidence row with pallet_id.
- Warehouse variant strips customer detail (and the new delivery_choices
  customer names) from the load-plan payload.

Run: --test-tags /prema_logistics_booking/tests/test_phase3_step2_popp_refresh
"""
import base64
import json
from datetime import date

from odoo.tests import TransactionCase

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestPhase3Step2PoppRefresh(TransactionCase):
    """Defects #29 / #20 / #21 — Step 2 delivery choices, booking-pallet
    links, per-pickup pallet prefixes, and POPP upload rules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.partner = env["res.partner"].create({"name": "Phase 3 Test Customer"})
        brand = env["fleet.vehicle.model.brand"].create({
            "name": "P3 Test Brand",
        })
        vehicle_model = env["fleet.vehicle.model"].create({
            "name": "P3 Test Truck Model",
            "brand_id": brand.id,
        })
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "P3-TRUCK-01",
            "license_plate": "P3-0001",
            "odometer_unit": "kilometers",
            "power_unit": "power",
            "model_id": vehicle_model.id,
        })

    # ── fixture helpers ──────────────────────────────────────────────

    @classmethod
    def _location(cls, name, **extra):
        cls._loc_n = getattr(cls, "_loc_n", 0) + 1
        vals = {"name": name,
                "address": "456 P3 Test Ave #%d, Ontario" % cls._loc_n,
                "pin_lat": 43.62, "pin_lng": -79.45}
        vals.update(extra)
        return cls.env["prema.dispatch.location"].create(vals)

    @classmethod
    def _layout_template(cls, codes=("A1", "A2")):
        return cls.env["prema.dispatch.vehicle.layout.template"].create({
            "name": "P3 Layout",
            "layout_type": "straight",
            "is_verified": True,
            "position_ids": [
                (0, 0, {"position_code": code, "sequence": i * 10})
                for i, code in enumerate(codes, start=1)
            ],
        })

    @classmethod
    def _booking(cls, pallets=2, pickup_loc=None, delivery_loc=None,
                 snapshot_alloc=False, **extra):
        """Legacy-architecture booking with booking stops (and optional
        price_snapshot pallet allocations) — the exact shape the legacy
        bridge consumes."""
        env = cls.env
        pickup_loc = pickup_loc or cls._location("P3 Pickup Depot")
        delivery_loc = delivery_loc or cls._location("P3 Delivery Depot")
        vals = {
            "partner_id": cls.partner.id,
            "shipment_type": "ltl",
            "temperature_mode": "dry",
            "service_mode": "dedicated",
            "load_type": "ltl",
            "equipment_requirement": "dry",
            "pallets": pallets,
            "physical_pallets": pallets,
            "weight_lbs": 2400.0,
            "pickup_date": date(2026, 9, 1),
            "estimated_delivery_date": date(2026, 9, 1),
            "commodity": "Test freight",
        }
        if snapshot_alloc:
            vals["price_snapshot"] = [{
                "line": "P3 test",
                "_pallet_allocs": [
                    {"pallet": p, "stops": [1], "shared": False}
                    for p in range(1, pallets + 1)
                ],
            }]
        vals.update(extra)
        booking = env["logistics.booking"].create(vals)
        env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": pickup_loc.id, "city": "Pickup City",
             "pallet_count": pallets},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": delivery_loc.id, "city": "Delivery City",
             "pallet_count": pallets},
        ])
        return booking

    @classmethod
    def _booking_pallets(cls, booking, count):
        pickup = booking.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        return cls.env["logistics.booking.pallet"].create([
            {"booking_id": booking.id, "sequence": (i + 1) * 10,
             "label": f"P-{i + 1}", "pickup_stop_id": pickup.id}
            for i in range(count)
        ])

    @classmethod
    def _depart_pickups(cls, job):
        """Mark the job's pickup stops departed (real-world: driver leaves
        the origin) so items stop counting as pending future pickups."""
        job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup").write({
                "actual_departure_time": __import__(
                    "datetime").datetime(2026, 9, 1, 12, 0)})
        return job

    def _png(self, tag):
        """Distinct valid PNG per tag (trailing byte after IEND), as the
        str base64 the upload validator expects."""
        return base64.b64encode(PNG_1PX + tag.encode()).decode("ascii")

    # ── #29: per-pickup-stop pallet prefix ───────────────────────────

    def test_a_milk_run_prefix_from_second_pickup(self):
        """A milk-run item received at the SECOND pickup stop is named
        from that stop's facility (Terra Freska → 'TF'), never from the
        job-level partner / first-pickup location."""
        first = self._location("ABC Cold Storage")
        terra = self._location("Terra Freska Produce")
        booking = self._booking(pallets=1, pickup_loc=first, snapshot_alloc=True)
        job = booking._create_dispatch_job()
        job.pickup_saved_location_id = first.id  # driver-app jobs carry this
        self.assertEqual(len(job.item_ids), 1)
        # Second milk-run pickup stop on the SAME job at Terra Freska.
        second = self.env["prema.dispatch.stop"].create({
            "job_id": job.id, "stop_type": "pickup", "sequence": 12,
            "partner_id": self.partner.id, "saved_location_id": terra.id,
            "address": terra.address,
        })
        # Job-level (singleton) prefix still comes from the job pickup…
        self.assertEqual(job._actual_pallet_prefix(), "AC")
        # …but the stop-aware prefix names the SECOND facility.
        self.assertEqual(job._actual_pallet_prefix(pickup_stop=second), "TF")
        # Confirming 1 pallet at the second stop creates "TF-01", not
        # "AC-01" / partner-derived "P-01".
        items = job._sync_actual_pallet_items(1, pickup_stop=second)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "TF-01")
        self.assertEqual(items[0].pickup_stop_id.id, second.id)

    def test_b_prefix_singleton_unchanged_single_pickup(self):
        """Single-pickup jobs keep the job-level prefix (no regression
        from the per-stop change)."""
        first = self._location("United Dairy")
        booking = self._booking(pallets=1, pickup_loc=first, snapshot_alloc=True)
        job = booking._create_dispatch_job()
        job.pickup_saved_location_id = first.id  # driver-app jobs carry this
        pickup_stop = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        self.assertEqual(job._actual_pallet_prefix(), "U")
        self.assertEqual(job._actual_pallet_prefix(pickup_stop=pickup_stop), "U")
        items = job._sync_actual_pallet_items(1)
        self.assertEqual(items[0].name, "U-01")

    # ── #20: delivery_choices without a load_plan_job link ───────────

    def test_c_delivery_choices_fallback_no_job_link(self):
        """The exact defect shape: pallets on a plan with NO
        load_plan_job link (available_stops empty). Every item payload
        still carries delivery_choices from its OWN job's dropoff stops,
        so Step 2 never renders 'No delivery stop assigned'."""
        booking = self._booking(pallets=2, snapshot_alloc=True)
        job = self._depart_pickups(booking._create_dispatch_job())
        self.assertEqual(len(job.item_ids), 2)
        plan = self.env["prema.dispatch.load.plan"].create({
            "vehicle_id": self.vehicle.id,
            "operating_date": date(2026, 9, 1),
            "layout_template_id": self._layout_template().id,
        })
        # Attach pallets directly — deliberately NO load_plan_job row.
        job.item_ids.write({"load_plan_id": plan.id})

        payload = plan.get_load_plan()
        self.assertEqual(payload["available_stops"], [],
                         "defect shape requires no plan-level stop groups")
        self.assertEqual(payload["jobs"], [])
        items = payload["unassigned_items"] + payload["non_floor_items"]
        self.assertEqual(len(items), 2)
        dropoff = job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff" and not s.planning_only)
        for item in items:
            self.assertTrue(item["delivery_choices"],
                            f"item {item['name']} must have choices")
            self.assertIn(dropoff.id,
                          [c["stop_id"] for c in item["delivery_choices"]])
            self.assertTrue(item["delivery_stop_id"],
                            "item's own delivery_stop_id stays authoritative")
            self.assertTrue(item["delivery_stop_name"])

    def test_d_delivery_choices_plan_group_wins(self):
        """With a load_plan_job link present, the plan-level group is
        used (shared group) — the fallback never shadows a real link."""
        booking = self._booking(pallets=2, snapshot_alloc=True)
        job = self._depart_pickups(booking._create_dispatch_job())
        plan = self.env["prema.dispatch.load.plan"].create({
            "vehicle_id": self.vehicle.id,
            "operating_date": date(2026, 9, 1),
            "layout_template_id": self._layout_template().id,
        })
        job.item_ids.write({"load_plan_id": plan.id})
        plan.write({"load_plan_job_ids": [(0, 0, {
            "job_id": job.id, "state": "included",
        })]})

        payload = plan.get_load_plan()
        self.assertEqual(len(payload["available_stops"]), 1)
        group = payload["available_stops"][0]
        self.assertEqual(group["job_id"], job.id)
        for item in payload["unassigned_items"] + payload["non_floor_items"]:
            self.assertEqual([c["stop_id"] for c in item["delivery_choices"]],
                             [s["stop_id"] for s in group["stops"]])

    # ── #20: legacy bridge writes logistics_booking_pallet_id ────────

    def test_e_legacy_bridge_links_booking_pallets(self):
        """Per-pallet items created by the legacy bridge carry their
        logistics.booking.pallet link (1:1 by pallet number) — the
        missing link that produced 'no linked booking pallet' rows."""
        booking = self._booking(pallets=2, snapshot_alloc=True)
        pallets = self._booking_pallets(booking, 2)
        job = booking._create_dispatch_job()
        items = job.item_ids.sorted("sequence")
        self.assertEqual(len(items), 2)
        for idx, item in enumerate(items, start=1):
            self.assertTrue(item.logistics_booking_pallet_id,
                            f"{item.name} must be linked to a booking pallet")
            self.assertEqual(item.logistics_booking_pallet_id.label,
                             f"P-{idx}")
        self.assertEqual(items.mapped("logistics_booking_pallet_id").ids,
                         pallets.ids)

    def test_f_legacy_bridge_no_fabricated_link(self):
        """Count mismatch (2 booking pallet rows vs 3 physical pallets)
        never fabricates a 1:1 link — the map stays empty and items are
        created unlinked (fail-safe, no wrong links)."""
        booking = self._booking(pallets=3, snapshot_alloc=True)
        self._booking_pallets(booking, 2)  # stale/partial row set
        job = booking._create_dispatch_job()
        items = job.item_ids.sorted("sequence")
        self.assertEqual(len(items), 3)
        self.assertFalse(any(items.mapped("logistics_booking_pallet_id")),
                         "mismatched pallet rows must not produce links")

    # ── #21: POPP upload rules ───────────────────────────────────────

    def test_g_popp_cap_and_scope(self):
        """POPP: max 4 photos per pallet, pallet must belong to THIS
        pickup, and every row creates a canonical evidence record with
        pallet_id (spec §20/§35)."""
        booking = self._booking(pallets=2, snapshot_alloc=True)
        job = booking._create_dispatch_job()
        pickup = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        item = job.item_ids.sorted("sequence")[0]

        for i in range(4):
            res = job.driver_add_evidence(
                pickup.id, "popp", self._png(f"tag{i}"),
                f"PALLET_{item.name}_POPP_20260901_{i}.png",
                {"pallet_id": item.id, "captured_at": "2026-09-01T08:00:00"})
            self.assertTrue(res["success"], res)
            self.assertEqual(res["pallet_id"], item.id)
        self.assertEqual(len(item.popp_attachment_ids), 4)

        # 5th photo → cap (spec §20: max 4 per physical pallet).
        res = job.driver_add_evidence(
            pickup.id, "popp", self._png("tag5"),
            f"PALLET_{item.name}_POPP_20260901_5.png",
            {"pallet_id": item.id})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "popp_limit")

        # A pallet from ANOTHER job / not picked up here → rejected.
        other = self._booking(pallets=1, snapshot_alloc=True)._create_dispatch_job()
        other_item = other.item_ids[:1]
        res = job.driver_add_evidence(
            pickup.id, "popp", self._png("tag6"),
            "PALLET_X_POPP_20260901_6.png",
            {"pallet_id": other_item.id})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "pallet_not_found")

        # Canonical evidence rows carry the pallet link (§20 metadata).
        ev = self.env["prema.dispatch.evidence"].search([
            ("stop_id", "=", pickup.id), ("evidence_type", "=", "popp"),
        ])
        self.assertEqual(len(ev), 4)
        self.assertEqual(set(ev.mapped("pallet_id.id")), {item.id})

        # Same bytes again → dedupe (never a 5th row).
        res = job.driver_add_evidence(
            pickup.id, "popp", self._png("tag0"),
            f"PALLET_{item.name}_POPP_20260901_0.png",
            {"pallet_id": item.id})
        self.assertTrue(res["success"])
        self.assertTrue(res.get("duplicate"))
        self.assertEqual(len(item.popp_attachment_ids), 4)

    # ── warehouse strip of the new payload keys ──────────────────────

    def test_h_warehouse_strips_delivery_choices(self):
        """get_load_plan_for_warehouse keeps stop_id/sequence in
        delivery_choices but strips customer names (and stops) — the
        warehouse never sees customer identity."""
        booking = self._booking(pallets=2, snapshot_alloc=True)
        job = self._depart_pickups(booking._create_dispatch_job())
        plan = self.env["prema.dispatch.load.plan"].create({
            "vehicle_id": self.vehicle.id,
            "operating_date": date(2026, 9, 1),
            "layout_template_id": self._layout_template().id,
        })
        job.item_ids.write({"load_plan_id": plan.id})
        plan.write({"load_plan_job_ids": [(0, 0, {
            "job_id": job.id, "state": "included",
        })]})

        payload = plan.get_load_plan_for_warehouse()
        for item in payload["unassigned_items"] + payload["non_floor_items"]:
            for choice in item["delivery_choices"]:
                self.assertEqual(sorted(choice.keys()),
                                 ["sequence", "stop_id"])
            for stop in item["stops"]:
                self.assertEqual(sorted(stop.keys()),
                                 ["sequence", "stop_id"])
        self.assertTrue(all(
            "customer" not in j for j in payload["jobs"]))

    def test_i_popp_filename_convention_metadata(self):
        """Driver-uploaded POPP filenames follow the
        PALLET_<item>_POPP_<stamp> convention through the full path —
        the stored attachment name and evidence checksum metadata are
        present (spec §16 metadata survives into the evidence row)."""
        booking = self._booking(pallets=1, snapshot_alloc=True)
        job = booking._create_dispatch_job()
        pickup = job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")[:1]
        item = job.item_ids[:1]

        res = job.driver_add_evidence(
            pickup.id, "popp", self._png("meta"),
            f"PALLET_{item.name}_POPP_20260901_081530.png",
            {"pallet_id": item.id, "captured_at": "2026-09-01T08:15:30",
             "lat": 43.62, "lng": -79.45, "device": "test-driver"})
        self.assertTrue(res["success"], res)
        self.assertTrue(
            res["name"].startswith(f"PALLET_{item.name}_POPP_"),
            f"unexpected filename {res['name']}")
        ev = self.env["prema.dispatch.evidence"].browse(res["evidence_id"])
        self.assertEqual(ev.evidence_type, "popp")
        self.assertEqual(ev.pallet_id.id, item.id)
        self.assertEqual(ev.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
                         "2026-09-01 08:15:30")
        self.assertEqual(ev.lat, 43.62)
        self.assertEqual(ev.device, "test-driver")
        self.assertTrue(ev.checksum_sha256)

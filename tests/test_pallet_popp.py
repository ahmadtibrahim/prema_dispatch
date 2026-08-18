"""Phase 4 — Pallet workflow (spec §19-§23, §5).

POPP per-pallet evidence (1-4 photos, pallet-owned, never invoice-copied),
the No Access / Sealed Load override with audit trail, and the full
Pickup Confirmation gate (pallet assignment complete AND POPP complete OR
documented override, plus §5 variance notes on a mismatch).
"""
import base64
import io

from odoo.tests import TransactionCase, tagged

try:
    from PIL import Image
except ImportError:
    Image = None


def _jpeg_b64(color=(120, 60, 200), size=(48, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


@tagged("post_install", "-at_install")
class TestPalletPopp(TransactionCase):

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create({"name": "Popp Customer", "is_company": True})
        self.driver_partner = self.env["res.partner"].create({"name": "Popp Driver Partner"})
        self.driver_user = self.env["res.users"].create({
            "name": "Popp Driver",
            "login": "poppdriver",
            "partner_id": self.driver_partner.id,
            "groups_id": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("prema_dispatch.group_dispatch_driver").id),
            ],
        })
        self.driver_env = self.env(user=self.driver_user.id)
        stage = self.env["prema.dispatch.stage"].search(
            [("is_booking_phase", "=", True)], limit=1) or \
            self.env["prema.dispatch.stage"].create({
                "name": "Popp Booking", "is_booking_phase": True})
        self.job = self.env["prema.dispatch.job"].create({
            "name": "POPPJOB-0001",
            "partner_id": self.customer.id,
            "stage_id": stage.id,
            "driver_id": self.driver_partner.id,
            "scheduled_pickup": "2026-08-18 09:00:00",
            "expected_pallet_count": 2,
            "approximate_skids": 2,
        })
        self.stop = self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": "pickup",
            "sequence": 10,
            "name": "Popp Warehouse",
            "scheduled_time": "2026-08-18 09:00:00",
        })
        self.drops = self.env["prema.dispatch.stop"].create([
            {"job_id": self.job.id, "stop_type": "dropoff", "sequence": 20,
             "name": "Drop A", "scheduled_time": "2026-08-18 12:00:00"},
            {"job_id": self.job.id, "stop_type": "dropoff", "sequence": 30,
             "name": "Drop B", "scheduled_time": "2026-08-18 15:00:00"},
        ])

    # ── helpers ────────────────────────────────────────────────

    def _confirm_actuals(self, actual=2, notes="", extra=None):
        values = {
            "job_id": self.job.id,
            "actual_received_pallet_count": actual,
            "variance_notes": notes,
            "route_sheet_received": False,
        }
        if extra:
            values.update(extra)
        return self.driver_env["prema.dispatch.job"].driver_confirm_pickup_actuals(
            self.stop.id, values)

    def _items(self):
        return self.job.item_ids.filtered(
            lambda i: i.consumes_floor_position and i.status != "cancelled")

    def _allocate_all(self, stop=None):
        items = self._items()
        target = stop or self.drops[0]
        for item in items:
            self.env["prema.dispatch.pallet.stop.allocation"].create({
                "dispatch_item_id": item.id,
                "stop_id": target.id,
            })

    def _add_popp(self, item, color=(120, 60, 200), extra=None):
        meta = {
            "pallet_id": item.id,
            "captured_at": "2026-08-18 09:30:00",
            "lat": 43.7, "lng": -79.4, "device": "test-app",
        }
        if extra:
            meta.update(extra)
        return self.driver_env["prema.dispatch.job"].driver_add_evidence(
            self.stop.id, "popp", _jpeg_b64(color=color), "popp.jpg",
            extra=meta)

    # ── §20 POPP upload ────────────────────────────────────────

    def test_01_popp_upload_lands_on_pallet_only(self):
        self._allocate_all()
        r = self._confirm_actuals()
        self.assertFalse(r.get("success"))  # gate: POPP not yet uploaded
        item = self._items()[0]
        r2 = self._add_popp(item)
        self.assertTrue(r2.get("success"), r2)
        self.assertEqual(item.popp_attachment_ids.ids, [r2["id"]])
        self.assertNotIn(r2["id"], self.stop.pop_attachment_ids.ids)
        ev = self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r2["id"])], limit=1)
        self.assertEqual(ev.evidence_type, "popp")
        self.assertEqual(ev.pallet_id, item)
        self.assertEqual(ev.stop_id, self.stop)
        # pallet-owned proof never reaches the invoice
        inv = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.customer.id,
            "invoice_date": "2026-08-18",
        })
        self.job.invoice_id = inv.id
        self.assertTrue(self.env["ir.attachment"].search_count([
            ("res_model", "=", "account.move"), ("res_id", "=", inv.id)]) == 0)

    def test_02_popp_cap_four_photos(self):
        self._confirm_actuals()  # materializes the pallet items (sync)
        self._allocate_all()
        item = self._items()[0]
        # distinct images — identical bytes would be deduped as duplicates
        for color in [(120, 60, 200), (10, 20, 30), (200, 50, 50), (30, 200, 90)]:
            self.assertTrue(self._add_popp(item, color=color).get("success"))
        r = self._add_popp(item, color=(255, 0, 0))
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "popp_limit")
        self.assertEqual(len(item.popp_attachment_ids), 4)

    def test_03_popp_rejects_foreign_pallet(self):
        self._allocate_all()
        other_job = self.env["prema.dispatch.job"].create({
            "name": "POPPJOB-0002",
            "partner_id": self.customer.id,
            "stage_id": self.job.stage_id.id,
            "expected_pallet_count": 1,
        })
        other_item = self.env["prema.dispatch.item"].create({
            "job_id": other_job.id, "name": "OTHER-01", "load_unit_type": "pallet",
        })
        r = self._add_popp(other_item)
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "pallet_not_found")

    def test_04_popp_remove(self):
        self._confirm_actuals()
        item = self._items()[0]
        r = self._add_popp(item)
        rr = self.driver_env["prema.dispatch.job"].driver_remove_evidence(
            self.stop.id, "popp", r["id"], extra={"pallet_id": item.id})
        self.assertTrue(rr.get("success"))
        self.assertNotIn(r["id"], item.popp_attachment_ids.ids)
        self.assertFalse(self.env["prema.dispatch.evidence"].search(
            [("attachment_id", "=", r["id"])]))

    # ── §21/§23 Pickup Confirmation gate ───────────────────────

    def test_05_gate_blocks_unassigned_pallets(self):
        r = self._confirm_actuals()
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "pickup_gate_blocked")
        joined = " ".join(r["missing"])
        self.assertIn("Assign every pallet", joined)
        # allocation unlocks that requirement
        self._allocate_all()
        r = self._confirm_actuals()
        self.assertFalse(r.get("success"))
        joined = " ".join(r["missing"])
        self.assertNotIn("Assign every pallet", joined)
        self.assertIn("POPP photo required", joined)

    def test_06_gate_requires_popp_per_pallet(self):
        self._confirm_actuals()
        self._allocate_all()
        items = self._items()
        self._add_popp(items[0])
        r = self._confirm_actuals()
        self.assertFalse(r.get("success"))
        joined = " ".join(r["missing"])
        self.assertIn(items[1].name, joined)
        self._add_popp(items[1])
        r = self._confirm_actuals()
        self.assertTrue(r.get("success"), r)

    def test_07_override_bypasses_popp(self):
        self._confirm_actuals()
        self._allocate_all()
        r = self.driver_env["prema.dispatch.job"].driver_create_popp_override(
            self.stop.id, "preloaded_sealed", seal_number="S-482913",
            lat=43.7, lng=-79.4)
        self.assertTrue(r.get("success"), r)
        ov = self.env["prema.dispatch.popp.override"].search(
            [("stop_id", "=", self.stop.id)], limit=1)
        self.assertEqual(ov.reason, "preloaded_sealed")
        self.assertEqual(ov.seal_number, "S-482913")
        self.assertEqual(ov.overridden_by, self.driver_user)
        # audit trail on the job timeline
        self.assertTrue(self.job.message_ids.search_count(
            [("model", "=", "prema.dispatch.job"),
             ("res_id", "=", self.job.id),
             ("body", "ilike", "POPP requirement overridden")]))
        # gate now passes without any POPP photos
        r = self._confirm_actuals()
        self.assertTrue(r.get("success"), r)

    def test_08_new_override_supersedes_old(self):
        self._confirm_actuals()
        self._allocate_all()
        self.driver_env["prema.dispatch.job"].driver_create_popp_override(
            self.stop.id, "policy_no_photography")
        self.driver_env["prema.dispatch.job"].driver_create_popp_override(
            self.stop.id, "security_restriction", seal_number="S-99")
        overrides = self.env["prema.dispatch.popp.override"].with_context(
            active_test=False).search(  # superseded overrides kept, inactive
            [("stop_id", "=", self.stop.id)], order="id")
        self.assertEqual(len(overrides), 2)
        self.assertFalse(overrides[0].active)
        self.assertTrue(overrides[1].active)

    def test_09_variance_notes_required_on_mismatch(self):
        # expected 2, actual 3 → +1 Pallet Difference needs notes (§5).
        # The variance item is created by the first confirm attempt (the
        # actuals sync runs before the gate); the gate still blocks on
        # the missing notes.
        r = self._confirm_actuals(actual=3, notes="")
        self.assertFalse(r.get("success"))
        self.assertIn("Pallet Difference", " ".join(r["missing"]))
        self._allocate_all()
        for item in self._items():
            self._add_popp(item)
        # with notes the confirmation passes
        r = self._confirm_actuals(actual=3, notes="Extra pallet added by shipper.")
        self.assertTrue(r.get("success"), r)

    def test_10_override_invalid_reason_rejected(self):
        r = self.driver_env["prema.dispatch.job"].driver_create_popp_override(
            self.stop.id, "i_feel_like_it")
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "invalid_reason")

    def test_11_foreign_driver_cannot_popp_or_override(self):
        self._confirm_actuals()
        item = self._items()[0]
        other_partner = self.env["res.partner"].create({"name": "Other Driver"})
        other_user = self.env["res.users"].create({
            "name": "Other Driver",
            "login": "otherdriver",
            "partner_id": other_partner.id,
            "groups_id": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("prema_dispatch.group_dispatch_driver").id),
            ],
        })
        r = self.env(user=other_user.id)["prema.dispatch.job"].driver_add_evidence(
            self.stop.id, "popp", _jpeg_b64(), "x.jpg",
            extra={"pallet_id": item.id})
        self.assertFalse(r.get("success"))
        self.assertEqual(r.get("code"), "unauthorized")
        r2 = self.env(user=other_user.id)["prema.dispatch.job"].driver_create_popp_override(
            self.stop.id, "other")
        self.assertFalse(r2.get("success"))
        self.assertEqual(r2.get("code"), "unauthorized")

    # ── §23 confirmation GPS ───────────────────────────────────

    def test_12_confirmation_records_gps(self):
        self._confirm_actuals()
        self._allocate_all()
        items = self._items()
        for item in items:
            self._add_popp(item)
        r = self._confirm_actuals(extra={"lat": 43.768431, "lng": -79.619128})
        self.assertTrue(r.get("success"), r)
        self.assertEqual(self.job.pickup_actuals_confirmed_lat, 43.768431)
        self.assertEqual(self.job.pickup_actuals_confirmed_lng, -79.619128)

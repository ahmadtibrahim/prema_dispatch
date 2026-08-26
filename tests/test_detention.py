"""Phase 10 detention — targeted verification (TEST I, ONE run, rolled back).

Covers the complete detention authority on the dispatch-module side:

  I1  Rule matching hierarchy: (partner, facility) > (partner) >
      (facility) > company default — single _match authority.
  I2  Charge formula: billable = MAX(dwell − free, 0),
      units = CEILING(billable / increment), charge = units × rate.
  I3  _suggest_for_stop idempotent (refreshes drafts, never duplicates,
      no suggestion when dwell <= free).
  I4  Approve / Modify / Waive workflow with review trail; re-reviewing
      an already-reviewed item raises UserError.
  I5  Company default baseline comes from the ONE config parameter
      (free 30 / increment 30 / rate 0.0 → suggested 0.0).

Nothing commits — every test rolls back.
"""

import json

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestDetention(TransactionCase):
    """Phase 10 customer detention (dispatch-module side)."""

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
            {"name": "Detention Test Customer"})
        cls.location = cls.env["prema.dispatch.location"].create({
            "name": "Detention Test Facility",
            "address": "123 Test St, Ontario",
            "pin_lat": 43.6,
            "pin_lng": -79.4,
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

    def _rule(self, free=30, increment=30, rate=25.0, partner=None,
              facility=None):
        return self.env["prema.dispatch.detention.rule"].create({
            "partner_id": partner.id if partner else False,
            "facility_id": facility.id if facility else False,
            "free_minutes": free,
            "increment_minutes": increment,
            "rate_per_increment": rate,
        })

    def _stop(self, dwell_minutes, facility=None):
        arrival = "2026-09-07 09:00:00"
        departure = "2026-09-07 09:%02d:00" % (dwell_minutes or 0)
        if dwell_minutes and dwell_minutes >= 60:
            departure = "2026-09-07 %02d:%02d:00" % (
                9 + dwell_minutes // 60, dwell_minutes % 60)
        return self.env["prema.dispatch.stop"].create({
            "job_id": self.job.id,
            "stop_type": "dropoff",
            "saved_location_id": facility.id if facility else self.location.id,
            "actual_arrival_time": arrival,
            "actual_departure_time": departure,
        })

    # ── I1: rule matching hierarchy ────────────────────────────────

    def test_i1_rule_match_hierarchy(self):
        """(partner, facility) > (partner) > (facility) > default."""
        Rule = self.env["prema.dispatch.detention.rule"]
        fac = self.location
        partner_only = self._rule(partner=self.partner)
        facility_only = self._rule(facility=fac)
        both = self._rule(partner=self.partner, facility=fac)

        match = Rule._match(self.partner.id, fac.id)
        self.assertEqual(match["rule"].id, both.id,
                         "the most specific rule wins")

        both.enabled = False
        match = Rule._match(self.partner.id, fac.id)
        self.assertEqual(match["rule"].id, partner_only.id,
                         "customer rule wins once the specific rule is off")

        partner_only.enabled = False
        match = Rule._match(self.partner.id, fac.id)
        self.assertEqual(match["rule"].id, facility_only.id,
                         "facility rule wins once the customer rule is off")

        facility_only.enabled = False
        match = Rule._match(self.partner.id, fac.id)
        self.assertFalse(match["rule"], "no rule matches → company default")

        default = Rule._company_defaults()
        self.assertEqual(default["free_minutes"], 30)
        self.assertEqual(default["increment_minutes"], 30)
        self.assertEqual(default["rate_per_increment"], 0.0)

    # ── I2 + I3: formula, idempotency, threshold ───────────────────

    def test_i2_i3_formula_and_idempotency(self):
        """95-min dwell with 30/30/25 → billable 65, units 3, $75;
        re-suggest refreshes the draft; <= free never suggests."""
        company_rule = self._rule()
        stop = self._stop(95)
        Item = self.env["prema.dispatch.detention.item"]

        item = Item._suggest_for_stop(stop)
        self.assertTrue(item, "suggestion must be created")
        self.assertEqual(item.actual_dwell_minutes, 95)
        self.assertEqual(item.billable_minutes, 65)
        self.assertEqual(item.units, 3)
        self.assertEqual(item.suggested_amount, 75.0)
        self.assertEqual(item.state, "draft")
        self.assertTrue(item.name.startswith("DET/"))

        # Idempotent: same stop → same item, refreshed, never duplicated.
        again = Item._suggest_for_stop(stop)
        self.assertEqual(again.id, item.id)
        self.assertEqual(
            Item.search_count([("stop_id", "=", stop.id)]), 1)

        # Dwell <= free → no suggestion.
        stop_short = self._stop(30)
        self.assertFalse(Item._suggest_for_stop(stop_short),
                         "free minutes are not billable")

        # Company-wide rule disabled + default rate 0.0 → suggested 0.0
        # (tracked, not charged). With the rule enabled the company-wide
        # rate 25.0 would apply — that fallback is exercised above.
        stop_free = self._stop(95, facility=self._other_facility())
        company_rule.enabled = False
        item_free = Item._suggest_for_stop(stop_free)
        self.assertTrue(item_free)
        self.assertEqual(item_free.units, 3)
        self.assertEqual(item_free.suggested_amount, 0.0)

    def _other_facility(self):
        return self.env["prema.dispatch.location"].create({
            "name": "Detention No-Rule Facility",
            "address": "124 Test St, Ontario",
            "pin_lat": 43.61,
            "pin_lng": -79.41,
        })

    # ── I4: Approve / Modify / Waive workflow ──────────────────────

    def test_i4_approve_modify_waive_workflow(self):
        """Approve books the suggested amount; Modify sets a reviewed
        amount with a reason; Waive zeroes it — each with a review trail;
        re-reviewing an approved item raises."""
        self._rule()
        Item = self.env["prema.dispatch.detention.item"]
        item = Item._suggest_for_stop(self._stop(95))

        # Approve → suggested amount confirmed + review trail.
        item.action_approve()
        self.assertEqual(item.state, "approved")
        self.assertEqual(item.approved_amount, 75.0)
        self.assertTrue(item.review_user_id)
        self.assertTrue(item.review_time)

        # Re-reviewing an approved item is refused.
        with self.assertRaises(UserError):
            item.action_approve()

        # Modify → dispatcher edits the amount + reason, then confirms.
        item2 = Item._suggest_for_stop(self._stop(95))
        item2.write({"approved_amount": 60.0, "reason_notes": "negotiated"})
        item2.action_modify()
        self.assertEqual(item2.state, "modified")
        self.assertEqual(item2.approved_amount, 60.0)
        self.assertEqual(item2.reason_notes, "negotiated")
        with self.assertRaises(UserError):
            item2.action_modify()

        # Waive → zeroed with a reason, still reviewed.
        item3 = Item._suggest_for_stop(self._stop(95))
        item3.write({"reason_notes": "goodwill"})
        item3.action_waive()
        self.assertEqual(item3.state, "waived")
        self.assertEqual(item3.approved_amount, 0.0)
        self.assertEqual(item3.reason_notes, "goodwill")
        with self.assertRaises(UserError):
            item3.action_waive()

        # The review trail survived on all three.
        for it in (item, item2, item3):
            self.assertTrue(it.review_user_id)
            self.assertTrue(it.review_time)

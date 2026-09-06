"""D-A3B — dispatch side of the CRM recurring-opportunity bridge
(contract docs/A3_RECURRING_BRIDGE_CONTRACT.md in the engine repo).

Covered (all mail mocked — nothing ever leaves the DB):
  1. Activation happy path: agreement created (anchor CRM-REC-<id>[ — PO],
     one job per preferred weekday, mapped profile, dates + 365-day end
     default), activated through the existing rules, opportunity synced to
     Active with the anchor mirrored; ZERO occurrence-generation side
     effects (no bookings, cron semantics untouched).
  2. Guard failures raise clear UserErrors BEFORE anything is created and
     leave the opportunity untouched: potential/unconfirmed cadence,
     Sunday weekday (no Sunday service in the dispatch network), irregular
     frequency (stays engine-managed), duplicate agreement, Ended
     opportunity (never re-activated), expired effective dates, missing
     or Google-unverified pickup/delivery route anchors.
  3. Reverse sync: pause/expire/cancel mirror onto the opportunity;
     idempotent replay (double pause adds no note, no state change);
     an Ended opportunity is never resurrected by agreement syncs.
  4. Rate-confirmation intent (§17.1-17.2): the canonical customer RC
     draft (logistics.custom.quote factory) is produced once per agreement
     and pre-filled from the opportunity profile.

Run targeted, e.g.:
    odoo-bin -c <conf> -d <staging-db> -u prema_logistics_booking \\
        --test-tags /recurring_bridge --stop-after-init
"""
import unittest.mock as mock
from datetime import timedelta

from odoo.exceptions import UserError
from odoo.fields import Date
from odoo.tests import TransactionCase, tagged

from odoo.addons.mail.models.mail_mail import MailMail


@tagged("recurring_bridge")
class TestRecurringCrmBridge(TransactionCase):

    def setUp(self):
        super().setUp()
        # No real outbound delivery during tests.
        self.patcher = mock.patch.object(MailMail, "send", autospec=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.partner = self.env["res.partner"].create({
            "name": "Recurring Bridge Customer",
            "is_company": True,
        })
        self.lead = self.env["crm.lead"].create({
            "name": "Recurring Bridge Opportunity",
            "type": "opportunity",
            "partner_id": self.partner.id,
        })
        # Google-verified anchors the recurring jobs mirror (the base
        # model refuses location-kind jobs without a verified address).
        self.pickup_location = self.env["prema.dispatch.location"].create({
            "name": "RB Pickup Dock",
            "partner_id": self.partner.id,
            "address": "1000 Recurring Way, Vaughan, ON",
            "city": "Vaughan", "province_code": "ON", "postal_code": "L4L 1A1",
            "pin_lat": 43.82, "pin_lng": -79.55,
            "google_verified": True,
            "google_place_id": "E2E-RB-PICKUP",
        })
        self.delivery_location = self.env["prema.dispatch.location"].create({
            "name": "RB Customer Store",
            "partner_id": self.partner.id,
            "address": "2000 Recurring Rd, Toronto, ON",
            "city": "Toronto", "province_code": "ON", "postal_code": "M9C 1A1",
            "pin_lat": 43.65, "pin_lng": -79.56,
            "google_verified": True,
            "google_place_id": "E2E-RB-DELIVERY",
        })

    # ── helpers ──────────────────────────────────────────────────────

    def _new_opportunity(self, frequency="weekly", confirmed=True,
                         activate=True, **kw):
        """Fresh lead + contracted/confirmed opportunity in the given
        activation state (default: awaiting_activation)."""
        lead = self.env["crm.lead"].create({
            "name": "Recurring Bridge Opp %s" % (len(self.env[
                "crm.lead"].search([])) + 1),
            "type": "opportunity",
            "partner_id": self.partner.id,
        })
        vals = {"lead_id": lead.id, "frequency": frequency}
        vals.update(kw)
        # Google-verified route anchors by default (the base model refuses
        # location-kind jobs without a verified address); override via kw
        # to exercise the anchor guards.
        if "pickup_dispatch_location_id" not in vals:
            vals["pickup_dispatch_location_id"] = self.pickup_location.id
        if "delivery_dispatch_location_id" not in vals:
            vals["delivery_dispatch_location_id"] = self.delivery_location.id
        opp = self.env["crm.recurring.opportunity"].create(vals)
        if confirmed:
            opp.action_confirm_customer()      # → contracted + confirmed
            if activate:
                opp.action_begin_verification()
                opp.action_mark_ready_activate()
        return opp

    def _create_agreement(self, opp):
        """The single activation entry point (agreement model)."""
        return self.env[
            "logistics.recurring.agreement"
        ].action_activate_for_crm_opportunity(opp)

    def _audit_bodies(self, rec):
        return [(m.body or "") for m in rec.message_ids]

    def _linked_agreement(self, opp):
        return self.env["logistics.recurring.agreement"].search(
            [("crm_opportunity_id", "=", opp.id)])

    # ── 1. activation happy path ─────────────────────────────────────

    def test_activation_creates_agreement_and_syncs_opportunity(self):
        opp = self._new_opportunity(
            preferred_tuesday=True, preferred_thursday=True,
            expected_pallets=6, expected_weight_lbs=1200.0,
            commodity="Auto parts", expected_temperature_mode="reefer",
            required_temperature_c=-18.0, agreement_reference="PO-777")
        self.assertEqual(opp.activation_state, "awaiting_activation")
        bookings_before = self.env["logistics.booking"].search_count(
            [("partner_id", "=", self.partner.id)])

        agreement = self._create_agreement(opp)

        self.assertEqual(agreement.partner_id, self.partner)
        self.assertEqual(
            agreement.agreement_reference,
            "CRM-REC-%d — PO-777" % opp.id)
        self.assertEqual(agreement.crm_opportunity_id, opp)
        self.assertEqual(agreement.state, "active")
        self.assertTrue(agreement.active)
        # start/end map from the opportunity (end defaults +365 days when
        # the CRM record has none)
        self.assertEqual(agreement.start_date, Date.today())
        self.assertEqual(
            agreement.end_date, Date.today() + timedelta(days=365))
        self.assertIn("365 days", agreement.service_notes)
        # account manager reads the lead's salesperson; the default (the
        # acting user) applies when the lead has none assigned
        self.assertEqual(agreement.account_manager_id, self.env.user)
        # one job per selected weekday, shared frequency, mirrored profile
        jobs = agreement.job_ids
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sorted(jobs.mapped("preferred_weekday")),
                         ["1", "3"])
        self.assertTrue(all(j.frequency == "weekly" for j in jobs))
        self.assertTrue(all(j.pallets == 6 for j in jobs))
        self.assertTrue(all(j.weight_lbs == 1200.0 for j in jobs))
        self.assertTrue(all(j.temperature_mode == "reefer" for j in jobs))
        self.assertTrue(all(j.required_temperature_c == -18.0 for j in jobs))
        self.assertTrue(all(j.temperature_confirmed for j in jobs))
        self.assertTrue(all(j.commodity == "Auto parts" for j in jobs))
        # jobs mirror the opportunity's Google-verified anchors but keep
        # auto-generate OFF: activation must not start the booking
        # generator — the dispatcher reviews each job first
        self.assertTrue(all(j.pickup_kind == "location" for j in jobs))
        self.assertTrue(all(
            j.pickup_location_id == self.pickup_location for j in jobs))
        self.assertTrue(all(j.delivery_kind == "location" for j in jobs))
        self.assertTrue(all(
            j.delivery_location_id == self.delivery_location for j in jobs))
        self.assertTrue(all(not j.auto_generate for j in jobs))
        # chatter both sides
        self.assertTrue(any(
            "Created and activated from CRM recurring opportunity #%d" % opp.id
            in body for body in self._audit_bodies(agreement)))
        self.assertTrue(any(
            "dispatch agreement sync" in body
            for body in self._audit_bodies(opp)))
        # opportunity synced to Active with the anchor mirrored
        self.assertEqual(opp.activation_state, "active")
        self.assertTrue(opp.activated_at)
        self.assertEqual(
            opp.dispatch_agreement_reference,
            "CRM-REC-%d — PO-777" % opp.id)
        # NO occurrence generation side effect: activation created nothing
        # beyond the agreement/jobs, and no booking exists (the cron was
        # not touched and would be the only generator)
        self.assertEqual(self.env["logistics.booking"].search_count(
            [("partner_id", "=", self.partner.id)]), bookings_before)
        self.assertEqual(self.env["logistics.booking"].search_count(
            [("recurring_agreement_id", "=", agreement.id)]), 0)

    def test_no_selected_day_defaults_to_one_monday_job(self):
        opp = self._new_opportunity()
        agreement = self._create_agreement(opp)
        self.assertEqual(len(agreement.job_ids), 1)
        self.assertEqual(agreement.job_ids.preferred_weekday, "0")

    def test_monthly_frequency_keeps_default_monthly_week(self):
        opp = self._new_opportunity(
            frequency="monthly", preferred_wednesday=True)
        agreement = self._create_agreement(opp)
        self.assertEqual(agreement.frequency, "monthly")
        self.assertEqual(len(agreement.job_ids), 1)
        self.assertEqual(agreement.job_ids.frequency, "monthly")
        self.assertEqual(agreement.job_ids.monthly_week, "1")
        self.assertEqual(agreement.job_ids.preferred_weekday, "2")

    def test_opportunity_dates_are_forwarded_verbatim(self):
        start = Date.today() - timedelta(days=10)
        end = Date.today() + timedelta(days=400)
        opp = self._new_opportunity(start_date=start, end_date=end)
        agreement = self._create_agreement(opp)
        self.assertEqual(agreement.start_date, start)
        self.assertEqual(agreement.end_date, end)
        self.assertNotIn("365 days", agreement.service_notes)

    # ── 2. guard failures (raise BEFORE creating anything) ───────────

    def test_unconfirmed_potential_cadence_is_refused(self):
        opp = self._new_opportunity(confirmed=False)
        self.assertEqual(opp.activation_state, "never_activated")
        with self.assertRaises(UserError):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))
        self.assertEqual(opp.activation_state, "never_activated")

    def test_confirmed_but_still_potential_is_refused(self):
        opp = self._new_opportunity(confirmed=False)
        opp.write({"kind": "potential", "customer_confirmed": True})
        with self.assertRaisesRegex(UserError, "Contracted"):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))

    def test_sunday_weekday_raises_no_sunday_service(self):
        opp = self._new_opportunity(
            preferred_tuesday=True, preferred_sunday=True)
        with self.assertRaisesRegex(UserError, "no Sunday service"):
            self._create_agreement(opp)
        # nothing was created even though a valid Tuesday exists
        self.assertFalse(self._linked_agreement(opp))
        self.assertEqual(opp.activation_state, "awaiting_activation")

    def test_irregular_frequency_creates_no_agreement(self):
        opp = self._new_opportunity(frequency="irregular",
                                    frequency_detail="As needed")
        with self.assertRaisesRegex(UserError, "Irregular"):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))
        self.assertFalse(self.env["logistics.recurring.agreement"].search(
            [("partner_id", "=", self.partner.id)]))
        self.assertEqual(opp.activation_state, "awaiting_activation")

    def test_duplicate_agreement_is_refused(self):
        opp = self._new_opportunity()
        first = self._create_agreement(opp)
        with self.assertRaisesRegex(UserError, "already exists"):
            self._create_agreement(opp)
        self.assertEqual(self._linked_agreement(opp), first)

    def test_ended_opportunity_cannot_create_agreement(self):
        opp = self._new_opportunity()
        self._create_agreement(opp)
        opp.action_end()
        self.assertEqual(opp.activation_state, "ended")
        with self.assertRaisesRegex(UserError, "Ended"):
            self._create_agreement(opp)
        with self.assertRaisesRegex(UserError, "Ended"):
            self.env["logistics.recurring.agreement"].create({
                "partner_id": self.partner.id,
                "crm_opportunity_id": opp.id,
                "start_date": Date.today(),
                "end_date": Date.today() + timedelta(days=30),
            }).action_activate()

    def test_expired_effective_period_is_refused(self):
        opp = self._new_opportunity(
            start_date=Date.today() - timedelta(days=60),
            end_date=Date.today() - timedelta(days=30))
        with self.assertRaisesRegex(UserError, "ended on"):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))

    def test_missing_route_anchors_are_refused(self):
        opp = self._new_opportunity(
            pickup_dispatch_location_id=False,
            delivery_dispatch_location_id=False)
        with self.assertRaisesRegex(
                UserError, "Set the recurring Pickup and Delivery"):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))
        self.assertEqual(opp.activation_state, "awaiting_activation")

    def test_unverified_route_anchor_is_refused(self):
        unverified = self.env["prema.dispatch.location"].create({
            "name": "RB Unverified Dock",
            "address": "3000 Unverified Ave, Brampton, ON",
            "pin_lat": 43.73, "pin_lng": -79.76,
        })
        opp = self._new_opportunity(
            pickup_dispatch_location_id=unverified.id)
        with self.assertRaisesRegex(
                UserError, "must be verified through Google Places"):
            self._create_agreement(opp)
        self.assertFalse(self._linked_agreement(opp))

    # ── 3. reverse sync: idempotent, terminal-guarded ────────────────

    def test_reverse_sync_idempotent_replay_adds_no_note(self):
        opp = self._new_opportunity()
        agreement = self._create_agreement(opp)
        self.assertEqual(opp.activation_state, "active")

        agreement.action_pause()
        self.assertEqual(opp.activation_state, "paused")
        notes_after_pause = len(opp.message_ids)
        # replaying the same agreement state is an idempotent no-op
        agreement.action_pause()
        self.assertEqual(opp.activation_state, "paused")
        self.assertEqual(len(opp.message_ids), notes_after_pause)
        self.assertFalse(opp.prema_sync_activation_from_dispatch("paused"))

        agreement.action_activate()
        self.assertEqual(opp.activation_state, "active")
        agreement.action_cancel()
        self.assertEqual(opp.activation_state, "ended")

    def test_ended_opportunity_never_resurrected_by_sync(self):
        opp = self._new_opportunity()
        agreement = self._create_agreement(opp)
        opp.action_end()
        self.assertEqual(opp.activation_state, "ended")
        # pause syncs after the terminal state are skipped, not fatal
        agreement.action_pause()
        self.assertEqual(opp.activation_state, "ended")
        # ...and activation of a linked agreement is refused BEFORE any
        # write: the terminal Ended state is never resurrected from the
        # dispatch side either
        with self.assertRaisesRegex(UserError, "Ended"):
            agreement.action_activate()
        self.assertEqual(opp.activation_state, "ended")
        self.assertEqual(agreement.state, "paused")  # nothing was mutated

    # ── 4. rate-confirmation intent (§17.1-17.2) ─────────────────────

    def test_rate_confirmation_intent_creates_canonical_rc_draft(self):
        opp = self._new_opportunity(
            preferred_friday=True, expected_pallets=4,
            expected_weight_lbs=900.0, intent_rate_confirmation=True)
        agreement = self._create_agreement(opp)
        quotes = self.env["logistics.custom.quote"].search(
            [("crm_lead_id", "=", opp.lead_id.id)])
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes.partner_id, self.partner)
        self.assertEqual(quotes.state, "new")
        self.assertEqual(quotes.pallets, 4)
        self.assertEqual(quotes.weight_lbs, 900.0)
        self.assertIn("CRM-REC-%d" % opp.id, quotes.notes)
        self.assertTrue(any(
            "Rate Confirmation draft %s" % quotes.name in body
            for body in self._audit_bodies(agreement)))
        # exactly-once per agreement: re-activation adds no second draft
        agreement.action_pause()
        agreement.action_activate()
        self.assertEqual(self.env["logistics.custom.quote"].search_count(
            [("crm_lead_id", "=", opp.lead_id.id)]), 1)

    def test_no_intent_flag_no_rate_confirmation(self):
        opp = self._new_opportunity()
        self._create_agreement(opp)
        self.assertFalse(self.env["logistics.custom.quote"].search(
            [("crm_lead_id", "=", self.lead.id)]))

    # ── 5. button surface (opportunity extension) ────────────────────

    def test_opportunity_button_action_opens_created_agreement(self):
        opp = self._new_opportunity(preferred_monday=True)
        action = opp.action_create_recurring_agreement()
        agreement = self._linked_agreement(opp)
        self.assertEqual(action["res_model"], "logistics.recurring.agreement")
        self.assertEqual(action["res_id"], agreement.id)
        self.assertEqual(opp.activation_state, "active")
